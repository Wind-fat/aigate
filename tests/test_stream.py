"""SSE passthrough: framing, metadata placement, persistence, and the two
failure modes (before first token = recoverable, after = not)."""

from __future__ import annotations

from sqlalchemy import select

from aigate.db import get_session_factory
from aigate.models import RequestTrace, RoutePolicy, UsageRecord

STREAM_PAYLOAD = {
    "model": "chat-stream",
    "stream": True,
    "messages": [{"role": "user", "content": "hello streaming world"}],
    "temperature": 0,
}


async def _add_policy(match_model: str, candidates: list, fallback: list | None = None) -> None:
    async with get_session_factory()() as session:
        session.add(
            RoutePolicy(
                name=match_model,
                match_model=match_model,
                candidates=candidates,
                fallback=fallback or [],
            )
        )
        await session.commit()


async def _stream(client, headers, payload) -> tuple[object, str]:
    async with client.stream("POST", "/v1/chat/completions", json=payload, headers=headers) as resp:
        body = "".join([chunk async for chunk in resp.aiter_text()])
        return resp, body


async def test_stream_emits_openai_chunks_and_meta(client, auth_headers):
    resp, body = await _stream(client, auth_headers, STREAM_PAYLOAD)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-request-id"].startswith("req_")
    assert resp.headers["x-accel-buffering"] == "no"

    assert "chat.completion.chunk" in body
    assert body.count("data: [DONE]") == 1
    assert ": aigate" in body
    assert '"ttft_ms"' in body
    assert '"degraded": false' in body


async def test_stream_persists_usage_and_ttft(client, auth_headers):
    await _stream(client, auth_headers, STREAM_PAYLOAD)

    async with get_session_factory()() as session:
        rows = (await session.execute(select(UsageRecord))).scalars().all()

    assert len(rows) == 1
    assert rows[0].status == "ok"
    assert rows[0].ttft_ms is not None
    assert rows[0].output_tokens > 0
    assert rows[0].cost_cny > 0


async def test_stream_usage_row_matches_the_usage_chunk_the_client_saw(
    client, auth_headers, monkeypatch
):
    """流式记账必须是上游报的数，不能是估算。

    真实 DashScope 上抓到的偏差：响应里的 usage chunk 是 34/82，`usage_records` 却记成
    16/141（+41 tokens）。原因是记账路径重新估算了一遍 token，而不是用同一份 usage。
    """
    import httpx

    from aigate.api import v1_chat
    from aigate.providers.openai_compat import OpenAICompatibleProvider

    sse = (
        'data: {"choices":[{"index":0,"delta":{"content":"好"},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":34,"completion_tokens":82,'
        '"total_tokens":116}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})

    provider = OpenAICompatibleProvider(
        "fake-vendor",
        "http://127.0.0.1:9/v1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async def _resolve(provider_name, session, settings):
        return provider

    monkeypatch.setattr(v1_chat, "_provider_for", _resolve)

    resp, body = await _stream(
        client, auth_headers, {**STREAM_PAYLOAD, "stream_options": {"include_usage": True}}
    )
    assert resp.status_code == 200, body
    assert '"prompt_tokens": 34' in body and '"completion_tokens": 82' in body

    async with get_session_factory()() as session:
        row = (await session.execute(select(UsageRecord))).scalars().one()
    assert (row.input_tokens, row.output_tokens) == (34, 82), (
        f"记账行与客户端的 usage chunk 不一致：{(row.input_tokens, row.output_tokens)}"
    )


async def test_stream_falls_back_when_failure_happens_before_first_token(client, auth_headers):
    # primary candidate raises 5xx before emitting anything -> silent recovery
    payload = {**STREAM_PAYLOAD, "model": "stream-fail"}
    resp, body = await _stream(client, auth_headers, payload)

    assert resp.status_code == 200
    assert '"degraded": true' in body
    assert "data: [DONE]" in body

    async with get_session_factory()() as session:
        trace = (await session.execute(select(RequestTrace))).scalars().first()
    assert trace.route_path[0]["error"] == "upstream_5xx"
    assert trace.route_path[1]["ok"] is True


async def test_stream_interruption_after_first_token_is_reported_not_hidden(client, auth_headers):
    await _add_policy("stream-interrupt", [{"provider": "mock", "model": "streamfail-x"}])
    payload = {**STREAM_PAYLOAD, "model": "stream-interrupt"}
    resp, body = await _stream(client, auth_headers, payload)

    assert resp.status_code == 200
    assert "gateway.stream_interrupted" in body
    assert "data: [DONE]" in body

    async with get_session_factory()() as session:
        rows = (await session.execute(select(UsageRecord))).scalars().all()
    assert rows[0].status == "error"
    assert rows[0].error_code == "gateway.stream_interrupted"
