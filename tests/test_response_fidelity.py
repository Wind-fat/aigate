"""响应侧说真话：`finish_reason` 反映真实结束原因，流式 usage 客户端拿得到。

上一轮只做了请求侧（白名单 + 真透传），响应侧还是两处硬编码：

1. **`finish_reason` 永远是 `"stop"`**：`max_tokens` 截断也报 stop，客户端无法区分
   「模型说完了」和「被切断了」。这里用假上游钉住两种情况——上游给了就透传，
   上游没给就按 token 数推导，**推导是启发式，不是 OpenAI 语义**（README/SPEC 已写明）。
2. **流式没有 OpenAI 形状的 usage**：token 数藏在 `: aigate {...}` SSE 注释里，
   SDK 一律忽略。现在 `stream_options.include_usage: true` 会在 `[DONE]` 前多发一个
   `choices: []` + `usage` 的 chunk，顺序与 OpenAI 一致。

所有断言都打在**客户端实际收到的 JSON**（或 SSE 帧）上，不看内部变量。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from aigate.api import v1_chat
from aigate.db import get_session_factory
from aigate.models import RoutePolicy
from aigate.providers.openai_compat import OpenAICompatibleProvider
from aigate.schemas import SUPPORTED_PARAMS

PROMPT = {"role": "user", "content": "只回答一个字"}


# ---------------------------------------------------------------------------
# 假上游：body 可定制，SSE 用真帧
# ---------------------------------------------------------------------------


def _completion(
    *,
    finish_reason: str | None = "stop",
    completion_tokens: int = 3,
    content: str = "上游答的",
) -> dict[str, Any]:
    choice: dict[str, Any] = {"index": 0, "message": {"role": "assistant", "content": content}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": "mock-ok",
        "choices": [choice],
        "usage": {"prompt_tokens": 7, "completion_tokens": completion_tokens},
    }


def _sse(
    *,
    finish_reason: str | None = "stop",
    usage: dict[str, int] | None = None,
) -> str:
    """OpenAI's own SSE order: deltas -> finish chunk -> usage chunk -> [DONE]."""
    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"上"}}]}\n\n',
        'data: {"choices":[{"index":0,"delta":{"content":"游"},"finish_reason":'
        + json.dumps(finish_reason or "null")
        + "}]}\n\n",
    ]
    if usage is not None:
        lines.append(f"data: {json.dumps({'choices': [], 'usage': usage})}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines)


def _silent_sse(text: str) -> str:
    """An upstream that names no `finish_reason` and reports no usage at all."""
    return f'data: {{"choices":[{{"index":0,"delta":{{"content":"{text}"}}}}]}}\n\ndata: [DONE]\n\n'


def _provider(
    received: list[dict],
    *,
    completion: dict[str, Any] | None = None,
    sse: str | None = None,
) -> OpenAICompatibleProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        if sse is not None:
            return httpx.Response(
                200, content=sse.encode(), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=completion or _completion())

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
    """只取 `data:` 帧（`[DONE]` 与 `: aigate` 注释不算 chunk）。"""
    out = []
    for line in body.splitlines():
        if line.startswith("data: ") and line[6:].strip() != "[DONE]":
            out.append(json.loads(line[6:]))
    return out


async def _stream(client, headers, payload) -> str:
    async with client.stream("POST", "/v1/chat/completions", json=payload, headers=headers) as resp:
        assert resp.status_code == 200
        return "".join([chunk async for chunk in resp.aiter_text()])


# ---------------------------------------------------------------------------
# ① finish_reason：上游给了就透传
# ---------------------------------------------------------------------------


async def test_non_stream_passes_upstream_length_through(
    client, auth_headers, received, monkeypatch
):
    """`max_tokens` 截断：上游说 length，客户端就必须看到 length（以前一律 stop）。"""
    _point_gateway_at(monkeypatch, _provider(received, completion=_completion(finish_reason="length")))

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "chat-default", "messages": [PROMPT], "temperature": 0, "max_tokens": 5},
    )

    assert resp.status_code == 200, resp.text
    print("non-stream:", json.dumps(resp.json()["choices"], ensure_ascii=False))
    assert resp.json()["choices"][0]["finish_reason"] == "length"


async def test_non_stream_passes_content_filter_through(
    client, auth_headers, received, monkeypatch
):
    _point_gateway_at(
        monkeypatch, _provider(received, completion=_completion(finish_reason="content_filter"))
    )

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "chat-default", "messages": [PROMPT], "temperature": 0},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["finish_reason"] == "content_filter"


# ---------------------------------------------------------------------------
# ② finish_reason：上游没给就按 token 数推导（启发式，不是语义）
# ---------------------------------------------------------------------------


async def test_non_stream_derives_length_when_upstream_is_silent_and_budget_is_used_up(
    client, auth_headers, received, monkeypatch
):
    """上游没给 finish_reason，但 completion_tokens 打满 max_tokens → 只能是 length。"""
    _point_gateway_at(
        monkeypatch,
        _provider(
            received, completion=_completion(finish_reason=None, completion_tokens=5, content="上")
        ),
    )

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "chat-default", "messages": [PROMPT], "temperature": 0, "max_tokens": 5},
    )

    assert resp.status_code == 200, resp.text
    print("non-stream (derived):", json.dumps(resp.json()["choices"], ensure_ascii=False))
    assert resp.json()["choices"][0]["finish_reason"] == "length"
    assert resp.json()["usage"]["completion_tokens"] == 5


async def test_non_stream_derives_stop_when_upstream_is_silent_and_budget_is_left(
    client, auth_headers, received, monkeypatch
):
    """没打满 max_tokens 才允许推导成 stop——推导不能比原文更悲观。"""
    _point_gateway_at(
        monkeypatch,
        _provider(
            received, completion=_completion(finish_reason=None, completion_tokens=3, content="上游答的")
        ),
    )

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "chat-default", "messages": [PROMPT], "temperature": 0, "max_tokens": 64},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["finish_reason"] == "stop"


async def test_non_stream_treats_literal_null_string_as_absent(
    client, auth_headers, received, monkeypatch
):
    """有几家厂商把 "null" 当字符串塞进来；那不是「说完了」。"""
    _point_gateway_at(
        monkeypatch,
        _provider(
            received, completion=_completion(finish_reason="null", completion_tokens=5, content="上")
        ),
    )

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "chat-default", "messages": [PROMPT], "temperature": 0, "max_tokens": 5},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["finish_reason"] == "length"


# ---------------------------------------------------------------------------
# ③ 流式 finish_reason
# ---------------------------------------------------------------------------


async def test_stream_passes_upstream_finish_reason_through(client, auth_headers, received, monkeypatch):
    _point_gateway_at(monkeypatch, _provider(received, sse=_sse(finish_reason="length")))

    body = await _stream(
        client,
        auth_headers,
        {"model": "chat-default", "messages": [PROMPT], "temperature": 0, "stream": True},
    )

    finishes = [f["choices"][0]["finish_reason"] for f in _frames(body) if f["choices"]]
    print("stream finish_reasons:", [f for f in finishes if f])
    assert finishes[-1] == "length", body


async def test_stream_derives_finish_reason_when_upstream_sends_none(
    client, auth_headers, received, monkeypatch
):
    """上游帧里没有 finish_reason、也没给 usage → 推导（这里 max_tokens=1，内容 1 token）。"""
    _point_gateway_at(
        monkeypatch,
        _provider(received, sse=_silent_sse("上")),
    )

    body = await _stream(
        client,
        auth_headers,
        {
            "model": "chat-default",
            "messages": [PROMPT],
            "temperature": 0,
            "stream": True,
            "max_tokens": 1,
        },
    )

    finishes = [f["choices"][0]["finish_reason"] for f in _frames(body) if f["choices"]]
    assert finishes[-1] == "length", body


# ---------------------------------------------------------------------------
# ④ 流式 usage：stream_options.include_usage
# ---------------------------------------------------------------------------


async def test_stream_include_usage_emits_openai_shaped_final_chunk(
    client, auth_headers, received, monkeypatch
):
    _point_gateway_at(
        monkeypatch,
        _provider(
            received, sse=_sse(usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})
        ),
    )

    body = await _stream(
        client,
        auth_headers,
        {
            "model": "chat-default",
            "messages": [PROMPT],
            "temperature": 0,
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    )

    frames = _frames(body)
    usage_frames = [f for f in frames if "usage" in f]
    print("last chunk:", json.dumps(frames[-1], ensure_ascii=False))
    assert len(usage_frames) == 1, body
    assert usage_frames[0]["choices"] == []
    assert usage_frames[0]["usage"] == {
        "prompt_tokens": 5,
        "completion_tokens": 2,
        "total_tokens": 7,
    }
    # 顺序：finish chunk 之后、[DONE] 之前
    finish_idx = max(i for i, f in enumerate(frames) if f["choices"] and f["choices"][0]["finish_reason"])
    assert frames.index(usage_frames[0]) > finish_idx
    assert body.rstrip().endswith("data: [DONE]")
    assert body.index('"choices": []') < body.index("data: [DONE]")


async def test_stream_without_stream_options_has_no_usage_chunk(client, auth_headers, received, monkeypatch):
    """不传 stream_options 时行为不变：这是兼容性承诺的一半，必须有测试。"""
    _point_gateway_at(
        monkeypatch,
        _provider(
            received, sse=_sse(usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})
        ),
    )

    body = await _stream(
        client,
        auth_headers,
        {"model": "chat-default", "messages": [PROMPT], "temperature": 0, "stream": True},
    )

    assert not [f for f in _frames(body) if "usage" in f], body
    assert '"choices": []' not in body
    assert ": aigate" in body  # 网关自己的元数据仍在注释里


async def test_stream_usage_falls_back_to_estimates_and_says_so(client, auth_headers, received, monkeypatch):
    """上游不给 usage 时不能编造「上游口径」，注释里要写明 estimated。"""
    _point_gateway_at(
        monkeypatch,
        _provider(received, sse=_silent_sse("上游")),
    )

    body = await _stream(
        client,
        auth_headers,
        {
            "model": "chat-default",
            "messages": [PROMPT],
            "temperature": 0,
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    )

    usage = [f for f in _frames(body) if "usage" in f][0]["usage"]
    print("estimated usage:", usage)
    assert usage["completion_tokens"] > 0
    assert '"usage_source": "estimated"' in body


# ---------------------------------------------------------------------------
# ⑤ stream_options 的入口契约（实现方式的另一面：传错必须 400，不是静默）
# ---------------------------------------------------------------------------


async def test_stream_options_is_in_the_whitelist_and_the_schema():
    assert "stream_options" in SUPPORTED_PARAMS


async def test_unknown_key_inside_stream_options_is_refused(client, auth_headers, received, monkeypatch):
    _point_gateway_at(monkeypatch, _provider(received, sse=_sse()))

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "chat-default",
            "messages": [PROMPT],
            "stream": True,
            "stream_options": {"include_usage": True, "chunk_size": 10},
        },
    )

    assert resp.status_code == 400, resp.text
    err = resp.json()["error"]
    print("refused:", json.dumps(err, ensure_ascii=False))
    assert err["code"] == "gateway.unsupported_parameter"
    assert err["unsupported_parameters"] == ["stream_options.chunk_size"]
    assert received == [], "入口就该拒，不能打上游"


async def test_non_boolean_include_usage_is_refused(client, auth_headers, received, monkeypatch):
    """`{"include_usage": "yes"}` 静默当 false 是另一种撒谎。"""
    _point_gateway_at(monkeypatch, _provider(received, sse=_sse()))

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "chat-default",
            "messages": [PROMPT],
            "stream": True,
            "stream_options": {"include_usage": "yes"},
        },
    )

    assert resp.status_code == 400, resp.text
    err = resp.json()["error"]
    assert err["code"] == "gateway.invalid_parameter"
    assert err["parameter"] == "stream_options.include_usage"
    assert received == []


async def test_stream_options_without_stream_is_accepted_and_ignored(
    client, auth_headers, received, monkeypatch
):
    """非流式本来就有 usage；OpenAI 也不报错，这里同样收下不炸。"""
    _point_gateway_at(monkeypatch, _provider(received))

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "chat-default",
            "messages": [PROMPT],
            "temperature": 0,
            "stream_options": {"include_usage": True},
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["usage"]["total_tokens"] > 0


# ---------------------------------------------------------------------------
# ⑥ 端到端（mock provider，不经假上游）：截断要能被客户端看见
# ---------------------------------------------------------------------------


async def test_mock_provider_honours_max_tokens_in_finish_reason(client, auth_headers):
    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "chat-default", "messages": [PROMPT], "temperature": 0, "max_tokens": 2},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    print("mock truncated:", json.dumps(body["choices"], ensure_ascii=False))
    assert body["choices"][0]["finish_reason"] == "length"
    assert body["usage"]["completion_tokens"] <= 2


async def test_stream_interruption_does_not_fabricate_a_usage_chunk(client, auth_headers):
    """中途挂掉时不能补一个「正常结束」的 usage chunk。"""
    async with get_session_factory()() as session:
        session.add(
            RoutePolicy(
                name="stream-interrupt-usage",
                match_model="stream-interrupt-usage",
                candidates=[{"provider": "mock", "model": "streamfail-x"}],
                fallback=[],
            )
        )
        await session.commit()

    body = await _stream(
        client,
        auth_headers,
        {
            "model": "stream-interrupt-usage",
            "messages": [PROMPT],
            "temperature": 0,
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    )

    assert "gateway.stream_interrupted" in body
    assert not [f for f in _frames(body) if "usage" in f], body
    assert body.rstrip().endswith("data: [DONE]")