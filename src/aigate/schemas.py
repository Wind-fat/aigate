"""Wire schemas. Request side is OpenAI-shaped on purpose (drop-in compatibility).

**The request body is a whitelist, not a filter.** `extra="allow"` is only there so we can
inspect unknown keys ourselves and answer 400 `gateway.unsupported_parameter` with the full
list of what we do support; a Pydantic `extra="forbid"` would answer 422 in a different
error shape and no caller could tell which field was the problem.

`SUPPORTED_PARAMS` is therefore the contract: every name in it is read by
`api/v1_chat.py` and reaches the upstream payload (or is acted on locally). Every name
outside it is refused at the door. Anything declared but not forwarded — the old
`top_p`/`stop`/`tools` situation — is worse than a 400, because the caller cannot tell
"the gateway did it" from "the gateway dropped it".
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# 真正生效的请求字段。新增一个字段必须同时：① 在这里登记；② 在 v1_chat 传给 provider
# （或由网关本地兑现）；③ 在 providers/openai_compat._build_payload 里进上游 body
# （本地兑现的字段除外）；④ 有断言上游收到它 / 客户端看到效果的测试。
SUPPORTED_PARAMS: tuple[str, ...] = (
    "model",
    "messages",
    "temperature",
    "top_p",
    "max_tokens",
    "stop",
    "stream",
    "stream_options",
    "user",
)


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
    # Acted on *locally*, not forwarded: the gateway always asks the vendor for usage
    # (`stream_options.include_usage` is baked into the upstream payload for metering),
    # and this flag decides whether that usage is also handed to the *client* as an
    # OpenAI-shaped final chunk. Kept as a plain dict so unknown keys get our 400 shape
    # (`v1_chat._reject_unsupported_parameters`) instead of a Pydantic 422.
    stream_options: dict[str, Any] | None = None


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
