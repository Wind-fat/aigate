"""Wire schemas. Request side is OpenAI-shaped on purpose (drop-in compatibility)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool", "function"]
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = 1.0
    top_p: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    user: str | None = None
    stop: str | list[str] | None = None


class UsageOut(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class GatewayMeta(BaseModel):
    """The `x_aigate` block: makes cost / cache / degradation protocol-level facts."""

    request_id: str
    team: str
    project: str
    provider: str
    model_requested: str
    model_used: str
    degraded: bool = False
    cache_hit: str | None = None
    cost_cny: float = 0.0
    latency_ms: dict[str, int] = Field(default_factory=dict)


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody