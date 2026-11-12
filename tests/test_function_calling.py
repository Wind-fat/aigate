"""Function calling 契约：`tools` 真的进上游 body，`tool_calls` 真的回客户端。

三条不许退化的不变量：

1. **转发**：`tools` / `tool_choice` / `parallel_tool_calls` 与 `tool` 角色消息
   （`tool_call_id`、assistant 的 `tool_calls`）必须出现在**上游收到的 JSON** 里。
   断言的是假上游扣下的 body，不是「我们以为会发什么」。
2. **回传**：上游的 `tool_calls` 原样回去（id/type/function.name/function.arguments），
   `finish_reason` 是 `tool_calls`；流式按 index 分片透传，交给客户端拼装。
3. **不串味**：带 `tools` 的请求既不吃语义缓存的旧答案，也不把自己的
   tool_calls 答案写进缓存（缓存里没有存 `tool_calls` 的位置）。

上游不支持 tools 的情况单独一条：必须是 `upstream_4xx`（厂商 400 被如实上报），
不能退化成网关自己的 `gateway.unsupported_parameter`——那个码的意思是「网关不干」。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from aigate.api import v1_chat
from aigate.providers.openai_compat import OpenAICompatibleProvider
from aigate.schemas import SUPPORTED_PARAMS

WEATHER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查某个城市的当前天气",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}

TOOL_CALLS: list[dict[str, Any]] = [
    {
        "id": "call_9f2c",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city":"杭州"}'},
    }
]

PROMPT = {"role": "user", "content": "杭州现在天气怎么样"}


# --------------------------------------------------------------------------- 假上游


def _completion(tool_calls: list[dict[str, Any]] | None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": "" if tool_calls else "上游答的"}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": "fake-model",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 41, "completion_tokens": 17},
    }


def _sse(*frames: dict[str, Any]) -> str:
    return "".join(f"data: {json.dumps(f, ensure_ascii=False)}\n\n" for f in frames) + "data: [DONE]\n\n"


def _tool_call_sse(*, finish_reason: str | None = "tool_calls", usage: dict[str, int] | None = None) -> str:
    """上游真实的分片方式：首片带 id/name，之后只有 `arguments` 片段，按 index 归属。"""
    frames: list[dict[str, Any]] = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": None}}]},
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_9f2c",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": ""},
                            }
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"city"'}}]}}
            ]
        },
        {
            "choices": [
                {"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": ':"杭州"}'}}]}}
            ]
        },
    ]
    frames.append(
        {
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": finish_reason}
                if finish_reason is not None
                else {"index": 0, "delta": {}}
            ]
        }
    )
    if usage is not None:
        frames.append({"choices": [], "usage": usage})
    return _sse(*frames)


def _fake(
    received: list[dict],
    completion: dict[str, Any] | None = None,
    *,
    sse: str | None = None,
    status: int = 200,
):
    """Return a provider that answers with exactly this body (or this SSE, or this error)."""

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        if status >= 400:
            return httpx.Response(
                status, json={"error": {"code": "invalid_parameter", "message": "tools are not supported"}}
            )
        if sse is not None:
            return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=completion or _completion(None))

    return OpenAICompatibleProvider(
        "fake-vendor",
        "http://127.0.0.1:9/v1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _point_gateway_at(monkeypatch, provider: OpenAICompatibleProvider) -> None:
    async def _resolve(provider_name: str, session: Any, settings: Any) -> Any:
        return provider

    monkeypatch.setattr(v1_chat, "_provider_for", _resolve)


@pytest.fixture
def received() -> list[dict]:
    return []


def _frames(body: str) -> list[dict]:
    out = []
    for line in body.splitlines():
        if line.startswith("data: ") and line[6:].strip() != "[DONE]":
            out.append(json.loads(line[6:]))
    return out


async def _stream(client, headers, payload) -> str:
    async with client.stream("POST", "/v1/chat/completions", json=payload, headers=headers) as resp:
        assert resp.status_code == 200
        return "".join([chunk async for chunk in resp.aiter_text()])


def _assemble(frames: list[dict]) -> list[dict]:
    """The client-side assembly OpenAI documents: merge fragments by `index`."""
    calls: dict[int, dict] = {}
    for frame in frames:
        for choice in frame.get("choices") or []:
            for fragment in (choice.get("delta") or {}).get("tool_calls") or []:
                slot = calls.setdefault(fragment["index"], {"function": {"name": "", "arguments": ""}})
                for key in ("id", "type"):
                    if fragment.get(key):
                        slot[key] = fragment[key]
                for key, value in (fragment.get("function") or {}).items():
                    slot["function"][key] += value
    return [calls[i] for i in sorted(calls)]


# --------------------------------------------------------------------------- ① 请求侧


async def test_tools_reach_the_upstream_body(client, auth_headers, received, monkeypatch):
    """`tools` / `tool_choice` / `parallel_tool_calls` 必须在**上游收到的 JSON** 里。"""
    _point_gateway_at(monkeypatch, _fake(received))

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "chat-default",
            "messages": [PROMPT],
            "temperature": 0,
            "tools": [WEATHER_TOOL],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        },
    )

    assert resp.status_code == 200, resp.text
    body = received[0]
    print("upstream body:", json.dumps(body, ensure_ascii=False))
    assert body["tools"] == [WEATHER_TOOL], "tools 没进上游 body（或被网关改写过）"
    assert body["tool_choice"] == "auto"
    assert body["parallel_tool_calls"] is False


async def test_tool_role_messages_are_forwarded_with_their_ids(client, auth_headers, received, monkeypatch):
    """多轮工具对话靠 `tool_call_id` / assistant 的 `tool_calls` 串联，两个都得过去。"""
    _point_gateway_at(monkeypatch, _fake(received))

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "chat-default",
            "messages": [
                PROMPT,
                {"role": "assistant", "content": "", "tool_calls": TOOL_CALLS},
                {"role": "tool", "tool_call_id": "call_9f2c", "content": "晴，26°C"},
            ],
            "tools": [WEATHER_TOOL],
        },
    )

    assert resp.status_code == 200, resp.text
    messages = received[0]["messages"]
    print("upstream messages:", json.dumps(messages, ensure_ascii=False))
    assert messages[1]["tool_calls"] == TOOL_CALLS
    assert messages[2] == {"role": "tool", "tool_call_id": "call_9f2c", "content": "晴，26°C"}


async def test_tool_choice_typo_is_refused_before_any_spend(client, auth_headers, received, monkeypatch):
    """字符串形态的 `tool_choice` 是 OpenAI 的闭集合；写错就是网关自己的 400。"""
    _point_gateway_at(monkeypatch, _fake(received))

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "chat-default",
            "messages": [PROMPT],
            "tools": [WEATHER_TOOL],
            "tool_choice": "always",
        },
    )

    assert resp.status_code == 400, resp.text
    err = resp.json()["error"]
    assert err["code"] == "gateway.invalid_parameter"
    assert err["parameter"] == "tool_choice"
    assert received == [], "入口就拒了，不许有上游请求"


# --------------------------------------------------------------------------- ② 非流式响应


async def test_non_stream_returns_the_upstream_tool_calls_verbatim(
    client, auth_headers, received, monkeypatch
):
    """id / type / function.name / function.arguments 一个字都不许改。"""
    _point_gateway_at(monkeypatch, _fake(received, _completion(TOOL_CALLS)))

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "chat-default", "messages": [PROMPT], "tools": [WEATHER_TOOL]},
    )

    assert resp.status_code == 200, resp.text
    choice = resp.json()["choices"][0]
    print("client saw:", json.dumps(choice, ensure_ascii=False))
    assert choice["message"]["tool_calls"] == TOOL_CALLS
    assert choice["finish_reason"] == "tool_calls"
    # 老字段还在：客户端同时读 text 的代码不该被吓到
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == ""


# --------------------------------------------------------------------------- ③ 流式响应


async def test_stream_tool_call_deltas_assemble_by_index(client, auth_headers, received, monkeypatch):
    """分片透传：客户端按 index 拼 `arguments`，拼出来必须等于上游那条完整 call。"""
    _point_gateway_at(
        monkeypatch,
        _fake(
            received,
            sse=_tool_call_sse(usage={"prompt_tokens": 41, "completion_tokens": 17}),
        ),
    )

    body = await _stream(
        client,
        auth_headers,
        {
            "model": "chat-default",
            "messages": [PROMPT],
            "stream": True,
            "stream_options": {"include_usage": True},
            "tools": [WEATHER_TOOL],
        },
    )

    frames = _frames(body)
    print("assembled:", json.dumps(_assemble(frames), ensure_ascii=False))
    assert _assemble(frames) == TOOL_CALLS, "拼装结果和上游的 tool_calls 不一致"
    finishes = [
        f["choices"][0]["finish_reason"]
        for f in frames
        if f.get("choices") and f["choices"][0]["finish_reason"]
    ]
    assert finishes == ["tool_calls"]
    # usage 仍然要能带出来（流式计量靠它）
    assert frames[-1]["choices"] == [] and frames[-1]["usage"]["completion_tokens"] == 17
    # 分片是逐帧到达的，不是网关攒成一个整包再发
    fragments = [
        f["choices"][0]["delta"]["tool_calls"]
        for f in frames
        if f.get("choices") and f["choices"][0]["delta"].get("tool_calls")
    ]
    assert len(fragments) == 3, f"分片被合并了：{fragments}"


async def test_stream_derives_finish_reason_tool_calls_when_upstream_says_nothing(
    client, auth_headers, received, monkeypatch
):
    """上游只给了 tool 分片、没给 finish_reason 时，不许报 `stop`（那会让客户端去读空文本）。"""
    _point_gateway_at(monkeypatch, _fake(received, sse=_tool_call_sse(finish_reason=None)))

    body = await _stream(
        client,
        auth_headers,
        {"model": "chat-default", "messages": [PROMPT], "stream": True, "tools": [WEATHER_TOOL]},
    )

    finishes = [f["choices"][0]["finish_reason"] for f in _frames(body) if f.get("choices")]
    print("stream finishes:", finishes)
    assert finishes[-1] == "tool_calls"
    assert _assemble(_frames(body)) == TOOL_CALLS


# --------------------------------------------------------------------------- ④ 不带 tools 时行为不变


async def test_request_without_tools_is_unchanged(client, auth_headers, received, monkeypatch):
    """回归护栏：没有 tools 的请求，body 与响应形状都不许多出东西。"""
    _point_gateway_at(monkeypatch, _fake(received))

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "chat-default", "messages": [PROMPT], "temperature": 0, "max_tokens": 64},
    )

    assert resp.status_code == 200, resp.text
    body = received[0]
    for absent in ("tools", "tool_choice", "parallel_tool_calls"):
        assert absent not in body, f"{absent} 没传却被写进了上游 body：{body}"
    choice = resp.json()["choices"][0]
    assert "tool_calls" not in choice["message"]
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["content"] == "上游答的"


# --------------------------------------------------------------------------- ⑤ 上游不支持 tools


async def test_upstream_rejecting_tools_is_reported_as_upstream_4xx(
    client, auth_headers, received, monkeypatch
):
    """厂商不认 tools 时，错误码必须是 `upstream_4xx`，不是网关自己的 `unsupported_parameter`。"""
    _point_gateway_at(monkeypatch, _fake(received, status=400))

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "chat-default", "messages": [PROMPT], "tools": [WEATHER_TOOL]},
    )

    assert resp.status_code == 502, resp.text
    err = resp.json()["error"]
    print("vendor 400 surfaced as:", json.dumps(err, ensure_ascii=False))
    assert err["code"] == "upstream_4xx"
    assert err["code"] != "gateway.unsupported_parameter"
    # 先证明请求确实带着 tools 发出去了：否则「上游不支持」这句话无从谈起
    assert received[0]["tools"] == [WEATHER_TOOL]


# --------------------------------------------------------------------------- ⑥ 语义缓存不串味


async def test_cache_never_serves_or_stores_a_tools_request(client, auth_headers, received, monkeypatch):
    """带 tools 的请求不许命中旧答案（里面没有 tool_calls），也不许污染缓存。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        received.append(body)
        # 带 tools 时上游回的是 tool_calls：如果网关拿缓存里那条文本答案顶包，下面立刻炸。
        return httpx.Response(200, json=_completion(TOOL_CALLS if body.get("tools") else None))

    _point_gateway_at(
        monkeypatch,
        OpenAICompatibleProvider(
            "fake-vendor",
            "http://127.0.0.1:9/v1",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ),
    )

    payload = {"model": "chat-default", "messages": [PROMPT], "temperature": 0}
    first = await client.post("/v1/chat/completions", headers=auth_headers, json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["x_aigate"]["cache_hit"] is None
    assert len(received) == 1

    # 同一个 prompt，多带 tools：缓存里那条文本答案不能顶上来
    with_tools = {**payload, "tools": [WEATHER_TOOL], "tool_choice": "auto"}
    second = await client.post("/v1/chat/completions", headers=auth_headers, json=with_tools)
    assert second.status_code == 200, second.text
    assert second.json()["x_aigate"]["cache_hit"] is None, "带 tools 的请求吃到了缓存里的旧答案"
    assert len(received) == 2, "带 tools 的请求必须真的打到上游"
    assert received[1]["tools"] == [WEATHER_TOOL]
    assert second.json()["choices"][0]["message"]["tool_calls"] == TOOL_CALLS
    assert second.json()["choices"][0]["finish_reason"] == "tool_calls"

    # 再回到不带 tools 的同一 prompt：原先那条缓存仍然在，且没被工具答案改写
    third = await client.post("/v1/chat/completions", headers=auth_headers, json=payload)
    assert third.status_code == 200, third.text
    assert third.json()["x_aigate"]["cache_hit"] == "L1"
    assert "tool_calls" not in third.json()["choices"][0]["message"]
    assert len(received) == 2, "第三次是缓存命中，不该再打上游"


# --------------------------------------------------------------------------- ⑦ 结构护栏


def test_tools_are_declared_as_supported_parameters():
    """白名单里必须登记：否则入口会 400，这条功能等于没做。"""
    for name in ("tools", "tool_choice", "parallel_tool_calls"):
        assert name in SUPPORTED_PARAMS


async def test_mock_provider_receives_the_tools(client, auth_headers):
    """最后一层：网关 → provider 的调用确实带着 tools（provider 自己记下来）。"""
    from aigate.providers import get_provider

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "chat-default",
            "messages": [PROMPT],
            "temperature": 0,
            "tools": [WEATHER_TOOL],
            "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
        },
    )

    assert resp.status_code == 200, resp.text
    seen = get_provider("mock").last_params
    assert seen["tools"] == [WEATHER_TOOL]
    assert seen["tool_choice"] == {"type": "function", "function": {"name": "get_weather"}}