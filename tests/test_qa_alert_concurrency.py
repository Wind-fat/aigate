"""QA 并发压测：告警带内 N 个并发请求打 /v1/chat/completions。

复验单第 3 条：状态 = 月预算 100、已花 90、warn 行已存在（预热请求写入）。
断言：
  1. budget_monthly 告警行恒为 1（唯一索引去重在并发下成立）；
  2. 用量行数 = 基线 + **200 的条数**，成功的请求一条都不许丢；
  3. 无 sqlite3.OperationalError: database is locked；
  4. 只有两种合法结局：200，或池满的快速 503（`gateway.pool_timeout` + `Retry-After`）。

第 4 条原来写的是「全部 200」，在 SQLite 上侥幸成立、换 PostgreSQL 立刻破：池容量是
`pool_size 5 + max_overflow 10 = 15`，N=16 时有 1 个请求必须排队，排队超过
`AIGATE_DB_POOL_TIMEOUT`(1s) 就是设计好的 503（`test_qa_pool_fast_fail.py` 里
`test_sixteen_concurrent_never_hangs` 早已允许 200 或快速 503）。把「全部 200」写死，
测的不是并发正确性，而是 SQLite 恰好够快。
"""

from __future__ import annotations

import asyncio
import traceback
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from aigate.db import get_session_factory
from aigate.models import Alert, Project, Team, UsageRecord

PAYLOAD = {
    "model": "chat-default",
    "messages": [{"role": "user", "content": "concurrent probe"}],
    "temperature": 0.7,  # 不进语义缓存，每个请求都走真实 provider + _persist
}


async def _seed_spend(cost: str) -> None:
    async with get_session_factory()() as session:
        project = (
            await session.execute(select(Project).where(Project.slug == "test-project"))
        ).scalar_one()
        team = (await session.execute(select(Team).where(Team.id == project.team_id))).scalar_one()
        session.add(
            UsageRecord(
                request_id="seed-conc",
                team_id=team.id,
                project_id=project.id,
                api_key_id="seed-key",
                provider="mock",
                model_requested="chat-default",
                model_used="mock-ok",
                cost_cny=Decimal(cost),
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def _counts() -> tuple[int, list[Alert]]:
    async with get_session_factory()() as session:
        usage = (
            await session.execute(select(func.count()).select_from(UsageRecord))
        ).scalar_one()
        alerts = (
            (await session.execute(select(Alert).where(Alert.metric == "budget_monthly")))
            .scalars()
            .all()
        )
        return usage, alerts


@pytest.mark.parametrize("n", [8, 16])
async def test_concurrent_requests_in_warn_band(client, auth_headers, n):
    await _seed_spend("90")

    # 预热：写入 warn 告警行（构造「warn 行已存在」的初始状态），同时验证单发 200
    warm = await client.post("/v1/chat/completions", json=PAYLOAD, headers=auth_headers)
    assert warm.status_code == 200, warm.text
    base_usage, base_alerts = await _counts()
    assert len(base_alerts) == 1 and base_alerts[0].level == "warn"

    results = await asyncio.gather(
        *(client.post("/v1/chat/completions", json=PAYLOAD, headers=auth_headers) for _ in range(n)),
        return_exceptions=True,
    )

    # 3. 异常/锁检查：任何异常都算失败，database is locked 单独点名
    exc_texts = [
        "".join(traceback.format_exception(type(r), r, r.__traceback__))
        for r in results
        if isinstance(r, BaseException)
    ]
    assert not exc_texts, f"{len(exc_texts)} 个请求抛异常：\n{exc_texts[:2]}"
    assert "database is locked" not in "".join(exc_texts)

    # 4. 只有两种合法结局：200，或池满的快速 503（带 Retry-After 的 gateway.pool_timeout）
    statuses = [r.status_code for r in results]
    assert set(statuses) <= {200, 503}, f"状态码分布：{sorted(statuses)}"
    for r in results:
        if r.status_code == 503:
            assert r.headers.get("retry-after"), f"503 必须带 Retry-After：{r.text}"
            assert r.json()["error"]["code"] == "gateway.pool_timeout", r.text
    bodies = "".join(r.text for r in results)
    assert "database is locked" not in bodies
    assert "Internal Server Error" not in bodies

    # 2. 计费只许「成功几条写几条」：一条 200 都不许丢，被 503 挡掉的本来就没调用上游
    accepted = statuses.count(200)
    after_usage, after_alerts = await _counts()
    assert after_usage == base_usage + accepted, (
        f"用量行与 200 数不符：{base_usage} + {accepted} != {after_usage}"
        f"（状态码 {sorted(statuses)}）"
    )

    # 1. 告警去重：恒为 1 行，且仍是 warn（花费未越过 100%）
    assert len(after_alerts) == 1, f"告警行重复：{[(a.level, a.period_key) for a in after_alerts]}"
    assert after_alerts[0].level == "warn"
