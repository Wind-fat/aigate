"""Deterministic offline provider.

Why it exists:
  1. tests never touch the network (fast, no key, no flakiness)
  2. `make up` gives a working demo even before the user has an API key
  3. fault injection — the model name decides the behaviour:
       fail500     -> retryable 5xx before first token  (fallback must kick in)
       timeout     -> retryable timeout
       badrequest  -> non-retryable 4xx (fallback must NOT kick in)
       streamfail  -> failure *after* the first token   (gateway cannot recover)
       toolcall    -> answers with a `tool_calls` result (function-calling demo)
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from aigate.metering import estimate_messages_tokens, estimate_tokens
from aigate.providers.base import (
    BaseProvider,
    ChatResult,
    ProviderError,
    ProviderUsage,
    StreamEvent,
)


def _truncate_to_tokens(text: str, max_tokens: int | None) -> tuple[str, bool]:
    """Cut `text` at `max_tokens` and say whether it was cut.

    A demo provider that ignores `max_tokens` while reporting `finish_reason: "stop"` is
    exactly the lie this gateway is trying to stop telling, so the mock honours it too.
    """
    if max_tokens is None or max_tokens <= 0:
        return ("", True) if max_tokens is not None else (text, False)
    if estimate_tokens(text) <= max_tokens:
        return text, False
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo], True


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content")
            return content if isinstance(content, str) else str(content)
    return ""


def _mock_tool_call(tools: list[dict[str, Any]] | None, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """A deterministic tool call for the `toolcall` demo model.

    Same spirit as `fail500` / `badrequest`: the *model name* picks the behaviour, so the
    whole function-calling path can be exercised offline (and on `make up`'s demo, which
    has no vendor key) without inventing a second fake upstream.
    """
    name = "get_weather"
    for tool in tools or []:
        fn = (tool or {}).get("function") or {}
        if isinstance(fn.get("name"), str) and fn["name"]:
            name = fn["name"]
            break
    args = json.dumps({"city": (_last_user_text(messages) or "Beijing")[:32]}, ensure_ascii=False)
    return {
        "id": "call_mock_0",
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


class MockProvider(BaseProvider):
    name = "mock"
    supports_stream = True

    def __init__(self) -> None:
        # Tests assert "the provider was never called" — that needs a counter.
        self.calls = 0
        # What the gateway actually handed us. Empty dict = no sampling parameter was
        # forwarded, which is what the passthrough tests assert against for a bare request.
        self.last_params: dict[str, Any] = {}

    def _record(self, **params: Any) -> None:
        self.last_params = dict(params)

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
        self.calls += 1
        self._record(
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stop=stop,
            user=user,
            enable_thinking=enable_thinking,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
        )
        started = time.perf_counter()
        await asyncio.sleep(0.01)

        if "fail500" in model:
            raise ProviderError("upstream_5xx", "injected 500", retryable=True, upstream_status=500)
        if "timeout" in model:
            raise ProviderError("upstream_timeout", "injected timeout", retryable=True)
        if "badrequest" in model:
            raise ProviderError("upstream_4xx", "injected 400", retryable=False, upstream_status=400)

        if "toolcall" in model and tools:
            call = _mock_tool_call(tools, messages)
            return ChatResult(
                content="",
                model=model,
                usage=ProviderUsage(
                    input_tokens=estimate_messages_tokens(messages),
                    output_tokens=estimate_tokens(call["function"]["arguments"]),
                ),
                raw={"id": f"mock-{int(started * 1000)}", "provider": self.name},
                latency_ms=int((time.perf_counter() - started) * 1000),
                finish_reason="tool_calls",
                tool_calls=[call],
            )

        content = f"[mock:{model}] {_last_user_text(messages)[:280]}"
        content, truncated = _truncate_to_tokens(content, max_tokens)
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
            finish_reason="length" if truncated else "stop",
        )

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
        """Word-by-word stream.

        `streamfail` fails *after* the first token — the one case an SSE gateway
        genuinely cannot recover from. We demo it on purpose instead of hiding it.
        """
        self.calls += 1
        self._record(
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stop=stop,
            user=user,
            enable_thinking=enable_thinking,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
        )

        if "fail500" in model:
            raise ProviderError("upstream_5xx", "injected 500", retryable=True, upstream_status=500)
        if "timeout" in model:
            raise ProviderError("upstream_timeout", "injected timeout", retryable=True)

        if "toolcall" in model and tools:
            # Fragmented on purpose: this is how vendors actually send it (id+name first,
            # then `arguments` in pieces keyed by index), so the assembly path is exercised
            # by every offline demo instead of only by tests.
            call = _mock_tool_call(tools, messages)
            arguments = call["function"]["arguments"]
            head, tail = arguments[: len(arguments) // 2], arguments[len(arguments) // 2 :]
            for fragment in (
                [
                    {
                        "index": 0,
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["function"]["name"], "arguments": ""},
                    }
                ],
                [{"index": 0, "function": {"arguments": head}}],
                [{"index": 0, "function": {"arguments": tail}}],
            ):
                await asyncio.sleep(0.005)
                yield StreamEvent(type="delta", text="", tool_calls=fragment)
            yield StreamEvent(
                type="done",
                finish_reason="tool_calls",
                usage=ProviderUsage(
                    input_tokens=estimate_messages_tokens(messages),
                    output_tokens=estimate_tokens(arguments),
                ),
            )
            return

        text = f"[mock:{model}] {_last_user_text(messages)[:280]}"
        text, truncated = _truncate_to_tokens(text, max_tokens)
        for idx, piece in enumerate(text.split(" ")):
            await asyncio.sleep(0.005)
            if "streamfail" in model and idx == 1:
                yield StreamEvent(
                    type="error",
                    code="upstream_stream_interrupted",
                    message="injected mid-stream failure",
                )
                return
            yield StreamEvent(type="delta", text=piece if idx == 0 else " " + piece)

        yield StreamEvent(
            type="done",
            finish_reason="length" if truncated else "stop",
            usage=ProviderUsage(
                input_tokens=estimate_messages_tokens(messages),
                output_tokens=estimate_tokens(text),
            ),
        )
