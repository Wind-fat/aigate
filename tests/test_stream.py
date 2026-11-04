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
