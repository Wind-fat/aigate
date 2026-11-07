"""控制面读端点：值班最常打开的那几个列表。

这三个端点是 scripts/audit.py 在自检里发现的缺口——不是逻辑错，是**没有**：
详情页有、列表页没有。值班的第一句话通常是「最近哪些请求失败了」，
靠翻日志回答不了一个内部团队。
"""

from __future__ import annotations

from httpx import AsyncClient

from aigate import provider_config
from aigate.config import get_settings
from aigate.core import circuit
from aigate.db import get_session_factory
from aigate.metering_queue import reset_writer

ADMIN = {"X-Admin-Token": get_settings().admin_token}

PAYLOAD = {
    "model": "chat-default",
    "messages": [{"role": "user", "content": "列表端点自检"}],
    "temperature": 0,
}


async def test_traces_list_returns_recent_calls(client: AsyncClient, auth_headers):
    created = await client.post("/v1/chat/completions", json=PAYLOAD, headers=auth_headers)
    assert created.status_code == 200
    request_id = created.json()["x_aigate"]["request_id"]

    listed = await client.get("/admin/v1/traces?limit=5", headers=ADMIN)
    assert listed.status_code == 200, listed.text
    ids = [item["request_id"] for item in listed.json()["items"]]
    assert request_id in ids
    row = next(i for i in listed.json()["items"] if i["request_id"] == request_id)
    assert row["route"] == "chat-default"
    assert row["provider"] == "mock"


async def test_traces_list_can_filter_by_status(client: AsyncClient, auth_headers):
    await client.post("/v1/chat/completions", json=PAYLOAD, headers=auth_headers)
    ok_only = await client.get("/admin/v1/traces?status=ok", headers=ADMIN)
    assert ok_only.status_code == 200
    assert all(item["status"] == "ok" for item in ok_only.json()["items"])


async def test_routes_list_exposes_the_candidate_chain(client: AsyncClient):
    listed = await client.get("/admin/v1/routes", headers=ADMIN)
    assert listed.status_code == 200
    names = [r["match_model"] for r in listed.json()["items"]]
    assert "chat-default" in names
    route = next(r for r in listed.json()["items"] if r["match_model"] == "chat-default")
    assert route["candidates"], "候选链不能是空的"
    assert "shadow" in route


async def test_traces_list_is_scoped_to_admins(client: AsyncClient):
    login = await client.post(
        "/console/v1/login", json={"email": "viewer@example.com", "password": "read-only-password"}
    )
    cookies = {"Cookie": f"aigate_session={login.cookies.get('aigate_session')}"}
    # viewer 只有 trace:own，而"所有人的调用链"是 trace:all
    assert (await client.get("/admin/v1/traces", headers=cookies)).status_code == 403


async def test_ops_health_reports_unknown_when_breaker_snapshot_fails(
    client: AsyncClient, monkeypatch
):
    """熔断快照失败 ≠ 全部正常（P0-2）。

    审计复现：`breaker.snapshot()` 抛错时后端只把异常塞进 `breaker.state`，
    每个 provider 的 `circuit` 回落成 `{"open": false}`，前端于是渲染
    "熔断：全部正常"。未知必须是未知：circuit=None + breaker.error 有值。
    """

    class BrokenBreaker:
        name = "redis"
        threshold = 3
        cooldown_s = 30.0

        async def snapshot(self) -> dict:
            raise RuntimeError("redis down")

    monkeypatch.setattr(circuit, "get_breaker", lambda: BrokenBreaker())

    # 供应商行由启动钩子 seed；测试环境不跑 lifespan，这里手动补上，
    # 否则 providers=[] 让"每行都未知"无从验证。
    async with get_session_factory()() as session:
        await provider_config.ensure_seeds(session)
    provider_config.invalidate()

    resp = await client.get("/admin/v1/ops-health", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert "redis down" in body["breaker"]["error"]
    assert "redis down" in body["breaker"]["state"]["__error__"]
    assert body["providers"], "空 providers 让这条用例失去意义"
    assert all(p["circuit"] is None for p in body["providers"]), (
        "快照失败时每一行都必须是未知，不能回落到 open=false"
    )


async def test_ops_health_metering_disabled_is_explicit(client: AsyncClient):
    """记账未启用 ≠ 队列 0（P1-5）：必须显式 enabled=false，且不给 queued=0。"""
    # conftest 会配置一个写者；这里主动拆掉，复现"writer 未配置"的部署形态。
    reset_writer()

    body = (await client.get("/admin/v1/ops-health", headers=ADMIN)).json()
    assert body["metering"]["enabled"] is False
    assert "queued" not in body["metering"]
    assert "written" not in body["metering"]


async def test_ops_health_metering_enabled_has_counters(client: AsyncClient):
    """对照组：写者存在时仍然给出真实计数（null 改造不能把正常路径也变成未知）。"""
    body = (await client.get("/admin/v1/ops-health", headers=ADMIN)).json()
    assert body["metering"]["enabled"] is True
    assert isinstance(body["metering"]["queued"], int)
    assert isinstance(body["metering"]["written"], int)


async def test_audit_can_be_exported_as_csv(client: AsyncClient):
    """审计要能"带走"：交接、对账、给安全团队看，都不该让人一屏屏截图。"""
    # 先制造一条审计（签发 key 是写操作）
    projects = (await client.get("/admin/v1/projects", headers=ADMIN)).json()["items"]
    await client.post(
        f"/admin/v1/keys?project_id={projects[0]['id']}&name=audit-csv-probe", headers=ADMIN
    )

    resp = await client.get("/admin/v1/audit.csv?limit=50", headers=ADMIN)
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]
    rows = resp.headers.get("x-export-rows")
    assert rows is not None and int(rows) >= 1

    body = resp.text
    assert body.startswith(
        "created_at,actor,role,action,target,method,path,status_code,ip,request_id"
    )
    assert "key.create" in body
    # 审计行里不能有密钥或 prompt
    assert "sk-ag-" not in body


async def test_audit_export_requires_the_capability(client: AsyncClient):
    login = await client.post(
        "/console/v1/login", json={"email": "viewer@example.com", "password": "read-only-password"}
    )
    cookies = {"Cookie": f"aigate_session={login.cookies.get('aigate_session')}"}
    assert (await client.get("/admin/v1/audit.csv", headers=cookies)).status_code == 403
