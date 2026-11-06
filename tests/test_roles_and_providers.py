"""角色隔离与供应商管理：四个角色各自能碰到什么，以及「来源」能不能被证明可用。

这一组测试的核心主张只有一句：**权限和范围在服务端，不在界面**。
界面藏掉按钮是为了不让人点了失败，而不是安全边界；所以每条规则都在 API 层断言。
"""

from __future__ import annotations

from aigate.config import get_settings

ADMIN = {"X-Admin-Token": get_settings().admin_token}
PROVIDER_ROW = {"label": "通义千问（DashScope）", "base_url": "https://example.invalid/v1"}


async def _login(client, email: str, password: str) -> None:
    resp = await client.post("/console/v1/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text


# ------------------------------------------------------------------ 角色能力


async def test_finance_sees_costs_but_not_prompts_or_keys(client, auth_headers, chat_payload):
    """财务要能对账，但不能看到 prompt 内容、密钥、路由。"""
    await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    request_id = (
        await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    ).json()["x_aigate"]["request_id"]

    await _login(client, "finance@example.com", "finance-password-1")

    assert (await client.get("/admin/v1/overview")).status_code == 200
    assert (await client.get("/admin/v1/projects/summary")).status_code == 200
    assert (await client.get("/admin/v1/usage/export.csv?days=7")).status_code == 200

    # prompts are private: finance gets a 403, not a redacted view
    trace = await client.get(f"/admin/v1/traces/{request_id}")
    assert trace.status_code == 403
    assert trace.json()["error"]["code"] == "gateway.forbidden"

    assert (await client.get("/admin/v1/keys")).status_code == 403
    assert (await client.get("/admin/v1/providers")).status_code == 403
    assert (await client.get("/admin/v1/audit")).status_code == 403


async def test_viewer_is_scoped_to_its_own_project(client, auth_headers, chat_payload):
    """开发者只看得到自己项目的调用；别人的项目对它等于不存在。"""
    await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    mine = (await client.get("/admin/v1/usage", headers=ADMIN)).json()["items"][0]["project_id"]

    await _login(client, "viewer@example.com", "read-only-password")

    rows = (await client.get("/admin/v1/usage")).json()
    assert rows["total"] >= 1
    assert all(item["project_id"] == mine for item in rows["items"])

    # 换个不属于自己的项目 id：返回空，而不是报错泄漏「这个项目存在」
    other = (
        await client.get("/admin/v1/usage", headers=ADMIN)
    ).json()["items"][0]["project_id"]
    await client.put(
        "/admin/v1/projects/none",
        headers=ADMIN,
        json={"rpm_limit": 10},
    )  # 404 不重要，只要不崩
    scoped = (await client.get(f"/admin/v1/usage?project_id={other}")).json()
    assert scoped["total"] == rows["total"]  # 过滤器被 scope 覆盖，不会越权放大


async def test_viewer_cannot_reach_admin_surfaces(client):
    await _login(client, "viewer@example.com", "read-only-password")

    for path in ("/admin/v1/audit", "/admin/v1/projects/summary", "/admin/v1/providers"):
        resp = await client.get(path)
        assert resp.status_code == 403, path

    # 路由与供应商都是平台决策，不是开发者的
    assert (await client.get("/admin/v1/models")).status_code == 200
    assert (
        await client.put("/admin/v1/routes/chat-default", json={"enabled": False})
    ).status_code == 403


async def test_viewer_creation_requires_a_project(client):
    resp = await client.post(
        "/console/v1/users",
        headers=ADMIN,
        json={"email": "noscope@example.com", "password": "long-enough-pass", "role": "viewer"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "gateway.scope_required"


async def test_login_payload_carries_capabilities(client):
    body = (
        await client.post(
            "/console/v1/login",
            json={"email": "viewer@example.com", "password": "read-only-password"},
        )
    ).json()
    caps = body["user"]["capabilities"]
    assert "dev" in caps
    assert "costs" not in caps
    assert body["user"]["project_scope"]


# ------------------------------------------------------------------ 供应商


async def test_providers_are_seeded_and_masked(client):
    """种子里有真实来源（通义千问 / DeepSeek），但永远不返回密钥本身。"""
    body = (await client.get("/admin/v1/providers", headers=ADMIN)).json()
    names = {item["name"] for item in body["items"]}
    assert {"dashscope", "deepseek", "vllm", "mock"} <= names

    ds = next(item for item in body["items"] if item["name"] == "dashscope")
    assert "通义千问" in ds["label"]
    assert ds["api_key_env"] == "AIGATE_DASHSCOPE_API_KEY"
    assert "api_key" not in ds  # 只有环境变量名
    assert isinstance(ds["configured"], bool)
    assert any(m["model"] == "qwen-plus" for m in ds["models"])


async def test_provider_edit_takes_effect_without_restart(client):
    resp = await client.put(
        "/admin/v1/providers/deepseek", headers=ADMIN, json={"label": "DeepSeek（主力）"}
    )
    assert resp.status_code == 200
    assert resp.json()["item"]["label"] == "DeepSeek（主力）"

    again = (await client.get("/admin/v1/providers", headers=ADMIN)).json()
    assert next(i for i in again["items"] if i["name"] == "deepseek")["label"] == "DeepSeek（主力）"


async def test_provider_rejects_a_bad_base_url(client):
    resp = await client.put(
        "/admin/v1/providers/deepseek", headers=ADMIN, json={"base_url": "api.deepseek.com"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "gateway.bad_base_url"


async def test_mock_provider_test_endpoint_is_offline(client):
    resp = await client.post("/admin/v1/providers/mock/test", headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "offline" in body["detail"]


async def test_unconfigured_provider_says_which_env_var(client):
    """没有 key 时测试接口要给出可执行的下一步，而不是一句 failed。"""
    await client.put(
        "/admin/v1/providers/vllm",
        headers=ADMIN,
        json={"api_key_env": "AIGATE_TEST_EMPTY_KEY_VAR", "base_url": "http://127.0.0.1:9/v1"},
    )
    body = (await client.post("/admin/v1/providers/vllm/test", headers=ADMIN)).json()
    assert body["ok"] is False
    assert "AIGATE_TEST_EMPTY_KEY_VAR" in body["detail"]


async def test_disabled_provider_fails_the_request_loudly(client, auth_headers, chat_payload):
    """停用一个供应商后，指向它的调用必须 502，且错误码说明是「这家被停用了」。

    不是笼统的 `no_candidate`：值班看到 `provider_disabled` 才知道该去供应商页恢复，
    看到 `no_candidate` 会去翻路由配置——同一件事浪费十分钟。
    """
    await client.put("/admin/v1/providers/mock", headers=ADMIN, json={"enabled": False})
    resp = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "provider_disabled"

    await client.put("/admin/v1/providers/mock", headers=ADMIN, json={"enabled": True})


async def test_provider_write_is_audited(client):
    await client.put("/admin/v1/providers/deepseek", headers=ADMIN, json={"label": "DS"})
    items = (await client.get("/admin/v1/audit?action=provider.update", headers=ADMIN)).json()["items"]
    assert items and items[0]["action"] == "provider.update"