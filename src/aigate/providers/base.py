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

    @abstractmethod
    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """Non-streaming chat completion. Streaming is added in Stage 2."""

    async def aclose(self) -> None:  # pragma: no cover - overridden where needed
        return None