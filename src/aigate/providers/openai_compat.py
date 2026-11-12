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
        enable_thinking: bool | None,
        stream: bool,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
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
        if enable_thinking is not None:
            # Vendor-specific (DashScope qwen3 reasoning models): forwarded under its own
            # name. Translating it to `reasoning_effort` would invent semantics the vendor
            # never promised, and dropping unknown fields silently is the bug this module
            # exists to prevent. An upstream that rejects it answers 400, which leaves here
            # as `upstream_4xx` (non-retryable) and reaches the caller unchanged.
            payload["enable_thinking"] = enable_thinking
        if tools is not None:
            # Function calling, verbatim: the gateway has no opinion about JSON Schema and
            # must not re-serialise it (an argument example, a `strict` flag or an extra
            # vendor key would be lost). Empty list is forwarded as sent: it is the
            # caller's body, and a vendor that dislikes it says so with a 400.
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = parallel_tool_calls
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
        enable_thinking: bool | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
    ) -> ChatResult:
        payload = self._build_payload(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stop=stop,
            user=user,
            enable_thinking=enable_thinking,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
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

        # `message.tool_calls` is handed back **as the vendor wrote it**: ids, indices and
        # the JSON-in-a-string `arguments` all matter to the client's next turn. `None`
        # (the key is absent) is the ordinary text answer and stays `None`; a present but
        # non-list value is a broken upstream, reported rather than coerced into nothing.
        tool_calls = choice["message"].get("tool_calls")
        if tool_calls is not None and not (
            isinstance(tool_calls, list) and all(isinstance(tc, dict) for tc in tool_calls)
        ):
            raise ProviderError(
                "upstream_bad_payload", f"message.tool_calls is not a list of objects: {tool_calls!r}"[:500]
            )

        return ChatResult(
            content=content,
            model=data.get("model", model),
            usage=self.normalize_usage(data.get("usage") or {}),
            raw=data,
            latency_ms=latency_ms,
            # Verbatim: whatever the vendor called it (`stop`, `length`,
            # `content_filter`, `tool_calls`, a Chinese-API-specific string). The router
            # decides whether to pass it through or derive one; this layer must not
            # normalise.
            finish_reason=choice.get("finish_reason"),
            tool_calls=tool_calls or None,
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
        enable_thinking: bool | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
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
            enable_thinking=enable_thinking,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
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
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        # `delta.tool_calls` arrives in *fragments*: the first one carries
                        # `id`/`type`/`function.name`, later ones carry only
                        # `function.arguments` pieces keyed by `index`. Handing each
                        # fragment on unchanged (rather than waiting for a complete call)
                        # is the only way the client can assemble them the way OpenAI
                        # does — and it is why `index` must survive untouched.
                        tool_calls = delta.get("tool_calls")
                        if tool_calls is not None and not (
                            isinstance(tool_calls, list) and all(isinstance(tc, dict) for tc in tool_calls)
                        ):
                            raise ProviderError(
                                "upstream_bad_payload",
                                f"delta.tool_calls is not a list of objects: {tool_calls!r}"[:500],
                            )
                        if text or tool_calls:
                            yield StreamEvent(
                                type="delta", text=text or "", tool_calls=tool_calls or None
                            )
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
