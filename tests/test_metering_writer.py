"""Batched metering writer: does it actually batch, does it actually persist,
and does it keep the billing invariant when there is no worker."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from aigate.db import get_session_factory
from aigate.metering_queue import UsageEvent, get_writer, submit
from aigate.models import ApiKey, Project, RequestTrace, UsageRecord


def _event(idx: int, project_id: str, key_id: str, team_id: str) -> UsageEvent:
    return UsageEvent(
        request_id=f"req_batch_{idx:04d}",
        team_id=team_id,
        project_id=project_id,
        api_key_id=key_id,
        provider="mock",
        model_requested="chat-default",
        model_used="mock-ok",
        input_tokens=10 + idx,
        output_tokens=20 + idx,
        cost_cny=Decimal("0.000123"),
        latency_ms=12,
        ttft_ms=None,
        status="ok",
        error_code=None,
        cache_hit=None,
        degraded=False,
        prompt_sha256="a" * 64,
        prompt_preview=f"prompt {idx}",
        pii_masked=0,
        http_status=200,
        stage_timings={"upstream_ms": 10},
        route_path=[{"attempt": 1, "ok": True}],
    )


async def _ids() -> tuple[str, str, str]:
    async with get_session_factory()() as session:
        key = (await session.execute(select(ApiKey).limit(1))).scalar_one()
        project = (await session.execute(select(Project).limit(1))).scalar_one()
        # read plain columns only: the session is about to close, and a lazy load of
        # key.project afterwards would raise DetachedInstanceError
        return project.id, key.id, project.team_id


async def test_started_writer_batches_then_flushes(client):
    writer = get_writer()
    assert writer is not None
    await writer.start()

    project_id, key_id, team_id = await _ids()
    for i in range(30):
        await submit(_event(i, project_id, key_id, team_id))

    await writer.flush()

    async with get_session_factory()() as session:
        usage = (await session.execute(select(UsageRecord))).scalars().all()
        traces = (await session.execute(select(RequestTrace))).scalars().all()

    assert len(usage) == 30
    assert len(traces) == 30, "trace rows must be batched alongside the bill"

    stats = writer.stats()
    assert stats["written"] == 30
    assert stats["batches"] < 30, f"expected batching, got one commit per event: {stats}"
    assert stats["inline_writes"] == 0

    await writer.stop()


async def test_writer_preserves_payload_fields(client):
    writer = get_writer()
    await writer.start()
    project_id, key_id, team_id = await _ids()

    await submit(_event(7, project_id, key_id, team_id))
    await writer.flush()

    async with get_session_factory()() as session:
        row = (
            await session.execute(select(UsageRecord).where(UsageRecord.request_id == "req_batch_0007"))
        ).scalar_one()
        trace = (
            await session.execute(
                select(RequestTrace).where(RequestTrace.request_id == "req_batch_0007")
            )
        ).scalar_one()

    assert row.input_tokens == 17
    assert row.output_tokens == 27
    assert trace.prompt_preview == "prompt 7"
    assert trace.route_path == [{"attempt": 1, "ok": True}]

    await writer.stop()


async def test_no_worker_means_inline_write(client):
    """The safety valve: a billing record must never be silently dropped."""
    writer = get_writer()
    assert writer is not None
    # not started -> submit falls back to an inline write
    assert writer.stats()["written"] == 0

    project_id, key_id, team_id = await _ids()
    await submit(_event(99, project_id, key_id, team_id))

    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.request_id == "req_batch_0099")
            )
        ).scalar_one_or_none()

    assert row is not None, "inline fallback failed to persist"
    assert writer.stats()["inline_writes"] == 1


async def test_last_used_at_is_written_once_per_batch(client):
    writer = get_writer()
    await writer.start()
    project_id, key_id, team_id = await _ids()

    for i in range(5):
        await submit(_event(i + 200, project_id, key_id, team_id))
    await writer.flush()

    async with get_session_factory()() as session:
        key = (await session.execute(select(ApiKey).where(ApiKey.id == key_id))).scalar_one()

    assert key.last_used_at is not None

    await writer.stop()