"""OpenAI-compatible provider — one adapter, three vendors.

DashScope, DeepSeek and vLLM all expose `POST {base}/chat/completions`. The point
of this class is not that they are identical, but that the *differences* are
absorbed here: field names, usage shape, error codes, stream chunk format.
"""

from __future__ import annotations

import ipaddress
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from aigate.providers.base import (
    BaseProvider,
    ChatResult,
    ProviderError,
    ProviderUsage,
    StreamEvent,
)


def is_loopback_url(url: str) -> bool:
    """True when the URL points at this machine (127.0.0.0/8, ::1, localhost).

    httpx honours HTTP(S)_PROXY / ALL_PROXY from the environment, but ignores Windows'
    `proxy_bypass()` (the ProxyOverride list): with the usual local Clash at
    127.0.0.1:7897, a request to the default vLLM at 127.0.0.1:8000 is handed to that
    proxy and dies. Loopback must therefore never inherit the environment proxy, while
    remote upstreams still do (a corporate egress proxy is a real deployment).
    """
    host = (httpx.URL(url).host or "").strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OpenAICompatibleProvider(BaseProvider):
    supports_stream = True

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str = "",
        timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            headers=headers,
            trust_env=not is_loopback_url(self._url),
        )

    # ------------------------------------------------------------------ non-stream
    def _build_payload(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
        top_p: float | None,
        stop: str | list[str] | None,
        user: str | None,
        stream: bool,
    ) -> dict[str, Any]:
        """The single place where a request field becomes an upstream field.

        Both `chat` and `stream` go through here on purpose: they used to build their own
        dicts, so a field could be added to one and silently missing from the other. Only
        non-None values are written — an absent key means "vendor default", while
        `"top_p": null` is a validation error on some vendors.
        """
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
        if stream:
            # Metering needs real token counts; without this DashScope/OpenAI omit `usage`.
            payload["stream_options"] = {"include_usage": True}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if top_p is not None:
            payload["top_p"] = top_p
        if stop is not None:
            payload["stop"] = stop
        if user is not None:
            payload["user"] = user
        return payload

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: str | list[str] | None = None,
        user: str | None = None,
    ) -> ChatResult:
        payload = self._build_payload(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stop=stop,
            user=user,
            stream=False,
        )

        started = time.perf_counter()
        try:
            resp = await self._client.post(self._url, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError("upstream_timeout", str(exc), retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("upstream_connection_error", str(exc), retryable=True) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        self._raise_for_status(resp.status_code, resp.text)

        data = resp.json()
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
        except (KeyError, IndexError) as exc:
            raise ProviderError("upstream_bad_payload", str(data)[:500]) from exc

        return ChatResult(
            content=content,
            model=data.get("model", model),
            usage=self.normalize_usage(data.get("usage") or {}),
            raw=data,
            latency_ms=latency_ms,
            # Verbatim: whatever the vendor called it (`stop`, `length`,
            # `content_filter`, a Chinese-API-specific string). The router decides
            # whether to pass it through or derive one; this layer must not normalise.
            finish_reason=choice.get("finish_reason"),
        )

    # ---------------------------------------------------------------------- stream
    async def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: str | list[str] | None = None,
        user: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """SSE passthrough.

        The status code is checked *inside* the stream context, before the body is
        consumed. A 400 here must surface as a normal ProviderError, not as a
        half-written stream the caller cannot interpret.
        """
        payload = self._build_payload(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stop=stop,
            user=user,
            stream=True,
        )

        try:
            async with self._client.stream("POST", self._url, json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "ignore")[:500]
                    self._raise_for_status(resp.status_code, body)
                upstream_finish: str | None = None
                upstream_usage: ProviderUsage | None = None
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    # `stream_options.include_usage` makes the vendor append one chunk
                    # with `choices: []` and the token counts. It arrives *before*
                    # `[DONE]`, so it must be read here and handed on — dropping it is
                    # why streaming used to have no real usage at all.
                    reported = chunk.get("usage")
                    if reported:
                        upstream_usage = self.normalize_usage(reported)
                    for choice in chunk.get("choices") or []:
                        reason = choice.get("finish_reason")
                        if reason:
                            upstream_finish = reason
                        text = (choice.get("delta") or {}).get("content")
                        if text:
                            yield StreamEvent(type="delta", text=text)
                # Always close with what we actually observed — possibly nothing.
                yield StreamEvent(
                    type="done", finish_reason=upstream_finish, usage=upstream_usage
                )
        except httpx.TimeoutException as exc:
            raise ProviderError("upstream_timeout", str(exc), retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("upstream_connection_error", str(exc), retryable=True) from exc

    # --------------------------------------------------------------------- helpers
    @staticmethod
    def _raise_for_status(status_code: int, body: str) -> None:
        if status_code < 400:
            return
        if status_code == 429:
            raise ProviderError("upstream_rate_limited", body[:500], retryable=True, upstream_status=429)
        if status_code >= 500:
            raise ProviderError("upstream_5xx", body[:500], retryable=True, upstream_status=status_code)
        # Never retry a client error: it will fail identically and cost twice.
        raise ProviderError("upstream_4xx", body[:500], retryable=False, upstream_status=status_code)

    @staticmethod
    def normalize_usage(raw: dict[str, Any]) -> ProviderUsage:
        """Vendors disagree on field names; the gateway speaks one language."""
        return ProviderUsage(
            input_tokens=int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0),
            output_tokens=int(raw.get("completion_tokens") or raw.get("output_tokens") or 0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
