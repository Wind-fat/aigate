"""Deterministic offline provider.

Why it exists:
  1. tests never touch the network (fast, no key, no flakiness)
  2. `make up` gives a working demo even before the user has an API key
  3. fault injection — `model` containing "fail" or "slow" simulates upstream errors,
     which is how the fallback path is demonstrable without unplugging anything
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from aigate.metering import estimate_messages_tokens, estimate_tokens
from aigate.providers.base import BaseProvider, ChatResult, ProviderError, ProviderUsage


class MockProvider(BaseProvider):
    name = "mock"

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        started = time.perf_counter()
        await asyncio.sleep(0.01)

        if "fail500" in model:
            raise ProviderError("upstream_5xx", "injected 500", retryable=True, upstream_status=500)
        if "timeout" in model:
            raise ProviderError("upstream_timeout", "injected timeout", retryable=True)
        if "badrequest" in model:
            raise ProviderError("upstream_4xx", "injected 400", retryable=False, upstream_status=400)

        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content")
                last_user = content if isinstance(content, str) else str(content)
                break

        content = f"[mock:{model}] {last_user[:280]}"
        usage = ProviderUsage(
            input_tokens=estimate_messages_tokens(messages),
            output_tokens=estimate_tokens(content),
        )
        return ChatResult(
            content=content,
            model=model,
            usage=usage,
            raw={"id": f"mock-{int(started * 1000)}", "provider": self.name},
            latency_ms=int((time.perf_counter() - started) * 1000),
        )