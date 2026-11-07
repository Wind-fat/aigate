"""月预算告警的账期键必须按 AIGATE_TIMEZONE 切，而不是固定 Asia/Shanghai。

缺陷（D1 遗留①，`api/v1_chat.py:388`）：

    period_key=timeutil.local_date(quota.month_start(tz_name=settings.timezone))[:7]

`local_date` 的第二个参数（时区名）漏传，于是退回 `timeutil.DEFAULT_TZ`
（Asia/Shanghai）；而**花费窗口**用的是 `settings.timezone`。两把尺子不在同一时区时，
跨月那一刻会出现「窗口已进新月份、账期键还写上月」：唯一索引
`(scope_type, scope_id, metric, period_key)` 把新月份的首次超支当成上月那一行的重复项，
于是要么被升级到上月的告警里，要么在 rollback 之后把会话里的实例全部过期、
让请求在后续访问 `principal.project` 时炸成 500。新月份的第一条预算告警永远不出现。

触发条件：任何比上海靠东的时区（如 `Asia/Tokyo`, UTC+9）。东京 3 月 1 日 00:30 时
上海还是 2 月 28 日 23:30 —— 月界正好落在两地日期不同的那一瞬。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from aigate import config, timeutil
from aigate.core import quota
from aigate.db import get_session_factory
from aigate.models import Alert, Project, Team, UsageRecord

TZ = "Asia/Tokyo"

# 2027-03-01 00:30 东京 == 2027-02-28T15:30Z == 2027-02-28 23:30 上海
NOW = datetime(2027, 2, 28, 15, 30, tzinfo=UTC)
# 东京的 3 月窗口从 2027-02-28T15:00Z 起，这笔钱落在新月份窗口内、但上海日期还在 2 月
SPEND_AT = datetime(2027, 2, 28, 15, 10, tzinfo=UTC)


class _FrozenDatetime(datetime):
    frozen: datetime = NOW

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - 只冻结「现在」，其余行为继承
        return cls.frozen.astimezone(tz) if tz is not None else cls.frozen.replace(tzinfo=None)


@pytest.fixture
def tokyo_month_boundary(monkeypatch):
    """把配置时区切成东京，并冻结所有「现在」。"""
    monkeypatch.setattr(timeutil, "datetime", _FrozenDatetime)
    monkeypatch.setattr(quota, "datetime", _FrozenDatetime)
    # v1_chat 走 Depends(get_settings)，拿到的是同一个 lru_cache 单例
    monkeypatch.setattr(config.get_settings(), "timezone", TZ)


async def _seed(spend: Decimal) -> Project:
    async with get_session_factory()() as session:
        project = (await session.execute(select(Project).where(Project.slug == "test-project"))).scalar_one()
        team = (await session.execute(select(Team).where(Team.id == project.team_id))).scalar_one()
        project.monthly_budget_cny = Decimal("100")
        session.add(
            UsageRecord(
                request_id="r-new-month",
                team_id=team.id,
                project_id=project.id,
                api_key_id="test-key",
                provider="mock",
                model_requested="chat-default",
                model_used="mock-ok",
                cost_cny=spend,
                created_at=SPEND_AT,
            )
        )
        await session.commit()
        return project


async def _period_keys() -> dict[str, Alert]:
    async with get_session_factory()() as session:
        rows = (
            (await session.execute(select(Alert).where(Alert.metric == "budget_monthly"))).scalars().all()
        )
    return {r.period_key: r for r in rows}


async def test_monthly_alert_period_key_follows_configured_timezone(
    client, auth_headers, chat_payload, tokyo_month_boundary
):
    """已花 90/100 落在新月份的窗口里：账期键必须是 2027-03，不是上个月的 2027-02。"""
    await _seed(Decimal("90"))

    resp = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert resp.status_code == 200, resp.text

    by_period = await _period_keys()
    assert "2027-03" in by_period, (
        f"新月份的首条预算告警应落在 2027-03，实际只有 {sorted(by_period)}"
        " —— 落在 2027-02 说明 period_key 用了上海日期，没跟花费窗口同一把尺子"
    )
    assert by_period["2027-03"].level == "warn"


async def test_new_month_alert_is_not_swallowed_by_last_months_row(
    client, auth_headers, chat_payload, tokyo_month_boundary
):
    """上月已有告警行时，新月份的首次超支必须写自己的行，而不是撞掉/升级上月那行。"""
    project = await _seed(Decimal("90"))
    async with get_session_factory()() as session:
        session.add(
            Alert(
                level="warn",
                scope_type="project",
                scope_id=project.id,
                scope_slug=project.slug,
                metric="budget_monthly",
                period_key="2027-02",
                value=Decimal("85"),
                limit_value=Decimal("100"),
                message="2 月的旧告警，本用例只许它原地不动",
            )
        )
        await session.commit()

    resp = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert resp.status_code == 200, f"撞上上月告警行不应把请求打成 {resp.status_code}: {resp.text}"

    by_period = await _period_keys()
    assert "2027-03" in by_period, f"新月份告警被上月那行吞掉了，实际账期键有 {sorted(by_period)}"
    assert by_period["2027-02"].level == "warn", "2 月那行不该被新月份的花费升级"
    assert by_period["2027-02"].value == Decimal("85")