"""Rate limiting and budget guard — the two ways a request may be rejected before
it ever reaches a provider. Both paths assert the same thing: no upstream call."""

from __future__ import annotations

from sqlalchemy import select, update

from aigate.core.quota import InMemoryTokenBucket
from aigate.db import get_session_factory
from aigate.models import Project, UsageRecord

CHAT = {"model": "chat-default", "messages": [{"role": "user", "content": "hi"}], "temperature": 0}


async def _set_project(**values):
    async with get_session_factory()() as session:
        await session.execute(update(Project).where(Project.slug == "test-project").values(**values))
        await session.commit()


async def _usage_count() -> int:
    async with get_session_factory()() as session:
        rows = (await session.execute(select(UsageRecord))).scalars().all()
        return len(rows)


# --------------------------------------------------------------------------- unit


async def test_bucket_allows_exactly_limit_then_denies():
    bucket = InMemoryTokenBucket()
    for _ in range(3):
        assert (await bucket.hit("k", 3, 60)).allowed

    denied = await bucket.hit("k", 3, 60)
    assert denied.allowed is False
    assert denied.remaining == 0
    assert denied.retry_after_s >= 1
    assert denied.backend == "memory"


async def test_bucket_keys_are_independent():
    bucket = InMemoryTokenBucket()
    for _ in range(3):
        await bucket.hit("a", 3, 60)
    assert (await bucket.hit("b", 3, 60)).allowed


async def test_bucket_refills_over_time():
    import asyncio

    bucket = InMemoryTokenBucket()
    assert (await bucket.hit("k", 1, 0.1)).allowed
    assert not (await bucket.hit("k", 1, 0.1)).allowed
    await asyncio.sleep(0.12)
    assert (await bucket.hit("k", 1, 0.1)).allowed, "token should refill after the window"


# --------------------------------------------------------------------- endpoint


async def test_rate_limit_returns_429_and_never_calls_upstream(client, auth_headers, mock_provider):
    await _set_project(rpm_limit=1)
    payload = dict(CHAT)
    first = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
    assert first.status_code == 200

    calls_before = mock_provider.calls
    second = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "gateway.quota_exceeded"
    assert int(second.headers["Retry-After"]) >= 1

    assert mock_provider.calls == calls_before, "429 must not touch the provider"
    assert await _usage_count() == 1


async def test_budget_exhausted_returns_429(client, auth_headers, mock_provider):
    await _set_project(monthly_budget_cny=0)
    calls_before = mock_provider.calls

    resp = await client.post("/v1/chat/completions", json=CHAT, headers=auth_headers)
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "gateway.budget_exhausted"
    assert mock_provider.calls == calls_before


async def test_unlimited_budget_allows_traffic(client, auth_headers):
    await _set_project(monthly_budget_cny=None)
    resp = await client.post("/v1/chat/completions", json=CHAT, headers=auth_headers)
    assert resp.status_code == 200
