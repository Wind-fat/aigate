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
