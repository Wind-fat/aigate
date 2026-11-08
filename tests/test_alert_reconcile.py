"""残留 open 告警的对账：谁来关那些「开告警的副本已经没了」的行。

聚合窗口在进程内存里（见 `core.incidents` 的 docstring）：多副本时开告警的副本先退出，
或者进程重启，那一行就永远停在 `period_key="open"`，只能靠人点 resolve。这里钉住对账的
三条底线：

1. **条件不再成立**（provider 已启用 / 已配密钥 / 路由不再指向它）→ 关掉，并写
   `detail.closed_by="reconcile"`：自动关也要留得下「谁关的、凭什么关的」。
2. **条件仍然成立**（还是停用、上游还是不答）→ **不动**。宁可留一条过期告警，也不误关
   一条真实故障。
3. **探测期间行被改写**（还有新的失败在往上写）→ 不关。那是活的故障，不是残留。

另外钉一条前端契约：`unknown_provider` 的告警行要把虚拟模型名存进 `detail.model`，
控制台才能把处置按钮链到 `/routes?model=<model>` —— 没有它，值班只能在一堆路由里猜。
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from aigate.config import get_settings
from aigate.core import incidents
from aigate.db import get_session_factory
from aigate.models import Alert, RoutePolicy

ADMIN = {"X-Admin-Token": get_settings().admin_token}


def _payload(model: str = "chat-default", text: str = "ping") -> dict:
    return {"model": model, "messages": [{"role": "user", "content": text}], "temperature": 0}


async def _alerts(metric: str | None = None) -> list[dict]:
    async with get_session_factory()() as session:
        stmt = select(Alert).where(Alert.scope_type == "provider")
        if metric is not None:
            stmt = stmt.where(Alert.metric == metric)
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": r.id,
                "level": r.level,
                "scope_id": r.scope_id,
                "metric": r.metric,
                "period_key": r.period_key,
                "value": int(r.value),
                "message": r.message,
                "detail": dict(r.detail or {}),
                "resolved_at": r.resolved_at,
            }
            for r in rows
        ]


def _show(label: str, row: dict[str, Any]) -> None:
    print(label, json.dumps({**row, "resolved_at": str(row["resolved_at"])}, default=str, ensure_ascii=False))


async def _open_alert(
    provider: str,
    metric: str = "upstream_failure",
    *,
    value: int = 3,
    detail: dict[str, Any] | None = None,
) -> str:
    """直接写一条 open 行 —— 模拟「那个副本已经退出、留下一条没人收口的告警」。

    走真实请求路径也能造出来，但这里要的是**只有行、没有内存窗口**的状态，那正是对账
    要处理的场景；用请求路径造出来的行在本进程里还有 episode，测不出「重启后没记忆」。
    """
    async with get_session_factory()() as session:
        row = Alert(
            level="critical" if metric == "upstream_failure" else "warn",
            scope_type="provider",
            scope_id=provider,
            scope_slug=provider,
            metric=metric,
            period_key="open",
            value=Decimal(value),
            limit_value=Decimal(0),
            message=f"synthetic {metric} for {provider}",
            detail=detail or {"provider": provider},
        )
        session.add(row)
        await session.commit()
        return row.id


async def test_reconcile_closes_a_stale_config_alert_once_the_provider_is_healthy(
    client, auth_headers
):
    """仍 open 的配置告警 + provider 已恢复 → 关掉，`closed_by="reconcile"`。"""
    await client.put("/admin/v1/providers/mock", headers=ADMIN, json={"enabled": False})
    bad = await client.post("/v1/chat/completions", headers=auth_headers, json=_payload())
    assert bad.json()["error"]["code"] == "provider_disabled"

    before = await _alerts("provider_config")
    assert len(before) == 1 and before[0]["resolved_at"] is None, before
    _show("before reconcile:", before[0])

    # 配置修好了（重新启用），但**没有人再打这条路由**：本进程的内存窗口不会自己去关。
    await client.put("/admin/v1/providers/mock", headers=ADMIN, json={"enabled": True})
    summary = await incidents.reconcile_open_alerts()
    print("reconcile summary:", json.dumps(summary, ensure_ascii=False))
    assert summary["scanned"] == 1, summary
    assert len(summary["closed"]) == 1, summary
    assert summary["closed"][0]["reason"] == "config_ok", summary

    after = await _alerts("provider_config")
    _show("after reconcile:", after[0])
    row = after[0]
    assert row["resolved_at"] is not None, "条件不成立了就该 resolved"
    assert row["period_key"].startswith("closed:"), row["period_key"]
    assert row["detail"]["closed_by"] == "reconcile", row["detail"]
    assert row["detail"]["reconcile_reason"] == "config_ok", row["detail"]
    assert row["detail"]["resolved_at"], "detail 里也要有恢复时间"
    assert row["detail"]["duration_s"] is not None, row["detail"]
    assert "reconciled" in row["message"], row["message"]

    # 控制台默认列表（未确认且未恢复）里不再出现
    listed = (await client.get("/admin/v1/alerts", headers=ADMIN)).json()
    assert listed["count"] == 0, listed["items"]

    # 幂等：没有 open 行可扫，第二遍什么也不做
    again = await incidents.reconcile_open_alerts()
    assert again["scanned"] == 0 and again["closed"] == [], again


async def test_reconcile_keeps_an_alert_whose_condition_is_still_true(client, auth_headers):
    """provider 还是停用 → 行原样留着（不关、不改、不刷屏）。"""
    await client.put("/admin/v1/providers/mock", headers=ADMIN, json={"enabled": False})
    await client.post("/v1/chat/completions", headers=auth_headers, json=_payload())

    before = await _alerts("provider_config")
    assert len(before) == 1 and before[0]["resolved_at"] is None, before

    summary = await incidents.reconcile_open_alerts()
    print("reconcile summary (still broken):", json.dumps(summary, ensure_ascii=False))
    assert summary["scanned"] == 1, summary
    assert summary["closed"] == [], "还坏着的行绝不能关"
    assert summary["kept"][0]["reason"] == "provider_disabled", summary

    after = await _alerts("provider_config")
    row = after[0]
    assert row["resolved_at"] is None and row["period_key"] == "open", row
    assert row["message"] == before[0]["message"], "没变更就不许重写这一行（不刷屏）"
    assert "closed_by" not in row["detail"], row["detail"]


async def test_reconcile_closes_an_upstream_alert_only_when_the_probe_says_it_answers(client):
    """上游故障：探针说「在应答」才关；探不通 = 证据不足，留着。"""
    await _open_alert("mock", "upstream_failure", value=3)

    async def unreachable(provider: str, cfg: Any) -> incidents._Probe:
        return incidents._Probe(False, "unreachable", "ConnectError: refused")

    summary = await incidents.reconcile_open_alerts(upstream_probe=unreachable)
    assert summary["closed"] == [], summary
    assert summary["kept"][0]["reason"] == "unreachable", summary
    rows = await _alerts("upstream_failure")
    assert rows[0]["resolved_at"] is None, "探不通不能当恢复"

    async def answering(provider: str, cfg: Any) -> incidents._Probe:
        return incidents._Probe(True, "upstream_answered_200", "http://x/models")

    summary = await incidents.reconcile_open_alerts(upstream_probe=answering)
    assert len(summary["closed"]) == 1, summary
    rows = await _alerts("upstream_failure")
    _show("upstream alert after reconcile:", rows[0])
    assert rows[0]["detail"]["closed_by"] == "reconcile", rows[0]["detail"]
    assert rows[0]["detail"]["reconcile_reason"] == "upstream_answered_200", rows[0]["detail"]
    assert rows[0]["detail"]["consecutive_failures"] == 3, "历史结论要留住"
    assert rows[0]["resolved_at"] is not None


async def test_a_config_broken_provider_is_not_closed_on_upstream_evidence_alone(client):
    """provider 本身在路由里是「不存在的名字」→ 就算上游探针说活着也不关。"""
    async with get_session_factory()() as session:
        policy = (
            await session.execute(select(RoutePolicy).where(RoutePolicy.match_model == "chat-default"))
        ).scalar_one()
        policy.candidates = [{"provider": "ghost-vendor", "model": "x"}]
        await session.commit()

    await _open_alert("ghost-vendor", "upstream_failure")

    async def answering(provider: str, cfg: Any) -> incidents._Probe:
        return incidents._Probe(True, "upstream_answered_200")

    summary = await incidents.reconcile_open_alerts(upstream_probe=answering)
    assert summary["closed"] == [], summary
    assert summary["kept"][0]["reason"] == "provider_missing", summary


async def test_a_row_that_changes_while_probing_is_not_closed(client):
    """探测期间还有新的失败写进来 → 那是活故障，不关（否则就是把正在发生的事关掉）。"""
    alert_id = await _open_alert("mock", "upstream_failure", value=3)

    async def bump_then_ok(provider: str, cfg: Any) -> incidents._Probe:
        async with get_session_factory()() as session:
            row = await session.get(Alert, alert_id)
            row.value = Decimal(4)
            await session.commit()
        return incidents._Probe(True, "upstream_answered_200")

    summary = await incidents.reconcile_open_alerts(upstream_probe=bump_then_ok)
    assert summary["closed"] == [], summary
    assert summary["kept"][0]["reason"] == "changed_while_probing", summary
    rows = await _alerts("upstream_failure")
    assert rows[0]["resolved_at"] is None and rows[0]["value"] == 4, rows


async def test_unknown_provider_alert_carries_the_model_and_clears_when_the_route_moves(
    client, auth_headers
):
    """`unknown_provider` 行里要有 model（控制台链到 `/routes?model=…`）；路由改走后自动收口。"""
    async with get_session_factory()() as session:
        policy = (
            await session.execute(select(RoutePolicy).where(RoutePolicy.match_model == "chat-default"))
        ).scalar_one()
        good = list(policy.candidates)
        policy.candidates = [{"provider": "ghost-vendor", "model": "x"}]
        await session.commit()

    resp = await client.post("/v1/chat/completions", headers=auth_headers, json=_payload())
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "provider_unknown"

    rows = await _alerts("provider_config")
    assert len(rows) == 1, rows
    _show("unknown_provider alert:", rows[0])
    assert rows[0]["detail"]["reason"] == "unknown_provider", rows[0]["detail"]
    # 前端 click → `/routes?model=chat-default`。模型名必须落在 detail 里，否则链接只能瞎猜。
    assert rows[0]["detail"]["model"] == "chat-default", rows[0]["detail"]

    # 路由改回一个真的 provider：坏路由不存在了 → 这条告警的条件已经不成立
    async with get_session_factory()() as session:
        policy = (
            await session.execute(select(RoutePolicy).where(RoutePolicy.match_model == "chat-default"))
        ).scalar_one()
        policy.candidates = good
        await session.commit()

    summary = await incidents.reconcile_open_alerts()
    print("reconcile summary (route moved):", json.dumps(summary, ensure_ascii=False))
    assert len(summary["closed"]) == 1, summary
    assert summary["closed"][0]["reason"] == "route_no_longer_references_provider", summary
    rows = await _alerts("provider_config")
    assert rows[0]["detail"]["closed_by"] == "reconcile", rows[0]["detail"]