"""prompt 版本库：版本可追溯、切换可回滚、缓存与账单能对上。"""

from __future__ import annotations

from httpx import AsyncClient

from aigate.config import get_settings

ADMIN = {"X-Admin-Token": get_settings().admin_token}

BASE = {
    "name": "support-reply",
    "version": "v1",
    "content": "你是客服，回答要短。用户：{{q}}",
    "notes": "初版",
}


async def _create(client: AsyncClient, **overrides):
    body = {**BASE, **overrides}
    return await client.post("/admin/v1/prompts", json=body, headers=ADMIN)


async def test_create_and_list_versions(client: AsyncClient):
    first = await _create(client)
    assert first.status_code == 201, first.text
    assert first.json()["item"]["active"] is False  # 新建默认不生效

    second = await _create(client, version="v2", content="你是客服，回答要短，且必须给下一步。")
    assert second.status_code == 201

    listed = await client.get("/admin/v1/prompts?name=support-reply", headers=ADMIN)
    versions = sorted(i["version"] for i in listed.json()["items"])
    assert versions == ["v1", "v2"]


async def test_duplicate_version_is_refused(client: AsyncClient):
    assert (await _create(client)).status_code == 201
    again = await _create(client)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "gateway.prompt_exists"


async def test_activating_one_version_deactivates_the_others(client: AsyncClient):
    v1 = (await _create(client)).json()["item"]
    v2 = (await _create(client, version="v2", content="第二版")).json()["item"]

    await client.post(f"/admin/v1/prompts/{v1['id']}/activate", headers=ADMIN)
    await client.post(f"/admin/v1/prompts/{v2['id']}/activate", headers=ADMIN)

    listed = (await client.get("/admin/v1/prompts?name=support-reply", headers=ADMIN)).json()["items"]
    active = [i["version"] for i in listed if i["active"]]
    assert active == ["v2"], "同一名字只能有一个当前版本"

    # 回滚
    await client.post(f"/admin/v1/prompts/{v1['id']}/activate", headers=ADMIN)
    active = [
        i["version"]
        for i in (await client.get("/admin/v1/prompts?name=support-reply", headers=ADMIN)).json()["items"]
        if i["active"]
    ]
    assert active == ["v1"]


async def test_active_alias_returns_full_content(client: AsyncClient):
    created = (await _create(client, version="v9")).json()["item"]
    await client.post(f"/admin/v1/prompts/{created['id']}/activate", headers=ADMIN)
    resp = await client.get("/admin/v1/prompts/support-reply/active", headers=ADMIN)
    assert resp.status_code == 200
    assert "{{q}}" in resp.json()["item"]["content"]


async def test_list_is_truncated_but_detail_is_full(client: AsyncClient):
    long_text = "x" * 500
    created = (await _create(client, content=long_text)).json()["item"]
    assert created["chars"] == 500
    listed = (await client.get("/admin/v1/prompts", headers=ADMIN)).json()["items"][0]
    assert len(listed["content"]) <= 200
    detail = (await client.get(f"/admin/v1/prompts/{created['id']}", headers=ADMIN)).json()["item"]
    assert len(detail["content"]) == 500


async def test_viewer_neither_reads_nor_writes_prompts(client: AsyncClient):
    """prompt 里常常有内部话术、客户名单、风控规则 —— 它是配置，不是"用量"。

    所以开发者看得到自己的调用明细和 trace，但看不到这一页；要看请找管理员。
    """
    login = await client.post(
        "/console/v1/login", json={"email": "viewer@example.com", "password": "read-only-password"}
    )
    cookies = {"Cookie": f"aigate_session={login.cookies.get('aigate_session')}"}
    assert (await client.get("/admin/v1/prompts", headers=cookies)).status_code == 403
    denied = await client.post("/admin/v1/prompts", json=BASE, headers=cookies)
    assert denied.status_code == 403