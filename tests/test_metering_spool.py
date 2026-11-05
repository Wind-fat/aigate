"""Spool durability: does a crash lose charges, and does replay double-bill?"""

from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import select

from aigate.db import get_session_factory
from aigate.metering_queue import MeteringWriter, Spool, UsageEvent
from aigate.models import Project, RequestTrace, UsageRecord


async def _ids() -> tuple[str, str, str]:
    async with get_session_factory()() as session:
        project = (await session.execute(select(Project).limit(1))).scalar_one()
        return project.id, project.team_id, project.id


def _ev(idx: int, project_id: str, team_id: str) -> UsageEvent:
    return UsageEvent(
        request_id=f"req_spool_{idx:04d}",
        team_id=team_id,
        project_id=project_id,
        api_key_id="key-spool",
        provider="mock",
        model_requested="chat-default",
        model_used="mock-ok",
        input_tokens=10,
        output_tokens=20,
        cost_cny=Decimal("0.000100"),
        latency_ms=11,
        ttft_ms=None,
        status="ok",
        error_code=None,
        cache_hit=None,
        degraded=False,
        prompt_sha256="b" * 64,
        prompt_preview=f"prompt {idx}",
        pii_masked=0,
        http_status=200,
    )


async def _rows() -> list[UsageRecord]:
    async with get_session_factory()() as session:
        return list((await session.execute(select(UsageRecord))).scalars().all())


async def _traces() -> list[RequestTrace]:
    async with get_session_factory()() as session:
        return list((await session.execute(select(RequestTrace))).scalars().all())


async def test_spool_replays_uncommitted_events_on_start(client, tmp_path):
    """The whole point: events that never reached the DB come back on the next boot."""
    spool_path = tmp_path / "m.spool"
    seed = Spool(spool_path)
    project_id, team_id, _ = await _ids()
    for i in range(3):
        seed.append(i + 1, _ev(i, project_id, team_id))

    assert spool_path.exists()
    assert len(await _rows()) == 0

    writer = MeteringWriter(get_session_factory(), spool=Spool(spool_path))
    await writer.start()  # replay happens inside start()
    await writer.stop()

    assert len(await _rows()) == 3
    assert len(await _traces()) == 3
    assert writer.stats()["replayed"] == 3
    assert Spool(spool_path).load() == [], "acknowledged events must leave the spool"


async def test_replay_is_idempotent_after_crash_between_commit_and_ack(client, tmp_path):
    """A crash after COMMIT but before spool-ack must not bill the customer twice."""
    spool_path = tmp_path / "m.spool"
    project_id, team_id, _ = await _ids()

    seed = Spool(spool_path)
    for i in range(2):
        seed.append(i + 1, _ev(i, project_id, team_id))

    first = MeteringWriter(get_session_factory(), spool=Spool(spool_path))
    await first.start()
    await first.stop()
    assert len(await _rows()) == 2

    # simulate the crash window: the same events are still on disk
    replay_again = Spool(spool_path)
    for i in range(2):
        replay_again.append(i + 1, _ev(i, project_id, team_id))

    second = MeteringWriter(get_session_factory(), spool=Spool(spool_path))
    await second.start()
    await second.stop()

    assert len(await _rows()) == 2, "duplicate replay must not create extra charges"
    assert second.stats()["duplicates_skipped"] == 2
    assert Spool(spool_path).load() == []


def test_ack_removes_only_acknowledged(tmp_path):
    spool = Spool(tmp_path / "a.spool")
    for seq in range(3):
        spool.append(seq, _ev(seq, "p", "t"))
    spool.ack([0, 2])

    remaining = [item.seq for item in Spool(tmp_path / "a.spool").load()]
    assert remaining == [1]


def test_corrupt_lines_are_skipped_not_fatal(tmp_path):
    path = tmp_path / "c.spool"
    good = json.dumps({"_seq": 7, **_ev(7, "p", "t").to_json()}, ensure_ascii=False)
    path.write_text(f"{good}\nnot-json-at-all\n", encoding="utf-8")

    items = Spool(path).load()
    assert [item.seq for item in items] == [7]


async def test_spool_is_empty_after_normal_operation(client, tmp_path):
    spool_path = tmp_path / "n.spool"
    writer = MeteringWriter(get_session_factory(), spool=Spool(spool_path))
    await writer.start()

    project_id, team_id, _ = await _ids()
    for i in range(5):
        await writer.submit(_ev(i + 10, project_id, team_id))
    await writer.flush()

    assert len(await _rows()) == 5
    assert writer.stats()["spooled"] == 0
    assert not spool_path.exists() or spool_path.read_text(encoding="utf-8").strip() == ""

    await writer.stop()
