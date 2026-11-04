"""End-to-end through the ASGI app: auth, routing, fallback, metering."""

from __future__ import annotations

from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select

from aigate.db import get_session_factory
from aigate.models import RequestTrace, UsageRecord


async def _usage_rows() -> list[UsageRecord]:
    async with get_session_factory()() as session:
        return list((await session.execute(select(UsageRecord))).scalars().all())


async def test_health_is_public(client: AsyncClient):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_missing_key_is_401(client: AsyncClient, chat_payload: dict):
    r = await client.post("/v1/chat/completions", json=chat_payload)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "gateway.missing_credentials"


async def test_bad_key_is_401(client: AsyncClient, chat_payload: dict):
    r = await client.post(
        "/v1/chat/completions",
        json=chat_payload,
        headers={"Authorization": "Bearer sk-ag-does-not-exist"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "gateway.invalid_key"


async def test_chat_success_returns_meta_and_meters(
    client: AsyncClient, auth_headers: dict, chat_payload: dict
):
    r = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert r.status_code == 200, r.text

    body = r.json()
    meta = body["x_aigate"]
    assert meta["provider"] == "mock"
    assert meta["degraded"] is False
    assert meta["priced"] is True
    assert meta["cost_cny"] > 0
    assert body["usage"]["total_tokens"] > 0
    assert r.headers["x-request-id"] == meta["request_id"]

    rows = await _usage_rows()
    assert len(rows) == 1
    assert rows[0].request_id == meta["request_id"]
    assert rows[0].cost_cny > Decimal("0")


async def test_usage_record_matches_reported_cost(
    client: AsyncClient, auth_headers: dict, chat_payload: dict
):
    r = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    reported = Decimal(str(r.json()["x_aigate"]["cost_cny"]))
    rows = await _usage_rows()
    assert rows[0].cost_cny == reported


async def test_trace_written_and_pii_masked(client: AsyncClient, auth_headers: dict):
    payload = {
        "model": "chat-default",
        "messages": [{"role": "user", "content": "我的手机号是13812345678，帮我登记"}],
    }
    r = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
    assert r.status_code == 200

    async with get_session_factory()() as session:
        trace = (await session.execute(select(RequestTrace))).scalars().first()
    assert trace is not None
    assert "13812345678" not in trace.prompt_preview
    assert "138****5678" in trace.prompt_preview
    assert len(trace.prompt_sha256) == 64


async def test_model_scope_is_enforced(
    client: AsyncClient, restricted_headers: dict
):
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "failover-demo", "messages": [{"role": "user", "content": "hi"}]},
        headers=restricted_headers,
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "gateway.model_not_allowed"


async def test_retryable_error_falls_back(client: AsyncClient, auth_headers: dict):
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "failover-demo", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    meta = r.json()["x_aigate"]
    assert meta["degraded"] is True
    assert meta["model_used"] == "mock-ok"

    rows = await _usage_rows()
    assert rows[0].status == "fallback"
    assert rows[0].degraded is True


async def test_non_retryable_error_does_not_fall_back(client: AsyncClient, auth_headers: dict):
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "bad-request", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_headers,
    )
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_4xx"
    # the fallback candidate must NOT have been called
    rows = await _usage_rows()
    assert rows[0].status == "error"


async def test_list_models_respects_scope(client: AsyncClient, restricted_headers: dict):
    r = await client.get("/v1/models", headers=restricted_headers)
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    assert ids == ["chat-default"]