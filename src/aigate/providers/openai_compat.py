"""OpenAI-compatible provider — one adapter, three vendors.

DashScope, DeepSeek and vLLM all expose `POST {base}/chat/completions`. The point
of this class is not that they are identical, but that the *differences* are
absorbed here: field names, usage shape, error codes.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from aigate.providers.base import BaseProvider, ChatResult, ProviderError, ProviderUsage


class OpenAICompatibleProvider(BaseProvider):
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
            timeout=httpx.Timeout(timeout_s, connect=5.0), headers=headers
        )

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        started = time.perf_counter()
        try:
            resp = await self._client.post(self._url, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError("upstream_timeout", str(exc), retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("upstream_connection_error", str(exc), retryable=True) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)

        if resp.status_code == 429:
            raise ProviderError(
                "upstream_rate_limited", resp.text[:500], retryable=True, upstream_status=429
            )
        if resp.status_code >= 500:
            raise ProviderError(
                "upstream_5xx",
                resp.text[:500],
                retryable=True,
                upstream_status=resp.status_code,
            )
        if resp.status_code >= 400:
            # Never retry a client error: it will fail identically and cost twice.
            raise ProviderError(
                "upstream_4xx",
                resp.text[:500],
                retryable=False,
                upstream_status=resp.status_code,
            )

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
        )

    @staticmethod
    def normalize_usage(raw: dict[str, Any]) -> ProviderUsage:
        """Vendors disagree on field names; gateway speaks one language."""
        return ProviderUsage(
            input_tokens=int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0),
            output_tokens=int(raw.get("completion_tokens") or raw.get("output_tokens") or 0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()