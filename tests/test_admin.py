"""Control-plane API: auth, aggregate shapes, key lifecycle."""

from __future__ import annotations

from aigate.config import get_settings

# Read the configured token instead of hardcoding one: a developer with a .env that has
# a random admin token must still be able to run the suite.
ADMIN = {"X-Admin-Token": get_settings().admin_token}


async def test_admin_requires_token(client):
    resp = await client.get("/admin/v1/overview")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "gateway.login_required"


async def test_overview_shape(client, auth_headers, chat_payload):
    await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)

    resp = await client.get("/admin/v1/overview?days=7", headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()

    assert body["today"]["requests"] == 1
    assert body["today"]["success_rate"] == 100.0
    assert len(body["series"]) == 7, "the 7-day chart must have 7 buckets, zero days included"
    assert sum(day["requests"] for day in body["series"]) == 1
    assert body["top_models"][0]["model"] == "mock-ok"


async def test_overview_with_no_traffic_reports_unknown_not_perfect(client):
    """空窗口不能说"100% 成功 / 0 ms"。

    审计复现：空库 `curl .../overview?days=7` 返回
    `success_rate: 100.0, avg_latency_ms: 0.0, p95_latency_ms: 0`，前端第一屏就写着
    "成功率 100.0% / P95 0 ms"。没有调用时这些量是**未知**，必须是 null +
    has_data=false；计数（requests/errors）是真 0，保留数字。
    """
    body = (await client.get("/admin/v1/overview?days=7", headers=ADMIN)).json()

    today = body["today"]
    assert today["requests"] == 0, "这条用例的前提是空库"
    assert today["has_data"] is False
    assert today["success_rate"] is None
    assert today["avg_latency_ms"] is None
    assert today["p95_latency_ms"] is None
    assert today["cache_hit_rate"] is None

    assert body["window"]["requests"] == 0
    assert body["window"]["errors"] == 0
    assert body["window"]["has_data"] is False
    assert body["window"]["success_rate"] is None
    assert body["window"]["p95_latency_ms"] is None


async def test_overview_with_traffic_still_computes_rates(client, auth_headers, chat_payload):
    """null 不能变成"永远未知"：有一条成功记录时仍然算出 100 / 真实延迟。"""
    await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)

    body = (await client.get("/admin/v1/overview?days=7", headers=ADMIN)).json()
    today = body["today"]
    assert today["has_data"] is True
    assert today["success_rate"] == 100.0
    assert today["avg_latency_ms"] is not None
    assert today["p95_latency_ms"] is not None


async def test_usage_list_and_filters(client, auth_headers, chat_payload):
    await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)

    resp = await client.get("/admin/v1/usage?limit=10", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json()["count"] == 1

    cached = await client.get("/admin/v1/usage?only_cached=true", headers=ADMIN)
    assert cached.json()["count"] == 0


async def test_trace_detail_and_404(client, auth_headers, chat_payload):
    resp = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    request_id = resp.json()["x_aigate"]["request_id"]

    detail = await client.get(f"/admin/v1/traces/{request_id}", headers=ADMIN)
    assert detail.status_code == 200
    body = detail.json()
    assert body["request_id"] == request_id
    assert body["usage"]["model_used"] == "mock-ok"
    assert isinstance(body["stage_timings"], dict)

    missing = await client.get("/admin/v1/traces/req_does_not_exist", headers=ADMIN)
    assert missing.status_code == 404


async def test_key_lifecycle(client):
    projects = (await client.get("/admin/v1/projects", headers=ADMIN)).json()["items"]
    project_id = projects[0]["id"]

    created = await client.post(f"/admin/v1/keys?project_id={project_id}&name=console", headers=ADMIN)
    assert created.status_code == 201
    raw = created.json()["api_key"]
    assert raw.startswith("sk-ag-")

    # the plaintext is never readable again
    listed = (await client.get("/admin/v1/keys", headers=ADMIN)).json()["items"]
    assert raw not in str(listed)

    revoked = await client.post(f"/admin/v1/keys/{created.json()['id']}/revoke", headers=ADMIN)
    assert revoked.json()["status"] == "revoked"


async def test_models_endpoint_lists_prices_and_policies(client):
    body = (await client.get("/admin/v1/models", headers=ADMIN)).json()
    assert any(p["model"] == "mock-ok" for p in body["prices"])
    assert any(p["match_model"] == "chat-default" for p in body["policies"])
