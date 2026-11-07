"""日预算账期必须按 AIGATE_TIMEZONE 的本地自然日切，而不是 UTC 日期。

缺陷复现（Q1 D1）：`check_budgets` 的 period_key 取 `now.date()`（UTC），而花费窗口
取 `day_start(now)`（默认 Asia/Shanghai）。北京时间 00:00-08:00 之间两者跨在不同的
日期上，唯一索引 `(scope_type, scope_id, metric, period_key)` 把新一天的超支当成
前一天的重复告警吞掉。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from aigate.core import quota
from aigate.db import get_session_factory
from aigate.models import Alert, Project, Team, UsageRecord

TZ = "Asia/Shanghai"

# 北京时间 2026-11-06 23:00 == 2026-11-06T15:00Z
CST_2026_11_06_23 = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)
# 北京时间 2026-11-07 01:00 == 2026-11-06T17:00Z  （UTC 日期还是 11-06）
CST_2026_11_07_01 = datetime(2026, 11, 6, 17, 0, tzinfo=UTC)


class _FrozenDatetime(datetime):
    frozen: datetime = CST_2026_11_06_23

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - 只冻结「现在」，其余行为继承
        return cls.frozen.astimezone(tz) if tz is not None else cls.frozen.replace(tzinfo=None)


@pytest.fixture
def frozen_clock(monkeypatch):
    """冻结 quota 模块里的 `datetime.now()`，调用形状与 v1_chat 完全一致。"""
    monkeypatch.setattr(quota, "datetime", _FrozenDatetime)
    return _FrozenDatetime


async def _set_daily_budget(limit: Decimal) -> None:
    async with get_session_factory()() as session:
        await session.execute(
            update(Project).where(Project.slug == "test-project").values(daily_budget_cny=limit)
        )
        await session.commit()


def _usage(project: Project, team: Team, *, cost: str, at: datetime, rid: str) -> UsageRecord:
    return UsageRecord(
        request_id=rid,
        team_id=team.id,
        project_id=project.id,
        api_key_id="test-key",
        provider="mock",
        model_requested="chat-default",
        model_used="mock-ok",
        cost_cny=Decimal(cost),
        created_at=at,
    )


async def _raise(session, check: quota.BudgetCheck) -> str | None:
    return await quota.raise_alert_if_needed(
        session,
        scope_type=check.scope,
        scope_id=check.scope_id,
        scope_slug=check.scope_slug,
        metric=check.metric,
        spent=check.spent,
        limit=check.limit or Decimal("0"),
        period_key=check.period_key,
    )


async def test_daily_budget_alert_survives_local_midnight(client, frozen_clock):
    """23:00 CST 已超支并告警；次日 01:00 CST 又是新一天、又超支 —— 必须产生新告警行。"""
    await _set_daily_budget(Decimal("1"))

    async with get_session_factory()() as session:
        project = (await session.execute(select(Project).where(Project.slug == "test-project"))).scalar_one()
        team = (await session.execute(select(Team).where(Team.id == project.team_id))).scalar_one()

        # --- 第一天 23:00 CST：花 5 元，日预算 1 元 ---------------------------------
        session.add(_usage(project, team, cost="5", at=CST_2026_11_06_23, rid="r-day1"))
        await session.commit()

        frozen_clock.frozen = CST_2026_11_06_23
        day1 = await quota.check_budgets(session, project, team, tz_name=TZ)
        assert day1.metric == "budget_daily"
        assert day1.allowed is False
        assert day1.period_key == "2026-11-06"
        assert await _raise(session, day1) == "critical"

        # --- 第二天 01:00 CST：窗口已重置，新花 5 元 -------------------------------
        session.add(_usage(project, team, cost="5", at=CST_2026_11_07_01, rid="r-day2"))
        await session.commit()

        frozen_clock.frozen = CST_2026_11_07_01
        day2 = await quota.check_budgets(session, project, team, tz_name=TZ)
        assert day2.allowed is False
        assert day2.spent == Decimal("5"), "日窗口必须从北京时间 11-07 00:00 起算"
        assert day2.period_key == "2026-11-07", "period_key 必须是当地日期，不是 UTC 日期"
        assert await _raise(session, day2) == "critical", "新一天的首次超支不能被当成重复告警吞掉"

        rows = (await session.execute(select(Alert).where(Alert.metric == "budget_daily"))).scalars().all()
        assert sorted(r.period_key for r in rows) == ["2026-11-06", "2026-11-07"]
        assert all(r.level == "critical" for r in rows)


async def test_daily_budget_window_follows_configured_timezone(client, frozen_clock):
    """日预算的 `since` 必须用 AIGATE_TIMEZONE，而不是永远 Asia/Shanghai。"""
    await _set_daily_budget(Decimal("1"))

    # now = 2026-11-05T17:00Z（北京 11-06 01:00）。
    # 上海窗口从 11-05T16:00Z 起，UTC 窗口从 11-05T00:00Z 起。
    # 花在 11-05T10:00Z：落在 UTC 窗口内，落在上海窗口外 —— 正好区分两把尺子。
    spend_at = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)
    frozen_clock.frozen = datetime(2026, 11, 5, 17, 0, tzinfo=UTC)

    async with get_session_factory()() as session:
        project = (await session.execute(select(Project).where(Project.slug == "test-project"))).scalar_one()
        team = (await session.execute(select(Team).where(Team.id == project.team_id))).scalar_one()
        session.add(_usage(project, team, cost="5", at=spend_at, rid="r-tz"))
        await session.commit()

        shanghai = await quota.check_budgets(session, project, team, tz_name="Asia/Shanghai")
        assert shanghai.allowed is True, "上海窗口从 11-05T16:00Z 起，10:00Z 那笔不算"

        utc = await quota.check_budgets(session, project, team, tz_name="UTC")
        assert utc.period_key == "2026-11-05"
        assert utc.spent == Decimal("5"), "UTC 窗口从 11-05T00:00Z 起，10:00Z 那笔要算进来"
        assert utc.allowed is False
