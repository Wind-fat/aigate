"""参数透传契约：能转发的必须**真的**出现在上游 body 里；不能转发的不许静默丢。

Q3 审计抓到的原始问题：`schemas.py` 声明了 `top_p` / `stop`，被 Pydantic 收下，然后
provider payload 只发 4 个字段——调用方拿到 200，以为参数生效了。`tools` 更糟：靠
`extra="allow"` 收下，只参与缓存准入，从不进上游。

这里用两种证据钉住它，都不依赖「读代码」：

1. **假上游**：`httpx.MockTransport` 拦住真实出站请求，把 body 原样扣下来。断言的是
   「上游收到的 JSON」，不是「我们以为会发什么」。
2. **入口拒绝**：确实没有转发路径的字段（`response_format`、`logprobs` 等），必须在
   花掉任何配额前面 400 `gateway.unsupported_parameter`，并在错误体里列出支持的参数。
   `tools` / `tool_choice` 曾经也在这张拒绝清单上；它们现在有转发路径了，于是变成
   第 ① 类（真转发），细节点见 `tests/test_function_calling.py`。

`mock` provider 只记录参数（`last_params`），用来覆盖「网关把参数交给 provider」这一层；
`_build_payload` 才是最终 body 的那一层，两层都断言，中间断链能被抓到。
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import httpx
import pytest

from aigate.api import v1_chat
from aigate.providers.base import BaseProvider
from aigate.providers.openai_compat import OpenAICompatibleProvider
from aigate.schemas import SUPPORTED_PARAMS, ChatCompletionRequest

COMPLETION = {
    "id": "chatcmpl-fake",
    "object": "chat.completion",
    "model": "mock-ok",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "上游答的"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
}

SSE = (
    'data: {"choices":[{"index":0,"delta":{"content":"上"}}]}\n\n'
    'data: {"choices":[{"index":0,"delta":{"content":"游"},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
    "data: [DONE]\n\n"
)


def _fake_upstream(received: list[dict], *, streaming: bool = False) -> httpx.AsyncClient:
    """A real HTTP client whose transport records every request body and answers locally."""

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        if streaming:
            return httpx.Response(
                200, content=SSE.encode(), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=COMPLETION)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _provider(received: list[dict], *, streaming: bool = False) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        "fake-vendor", "http://127.0.0.1:9/v1", client=_fake_upstream(received, streaming=streaming)
    )


def _point_gateway_at(monkeypatch, provider: OpenAICompatibleProvider) -> None:
    """Route the gateway's provider resolution at the fake upstream.

    Patched at `_provider_for`, i.e. the one seam between "which provider did the router
    pick" and "what did it send" — everything upstream of it (auth, quota, masking,
    payload construction, the call itself) is the real code.
    """

    async def _resolve(provider_name: str, session: Any, settings: Any) -> Any:
        return provider

    monkeypatch.setattr(v1_chat, "_provider_for", _resolve)


@pytest.fixture
def received() -> list[dict]:
    return []


# ---------------------------------------------------------------------------
# ① 转发：假上游收到的 JSON 里确实有这些字段
# ---------------------------------------------------------------------------


async def test_top_p_and_stop_reach_the_upstream_request_body(
    client, auth_headers, received, monkeypatch
):
    _point_gateway_at(monkeypatch, _provider(received))

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "chat-default",
            "messages": [{"role": "user", "content": "只回答一个字"}],
            "temperature": 0,
            "top_p": 0.35,
            "stop": ["\n\n", "###"],
            "enable_thinking": False,
        },
    )

    assert resp.status_code == 200, resp.text
    assert len(received) == 1, "上游必须收到恰好一次请求"
    body = received[0]
    # `pytest -s` 时把上游真实收到的 body 打出来：这就是这条用例的证据本体。
    print("upstream body:", json.dumps(body, ensure_ascii=False))
    assert body["top_p"] == 0.35, f"top_p 没进上游 body：{body}"
    assert body["stop"] == ["\n\n", "###"], f"stop 没进上游 body：{body}"
    # 供应商专有字段同样要原样出现在上游 body 里：`enable_thinking` 只有 DashScope 认，
    # 但网关的职责是转发而不是替厂商挑选字段（见上一条注释）。
    assert body["enable_thinking"] is False, f"enable_thinking 没进上游 body：{body}"
    assert body["temperature"] == 0
    assert body["stream"] is False
    # 网关的响应形状没被参数透传改坏
    assert resp.json()["choices"][0]["message"]["content"] == "上游答的"


async def test_stop_as_a_plain_string_is_forwarded_verbatim(client, auth_headers, received, monkeypatch):
    """OpenAI 允许 `stop` 是字符串或数组；两种都得原样发过去，不能自己归一化。"""
    _point_gateway_at(monkeypatch, _provider(received))

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "chat-default",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0,
            "stop": "END",
        },
    )

    assert resp.status_code == 200, resp.text
    assert received[0]["stop"] == "END"


async def test_user_field_is_forwarded_not_dropped(client, auth_headers, received, monkeypatch):
    """`user` 以前也是「声明了但没人用」的一员。"""
    _point_gateway_at(monkeypatch, _provider(received))

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "chat-default",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0,
            "user": "end-user-42",
        },
    )

    assert resp.status_code == 200, resp.text
    assert received[0]["user"] == "end-user-42"


async def test_streaming_forwards_top_p_and_stop_too(client, auth_headers, received, monkeypatch):
    """流式与非流式有一份独立的 payload 构造——曾经就是这样漂移的。"""
    _point_gateway_at(monkeypatch, _provider(received, streaming=True))

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "chat-default",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0,
            "stream": True,
            "top_p": 0.5,
            "stop": ["\n"],
            "enable_thinking": False,
        },
    )

    assert resp.status_code == 200, resp.text
    body = received[0]
    assert body["stream"] is True
    assert body["top_p"] == 0.5
    assert body["stop"] == ["\n"]
    assert body["enable_thinking"] is False
    # 计量要 token 数：这个字段是网关自己的需求，不属于调用方参数
    assert body["stream_options"] == {"include_usage": True}


# ---------------------------------------------------------------------------
# ③ 不传时不多出 null 字段
# ---------------------------------------------------------------------------


async def test_absent_parameters_do_not_appear_as_null_fields(
    client, auth_headers, received, monkeypatch
):
    """`"top_p": null` 在部分厂商是 400；没传就不该出现这个键。"""
    _point_gateway_at(monkeypatch, _provider(received))

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "chat-default",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0,
        },
    )

    assert resp.status_code == 200, resp.text
    body = received[0]
    for absent in ("top_p", "stop", "user", "max_tokens", "enable_thinking"):
        assert absent not in body, f"{absent} 没传却被写进了上游 body：{body}"
    assert all(value is not None for value in body.values()), f"body 里有 null：{body}"
    assert set(body) == {"model", "messages", "stream", "temperature"}


# ---------------------------------------------------------------------------
# ② 不支持的参数：400 + 错误码 + 支持清单，而且一分钱没花
# ---------------------------------------------------------------------------


async def test_tools_are_accepted_and_listed_as_supported(client, auth_headers, received, monkeypatch):
    """2026-11 起 `tools` 有了转发路径，入口不再拒它。

    这条用例以前断言的是「400 + unsupported_parameters == ["tool_choice","tools"]」——
    那是当时的真相（收下就丢，不如拒掉）。现在两者都真转发，所以这里只钉住
    「不再被拒」「出现在 supported 清单里」；转发与响应侧结构见
    `tests/test_function_calling.py`。
    """
    _point_gateway_at(monkeypatch, _provider(received))

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "chat-default",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0,
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "get_weather", "parameters": {"type": "object"}},
                }
            ],
            "tool_choice": "auto",
        },
    )

    assert resp.status_code == 200, resp.text
    assert {"tools", "tool_choice"} <= set(SUPPORTED_PARAMS)
    assert len(received) == 1
    assert received[0]["tool_choice"] == "auto"


async def test_unknown_parameter_is_refused_instead_of_ignored(
    client, auth_headers, received, monkeypatch
):
    """白名单的意义在「没登记的新字段」上：它必须是 400，不能是静默无效。"""
    _point_gateway_at(monkeypatch, _provider(received))

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "chat-default",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_object"},
        },
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["unsupported_parameters"] == ["response_format"]
    assert received == []


async def test_refusal_costs_nothing_in_metering(client, auth_headers, received, monkeypatch):
    """拒绝必须发生在配额与记账之前：否则「参数写错」还会被计一次费。"""
    from sqlalchemy import func, select

    from aigate.db import get_session_factory
    from aigate.models import UsageRecord

    _point_gateway_at(monkeypatch, _provider(received))
    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "chat-default",
            "messages": [{"role": "user", "content": "hi"}],
            "logprobs": True,
        },
    )

    assert resp.status_code == 400, resp.text
    async with get_session_factory()() as session:
        rows = (await session.execute(select(func.count()).select_from(UsageRecord))).scalar_one()
    assert rows == 0, "被 400 拒掉的请求不该产生 usage_records 行"


async def test_out_of_range_top_p_gets_a_stable_code(client, auth_headers, received, monkeypatch):
    """越界的 top_p 是调用方 bug；给出网关自己的错误码，而不是厂商的 upstream_4xx。"""
    _point_gateway_at(monkeypatch, _provider(received))

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "chat-default",
            "messages": [{"role": "user", "content": "hi"}],
            "top_p": 1.5,
        },
    )

    assert resp.status_code == 400, resp.text
    err = resp.json()["error"]
    assert err["code"] == "gateway.invalid_parameter"
    assert err["parameter"] == "top_p"
    assert received == []


async def test_vendor_rejecting_the_vendor_specific_field_is_reported_not_hidden(
    client, auth_headers, received, monkeypatch
):
    """非 DashScope 上游可能不认 `enable_thinking`。

    契约：仍然发出去，厂商 400 就以 `upstream_4xx` 原样返回（502 + 错误码），
    而不是「网关偷偷去掉这个字段再返回 200」——后者会让调用方以为开关生效了。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(
            400,
            json={"error": {"code": "unknown_parameter", "message": "enable_thinking"}},
        )

    stubborn = OpenAICompatibleProvider(
        "stubborn-vendor",
        "http://127.0.0.1:9/v1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    _point_gateway_at(monkeypatch, stubborn)

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "chat-default",
            "messages": [{"role": "user", "content": "hi"}],
            "enable_thinking": False,
        },
    )

    assert resp.status_code == 502, resp.text
    err = resp.json()["error"]
    print("vendor 400 surfaced as:", json.dumps(err, ensure_ascii=False))
    assert err["code"] == "upstream_4xx", err
    # 证据：被拒之前它确实被发出去了（没有静默丢弃）
    assert received[0]["enable_thinking"] is False


# ---------------------------------------------------------------------------
# 结构护栏：白名单、schema、provider 签名三者不许各说各话
# ---------------------------------------------------------------------------


def test_declared_request_fields_are_exactly_the_supported_ones():
    """声明一个字段却不登记 = 又回到「收下就丢」。两边必须逐字相等。"""
    declared = set(ChatCompletionRequest.model_fields)
    assert declared == set(SUPPORTED_PARAMS)


def test_provider_signature_can_carry_every_forwarded_field():
    """v1_chat 传得出去、provider 收不下的字段，会变成 TypeError 或再次静默丢。"""
    forwarded = (
        "temperature",
        "top_p",
        "max_tokens",
        "stop",
        "user",
        "enable_thinking",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
    )
    params = inspect.signature(BaseProvider.chat).parameters
    for field in forwarded:
        assert field in params, f"BaseProvider.chat 缺 {field}：这个字段到不了上游"
    stream_params = inspect.signature(BaseProvider.stream).parameters
    for field in forwarded:
        assert field in stream_params, f"BaseProvider.stream 缺 {field}：流式路径会丢参数"


async def test_mock_provider_receives_the_parameters_from_the_gateway(client, auth_headers):
    """最后一层证据：网关 → provider 的调用确实带了参数（provider 自己记下来）。"""
    from aigate.providers import get_provider

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "chat-default",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0,
            "top_p": 0.7,
            "stop": ["\n\n"],
        },
    )

    assert resp.status_code == 200, resp.text
    seen = get_provider("mock").last_params
    assert seen["top_p"] == 0.7
    assert seen["stop"] == ["\n\n"]
    # 没传就是 None：网关不替调用方决定推理开关
    assert seen["enable_thinking"] is None
    assert seen["temperature"] == 0