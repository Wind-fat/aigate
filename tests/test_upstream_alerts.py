"""上游故障告警：连续失败要落一条**可处置**的 Alert，而不是只多一行日志。

真调时暴露的缺口：供应商整夜 502，网关上只有计数器和熔断态，`alerts` 表一条都没有
（它唯一的写入者是预算检查）。

这里的断言全部落在**库里的行**上，不落「我们以为会写什么」：

1. 假上游连续失败 3 次 → 恰好一条 `scope_type="provider"` 的 Alert，字段对得上；
   结构化 WARNING 日志里的 provider / consecutive_failures 也在。
2. 继续失败 → 仍然一条（聚合），`value` 跟着涨；**确认之后也不刷**，确认时间不被清掉。
3. 恢复一次 → 同一行被 resolve（`resolved_at` / `period_key="closed:…"` /
   `detail.duration_s`），默认列表不再显示；下一次故障另开一条。
4. 阈值可配（默认 3，`AIGATE_UPSTREAM_ALERT_THRESHOLD`）。
5. 厂商 4xx（非 retryable）说明它活着 → 不告警。
6. 流式路径同样计数。
"""

from __future__ import annotations

import json
import logging

import pytest
from sqlalchemy import select

from aigate.config import get_settings
from aigate.core import circuit, incidents
from aigate.db import get_session_factory
from aigate.models import Alert, UsageRecord

ADMIN = {"X-Admin-Token": get_settings().admin_token}
FAILOVER = "failover-demo"  # mock/fail500-primary -> mock-alt/mock-ok


def _payload(model: str = FAILOVER, text: str = "ping") -> dict:
    return {"model": model, "messages": [{"role": "user", "content": text}], "temperature": 0}


async def _provider_alerts() -> list[dict]:
    """Read the rows the way the console would: flat dicts, all fields intact."""
    async with get_session_factory()() as session:
        rows = (
            (await session.execute(select(Alert).where(Alert.scope_type == "provider")))
            .scalars()
            .all()
        )
        return [
            {
                "id": r.id,
                "level": r.level,
                "scope_type": r.scope_type,
                "scope_id": r.scope_id,
                "metric": r.metric,
                "period_key": r.period_key,
                "value": int(r.value),
                "limit_value": int(r.limit_value),
                "message": r.message,
                "detail": dict(r.detail or {}),
                "created_at": r.created_at,
                "acknowledged_at": r.acknowledged_at,
                "resolved_at": r.resolved_at,
            }
            for r in rows
        ]


async def _ok(client, headers, model: str = FAILOVER, text: str = "ping") -> dict:
    """POST 一次并要求 200；失败时把响应体带出来。"""
    body = {"model": model, "messages": [{"role": "user", "content": text}], "temperature": 0}
    resp = await client.post("/v1/chat/completions", headers=headers, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()

async def test_three_consecutive_failures_raise_one_provider_alert(client, auth_headers, caplog):
    with caplog.at_level(logging.WARNING, logger="aigate.upstream"):
        for _ in range(3):
            resp = await client.post("/v1/chat/completions", headers=auth_headers, json=_payload())
            assert resp.status_code == 200, resp.text  # 备用实例兜住了，调用方仍是 200
            assert resp.json()["x_aigate"]["degraded"] is True

    rows = await _provider_alerts()
    assert len(rows) == 1, rows
    a = rows[0]
    print("provider alert row:", json.dumps({**a, "created_at": str(a["created_at"])}, ensure_ascii=False))

    assert a["scope_id"] == "mock"
    assert a["metric"] == "upstream_failure"
    assert a["level"] == "critical"
    assert a["period_key"] == "open"
    assert a["value"] == 3 and a["limit_value"] == 3
    assert a["detail"]["provider"] == "mock"
    assert a["detail"]["consecutive_failures"] == 3
    assert a["detail"]["threshold"] == 3
    assert a["detail"]["first_failure_at"], "首次失败时间必须落库（持续时长靠它算）"
    assert a["detail"]["last_error_code"] == "upstream_5xx"
    assert a["resolved_at"] is None and a["acknowledged_at"] is None
    assert "3 consecutive failures" in a["message"]

    # 结构化日志：grep provider，或按字段聚合，都能用
    logged = [r for r in caplog.records if r.name == "aigate.upstream"]
    assert logged, "必须有 WARNING 日志"
    last = logged[-1]
    assert getattr(last, "provider", None) == "mock"
    assert getattr(last, "consecutive_failures", None) == 3
    assert getattr(last, "error_code", None) == "upstream_5xx"
    assert getattr(last, "alerting", None) is True

    # 控制台读的就是这个端点
    listed = (await client.get("/admin/v1/alerts", headers=ADMIN)).json()
    assert listed["count"] == 1, listed
    item = listed["items"][0]
    print("GET /admin/v1/alerts item:", json.dumps(item, ensure_ascii=False))
    assert item["scope_type"] == "provider" and item["scope_id"] == "mock"
    assert item["resolved_at"] is None
    assert item["detail"]["last_error_code"] == "upstream_5xx"


async def test_repeated_failures_aggregate_and_an_ack_survives(client, auth_headers):
    """告警风暴就是「同一次故障发 N 条」。这里钉住：永远一条，且确认不会被刷掉。"""
    circuit.configure_breaker(threshold=10, cooldown_s=30.0, backend="memory")

    for _ in range(3):
        await _ok(client, auth_headers)
    rows = await _provider_alerts()
    assert len(rows) == 1 and rows[0]["value"] == 3

    ack = await client.post(f"/admin/v1/alerts/{rows[0]['id']}/ack", headers=ADMIN)
    assert ack.status_code == 200, ack.text

    for _ in range(2):
        await _ok(client, auth_headers)

    rows = await _provider_alerts()
    assert len(rows) == 1, f"同一段故障不许刷第二条：{rows}"
    assert rows[0]["value"] == 5, "计数要继续聚合进同一行"
    assert rows[0]["acknowledged_at"] is not None, "人确认过的时间不能被后续失败清掉"
    print("aggregated row:", rows[0]["value"], rows[0]["message"])


async def test_recovery_resolves_the_row_and_a_new_outage_opens_a_new_one(client, auth_headers):
    # 3 次失败同时把熔断器打开了（同一个阈值）。恢复只可能发生在「半开探测」那一次，
    # 所以把 cooldown 设成 0：这不是绕过熔断，而是模拟 30s 之后的那一次探测请求。
    circuit.configure_breaker(threshold=3, cooldown_s=0.0, backend="memory")
    for _ in range(3):
        await _ok(client, auth_headers)
    before = await _provider_alerts()
    assert len(before) == 1 and before[0]["period_key"] == "open"

    # 换回健康的路由再打一次：mock 成功了
    healed = await client.put(
        "/admin/v1/routes/chat-default",
        headers=ADMIN,
        json={"candidates": [{"provider": "mock", "model": "mock-ok"}]},
    )
    assert healed.status_code == 200, healed.text
    ok = await client.post(
        "/v1/chat/completions", headers=auth_headers, json=_payload("chat-default", "recovered?")
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["x_aigate"]["provider"] == "mock"

    after = await _provider_alerts()
    assert len(after) == 1, after
    row = after[0]
    printable = {**row, "created_at": str(row["created_at"])}
    print("resolved row:", json.dumps(printable, default=str, ensure_ascii=False))
    assert row["resolved_at"] is not None, "恢复必须写 resolved_at"
    assert row["period_key"].startswith("closed:"), row["period_key"]
    assert row["detail"]["resolved_at"] and row["detail"]["duration_s"] is not None
    assert "recovered after" in row["message"]
    assert row["acknowledged_at"] is None, "自动恢复不等于有人看过"

    # 默认视图（待处理）不再显示；带上历史才看得到
    pending = (await client.get("/admin/v1/alerts", headers=ADMIN)).json()
    assert pending["count"] == 0, pending
    history = (await client.get("/admin/v1/alerts?include_acknowledged=true", headers=ADMIN)).json()
    assert any(i["id"] == row["id"] for i in history["items"]), history

    # 第二次故障：period_key 空出来了，应该开新的一行而不是复活旧的
    for _ in range(3):
        await _ok(client, auth_headers)
    again = await _provider_alerts()
    assert len(again) == 2, again
    assert sorted(r["period_key"] for r in again) == sorted(
        ["open", f"closed:{row['id'][:12]}"]
    )


async def test_threshold_is_configurable_and_counts_are_reported(client):
    assert get_settings().upstream_alert_threshold == 3
    for _ in range(4):
        await incidents.note_failure("mock", "upstream_5xx", threshold=5)
    assert await _provider_alerts() == [], "没到阈值不许告警"

    await incidents.note_failure("mock", "upstream_timeout", threshold=5)
    rows = await _provider_alerts()
    assert len(rows) == 1
    assert rows[0]["value"] == 5 and rows[0]["limit_value"] == 5
    assert rows[0]["detail"]["last_error_code"] == "upstream_timeout"


async def test_vendor_4xx_does_not_page_anyone(client, auth_headers):
    """厂商 400 = 它活着并且在正常拒绝我们的请求，那是我们的 bug，不是它挂了。"""
    for _ in range(3):
        resp = await client.post(
            "/v1/chat/completions", headers=auth_headers, json=_payload("bad-request")
        )
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "upstream_4xx"
    assert await _provider_alerts() == []


async def test_streaming_path_counts_failures_too(client, auth_headers):
    """非流式与流式是两条代码路径；告警不能只挂在其中一条上。"""
    for _ in range(3):
        resp = await client.post(
            "/v1/chat/completions", headers=auth_headers, json={**_payload("stream-fail"), "stream": True}
        )
        assert resp.status_code == 200, resp.text

    rows = await _provider_alerts()
    assert len(rows) == 1 and rows[0]["scope_id"] == "mock", rows
    assert rows[0]["detail"]["last_error_code"] == "upstream_5xx"


async def test_manual_resolve_closes_a_stale_incident(client):
    """开告警的副本先挂了，行就永远开着。这条是那个手工出口。"""
    for _ in range(3):
        await incidents.note_failure("mock-alt", "upstream_timeout", threshold=3)
    rows = await _provider_alerts()
    assert len(rows) == 1

    resp = await client.post(f"/admin/v1/alerts/{rows[0]['id']}/resolve", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    assert resp.json()["resolved_at"] is not None
    assert resp.json()["period_key"].startswith("closed:")

    # 关掉之后，下一次连续失败必须能开一条新的（否则告警永久失聪）
    for _ in range(3):
        await incidents.note_failure("mock-alt", "upstream_timeout", threshold=3)
    assert len(await _provider_alerts()) == 2


async def test_metering_still_records_the_degraded_call(client, auth_headers):
    """告警不能把请求路径搞坏：降级请求照常记账，钱没丢。"""
    for i in range(3):
        await _ok(client, auth_headers, text=f"m-{i}")
    async with get_session_factory()() as session:
        rows = (await session.execute(select(UsageRecord))).scalars().all()
    assert len(rows) == 3
    assert {r.status for r in rows} == {"fallback"}
    assert {r.provider for r in rows} == {"mock-alt"}


@pytest.mark.parametrize("provider", ["mock"])
async def test_alert_scope_is_the_provider_not_the_project(client, auth_headers, provider):
    for _ in range(3):
        await client.post("/v1/chat/completions", headers=auth_headers, json=_payload())
    rows = await _provider_alerts()
    assert rows[0]["scope_id"] == provider
    assert rows[0]["scope_type"] == "provider"