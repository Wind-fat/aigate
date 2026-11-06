"""控制面读端点：值班最常打开的那几个列表。

这三个端点是 scripts/audit.py 在自检里发现的缺口——不是逻辑错，是**没有**：
详情页有、列表页没有。值班的第一句话通常是「最近哪些请求失败了」，
靠翻日志回答不了一个内部团队。
"""

from __future__ import annotations

from httpx import AsyncClient

from aigate.config import get_settings

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
