"""QA 独立复现 + 回归：告警撞唯一索引不得污染请求会话（P0）。

缺陷（修复前，本文件四条断言方向为「按错误行为通过」）：
  db.py:33          async_sessionmaker(..., expire_on_commit=False)
                    -> commit 不 expire，所以第一个请求（告警 insert 成功）能走完全程。
  quota.py:330      IntegrityError -> await session.rollback()
                    -> rollback 无视 expire_on_commit，强制 expire 会话内全部持久对象，
                       包括 deps.py 里同一会话 selectinload 出来的
                       api_key / project / team（Principal 三个成员全挂在请求会话上）。
  v1_chat.py        rollback 之后端点继续同步访问 principal.project.id
                    （cache.lookup 实参、_persist 里的 team.id/project.id/api_key.id），
                    过期属性触发同步刷新 IO -> AsyncSession -> MissingGreenlet -> 500。

触发条件（从代码推的最小集）：
  1. project 或 team 配了 monthly_budget_cny；
  2. 本账期花费落在 [80%, 100%) 告警带；
  3. 本账期告警行已存在（即进入告警带后的第 2 次及以后任意一次请求）；
  4. 请求走到 v1_chat 的 warn 循环（budget 仍 allowed）。

修复后（raise_alert_if_needed 把 insert 放进 SAVEPOINT，冲突只回滚到保存点）：
  去重依然靠唯一索引 —— 同一账期同一告警恒为一行，不多不少；
  但请求会话的 identity map 不再被 expire，告警带内任意次请求都是 200，
  流式的用量行也必须照常落库（否则是静默漏账）。
"""

from __future__ import annotations

import traceback
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select

from aigate.db import get_session_factory
from aigate.models import Alert, Project, Team, UsageRecord

PAYLOAD_DETERMINISTIC = {
    "model": "chat-default",
    "messages": [{"role": "user", "content": "hello"}],
    "temperature": 0,
}
PAYLOAD_CACHE_MISS = {
    "model": "chat-default",
    "messages": [{"role": "user", "content": "hello"}],
    "temperature": 0.7,
}
PAYLOAD_STREAM = {
    "model": "chat-default",
    "messages": [{"role": "user", "content": "hello"}],
    "stream": True,
}


async def _seed_spend(cost: str) -> None:
    """在当前账期塞一笔花费，把项目推进告警带。"""
    async with get_session_factory()() as session:
        project = (
            await session.execute(select(Project).where(Project.slug == "test-project"))
        ).scalar_one()
        team = (await session.execute(select(Team).where(Team.id == project.team_id))).scalar_one()
        session.add(
            UsageRecord(
                request_id="seed-qa",
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


async def _usage_rows() -> int:
    async with get_session_factory()() as session:
        return (await session.execute(select(func.count()).select_from(UsageRecord))).scalar_one()


async def _budget_alerts() -> list[Alert]:
    async with get_session_factory()() as session:
        return (
            (await session.execute(select(Alert).where(Alert.metric == "budget_monthly")))
            .scalars()
            .all()
        )


def _tb(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


async def test_warn_band_second_request_succeeds(client, auth_headers):
    """预算 100、已花 90：告警带内连发三次都必须 200，且告警恒为一行。"""
    await _seed_spend("90")

    r1 = await client.post("/v1/chat/completions", json=PAYLOAD_DETERMINISTIC, headers=auth_headers)
    assert r1.status_code == 200, r1.text
    assert r1.json()["choices"], r1.text

    alerts = await _budget_alerts()
    assert len(alerts) == 1 and alerts[0].level == "warn", [
        (a.level, a.period_key) for a in alerts
    ]

    # 第 2、3 次：告警行已存在，insert 撞唯一索引 —— 这正是修复前 500 的位置。
    for n in (2, 3):
        r = await client.post(
            "/v1/chat/completions", json=PAYLOAD_DETERMINISTIC, headers=auth_headers
        )
        assert r.status_code == 200, f"第 {n} 次请求被 500 挡住：{r.text[:400]}"
        assert r.json()["choices"], r.text

    # 去重语义不许放宽：同一账期同一告警仍然只有一行。
    alerts = await _budget_alerts()
    assert len(alerts) == 1, f"告警行重复了：{[(a.level, a.period_key) for a in alerts]}"


async def test_warn_band_noncache_path_succeeds(client, auth_headers):
    """temperature>0（不进缓存）同样不能再炸：修复前第一个同步触点是 _persist 里的 team/project id。"""
    await _seed_spend("90")

    r1 = await client.post("/v1/chat/completions", json=PAYLOAD_CACHE_MISS, headers=auth_headers)
    assert r1.status_code == 200, r1.text
    rows_after_r1 = await _usage_rows()

    try:
        r2 = await client.post("/v1/chat/completions", json=PAYLOAD_CACHE_MISS, headers=auth_headers)
    except Exception as exc:  # noqa: BLE001 - 修复前这里冒 MissingGreenlet
        raise AssertionError(f"第 2 次请求抛异常而不是返回响应：\n{_tb(exc)}") from exc
    assert r2.status_code == 200, r2.text

    # 非缓存路径的账也要记：r2 必须新增一行用量。
    assert await _usage_rows() == rows_after_r1 + 1
    assert len(await _budget_alerts()) == 1


async def test_warn_band_streaming_keeps_usage_accounting(client, auth_headers):
    """流式：修复前客户端拿到完整流和 [DONE]，但 _persist 抛 MissingGreenlet -> 用量行静默丢失。"""
    await _seed_spend("90")

    r1 = await client.post("/v1/chat/completions", json=PAYLOAD_STREAM, headers=auth_headers)
    assert r1.status_code == 200, r1.text
    assert "[DONE]" in r1.text, r1.text
    rows_after_r1 = await _usage_rows()
    assert rows_after_r1 >= 2, "seed + r1 的用量行都该在"

    try:
        r2 = await client.post("/v1/chat/completions", json=PAYLOAD_STREAM, headers=auth_headers)
        body = r2.text  # 强制读完流
    except Exception as exc:  # noqa: BLE001 - 修复前这里冒 MissingGreenlet
        raise AssertionError(f"第 2 次流式请求抛异常：\n{_tb(exc)}") from exc

    assert r2.status_code == 200, body
    assert "[DONE]" in body, body
    # 关键断言：流式路径的计费不许静默丢账，r2 恰好新增一行。
    rows_after_r2 = await _usage_rows()
    assert rows_after_r2 == rows_after_r1 + 1, (
        f"r2 的用量行没落库：{rows_after_r1} -> {rows_after_r2}（静默漏账）"
    )
    assert len(await _budget_alerts()) == 1


async def test_over_100pct_path_still_429(client, auth_headers):
    """影响面边界：花费 >= 100% 走 check_budgets 的 429 分支；告警照样只一行、依然 429。"""
    await _seed_spend("100")

    r1 = await client.post("/v1/chat/completions", json=PAYLOAD_CACHE_MISS, headers=auth_headers)
    assert r1.status_code == 429, r1.text
    assert "gateway.budget_exhausted" in r1.text, r1.text

    # 第 2 次：告警行已存在 -> insert 撞唯一索引；该分支随后直接 raise HTTPException，
    # 但出口必须仍是 429，不能因为告警写入变成 500。
    r2 = await client.post("/v1/chat/completions", json=PAYLOAD_CACHE_MISS, headers=auth_headers)
    assert r2.status_code == 429, r2.text
    assert "gateway.budget_exhausted" in r2.text, r2.text

    alerts = await _budget_alerts()
    assert len(alerts) == 1, f"告警行重复了：{[(a.level, a.period_key) for a in alerts]}"
    assert alerts[0].level == "critical"