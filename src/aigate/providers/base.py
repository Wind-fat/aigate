"""Provider abstraction.

Every vendor difference that matters to the gateway lives here:
  * request shape        -> callers always pass the internal IR
  * usage field names    -> normalised to ProviderUsage
  * error semantics      -> normalised to ProviderError(code, retryable)

`retryable` is the only thing the router reads to decide fallback, which is why
4xx must never be marked retryable: retrying a bad request just doubles the bill.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ChatResult:
    content: str
    model: str
    usage: ProviderUsage
    raw: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    ttft_ms: int | None = None
    # The vendor's own verdict on why generation ended. `None` means "the vendor did not
    # say", which the router must treat differently from "the vendor said stop".
    finish_reason: str | None = None


@dataclass
class StreamEvent:
    """One unit of a streaming response.

    `delta` carries text; `done` closes the stream carrying the vendor's verdict
    (`finish_reason`) and its token counts when it reported any; `error` carries a
    normalised code and ends the stream.

    `done` is optional on purpose: a provider that cannot observe an ending simply omits
    it, and the router falls back to counting tokens rather than inventing a verdict.
    """

    type: str  # "delta" | "done" | "error"
    text: str = ""
    code: str = ""
    message: str = ""
    retryable: bool = False
    finish_reason: str | None = None
    usage: ProviderUsage | None = None


class ProviderError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.upstream_status = upstream_status

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.code}: {self.message}"


class BaseProvider(ABC):
    name: str = "base"
    supports_stream: bool = False

    @abstractmethod
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
        """Non-streaming chat completion.

        The signature *is* the passthrough contract: a request field that is not a
        parameter here can never reach an upstream body. `schemas.SUPPORTED_PARAMS`
        and this signature must therefore stay in step — adding a whitelist entry
        without adding it here is exactly the old "accepted, then dropped" bug.
        """

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
    ) -> AsyncIterator[StreamEvent]:  # type: ignore[override]
        """Streaming chat completion. Subclasses override with an async generator."""
        raise ProviderError("gateway.stream_unsupported", f"{self.name} has no streaming support")
        yield  # pragma: no cover - makes this an async generator for typing

    async def aclose(self) -> None:  # pragma: no cover - overridden where needed
        return None
