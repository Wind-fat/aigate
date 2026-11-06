"""成员管理：把「谁能进、能看哪个项目、还能不能进」变成可操作的动作。

起因是一个真实的坑：本仓库里有两个 viewer 账号的 `project_scope` 是空的。它们能登录，
但看不到任何数据、也签不了 key —— 而控制台当时没有任何地方能把项目补给他们。
一个管理员无法修复的状态，等于一个坏掉的产品，不是权限策略。
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select

from aigate.db import get_session_factory
from aigate.models import ConsoleUser, Project

OWNER = {"email": "owner@example.com", "password": "correct-horse-battery"}
ADMIN = {"email": "admin@example.com", "password": "plain-admin-password"}
VIEWER = {"email": "viewer@example.com", "password": "read-only-password"}


async def _login(client: AsyncClient, who: dict[str, str]) -> dict[str, str]:
    resp = await client.post("/console/v1/login", json=who)
    assert resp.status_code == 200, resp.text
    return {"Cookie": f"aigate_session={resp.cookies.get('aigate_session')}"}


async def _user_id(email: str) -> str:
    async with get_session_factory()() as session:
        user = (
            await session.execute(select(ConsoleUser).where(ConsoleUser.email == email))
        ).scalar_one()
        return user.id


async def _project_ids() -> list[str]:
    async with get_session_factory()() as session:
        return [p.id for p in (await session.execute(select(Project))).scalars().all()]


async def test_admin_can_assign_a_project_to_a_scopeless_viewer(client):
    """这就是「开发者签不了 key」的根因与解法。"""
    viewer_id = await _user_id("viewer@example.com")
    project_id = (await _project_ids())[0]

    # 先制造坏状态：viewer 没有可见项目
    async with get_session_factory()() as session:
        user = (await session.execute(select(ConsoleUser).where(ConsoleUser.id == viewer_id))).scalar_one()
        user.project_scope = []
        await session.commit()

    owner = await _login(client, OWNER)
    viewer = await _login(client, VIEWER)
    denied = await client.post(f"/admin/v1/keys?project_id={project_id}&name=before", headers=viewer)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "gateway.key_scope_denied"

    fixed = await client.put(
        f"/console/v1/users/{viewer_id}",
        json={"project_scope": [project_id]},
        headers=owner,
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["project_scope"] == [project_id]

    # 还是同一个会话，不需要重新登录
    allowed = await client.post(f"/admin/v1/keys?project_id={project_id}&name=after", headers=viewer)
    assert allowed.status_code == 201, allowed.text


async def test_viewer_cannot_edit_members(client):
    owner_id = await _user_id("owner@example.com")
    viewer = await _login(client, VIEWER)
    resp = await client.put(
        f"/console/v1/users/{owner_id}", json={"role": "viewer"}, headers=viewer
    )
    assert resp.status_code == 403


async def test_admin_cannot_touch_an_owner(client):
    owner_id = await _user_id("owner@example.com")
    admin = await _login(client, ADMIN)
    resp = await client.put(
        f"/console/v1/users/{owner_id}", json={"project_scope": []}, headers=admin
    )
    assert resp.status_code == 403
    # 也不能把别人提成 owner —— 提权是一个 POST 的事，所以这条必须是 owner 专属
    viewer_id = await _user_id("viewer@example.com")
    promote = await client.put(
        f"/console/v1/users/{viewer_id}", json={"role": "owner"}, headers=admin
    )
    assert promote.status_code == 403


async def test_nobody_can_demote_or_disable_themselves(client):
    owner_id = await _user_id("owner@example.com")
    owner = await _login(client, OWNER)
    demote = await client.put(
        f"/console/v1/users/{owner_id}", json={"role": "admin"}, headers=owner
    )
    assert demote.status_code == 400
    assert demote.json()["error"]["code"] == "gateway.self_lockout"

    off = await client.put(
        f"/console/v1/users/{owner_id}", json={"status": "disabled"}, headers=owner
    )
    assert off.status_code == 400


async def test_viewer_needs_at_least_one_project(client):
    viewer_id = await _user_id("viewer@example.com")
    owner = await _login(client, OWNER)
    resp = await client.put(
        f"/console/v1/users/{viewer_id}", json={"project_scope": []}, headers=owner
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "gateway.scope_required"


async def test_unknown_project_id_is_rejected(client):
    """拼错的项目 id 存进 scope，效果是这个人静默地什么都看不到。"""
    viewer_id = await _user_id("viewer@example.com")
    owner = await _login(client, OWNER)
    resp = await client.put(
        f"/console/v1/users/{viewer_id}",
        json={"project_scope": ["does-not-exist"]},
        headers=owner,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "gateway.unknown_project"


async def test_password_reset_by_owner_takes_effect(client):
    viewer_id = await _user_id("viewer@example.com")
    owner = await _login(client, OWNER)
    resp = await client.put(
        f"/console/v1/users/{viewer_id}",
        json={"password": "brand-new-password"},
        headers=owner,
    )
    assert resp.status_code == 200, resp.text
    assert "password reset" in resp.json()["changed"]

    old = await client.post("/console/v1/login", json=VIEWER)
    assert old.status_code == 401
    new = await client.post(
        "/console/v1/login",
        json={"email": VIEWER["email"], "password": "brand-new-password"},
    )
    assert new.status_code == 200


async def test_demotion_takes_effect_on_the_next_request(client):
    admin_id = await _user_id("admin@example.com")
    owner = await _login(client, OWNER)
    admin = await _login(client, ADMIN)

    before = await client.get("/admin/v1/keys", headers=admin)
    assert before.status_code == 200

    await client.put(f"/console/v1/users/{admin_id}", json={"role": "viewer"}, headers=owner)

    after = await client.get("/admin/v1/keys", headers=admin)
    # viewer 没有 keys:read，只能看自己的 key —— 这里项目范围为空，所以列表是空的而不是 403
    assert after.status_code in (200, 403)
    if after.status_code == 200:
        assert after.json()["items"] == []


async def test_change_is_recorded_with_diff(client):
    viewer_id = await _user_id("viewer@example.com")
    owner = await _login(client, OWNER)
    project_id = (await _project_ids())[0]
    resp = await client.put(
        f"/console/v1/users/{viewer_id}",
        json={"role": "finance", "project_scope": [project_id]},
        headers=owner,
    )
    assert resp.status_code == 200
    changed = resp.json()["changed"]
    assert any("role viewer->finance" in c for c in changed)
    assert any("scope=" in c for c in changed)

    again = await client.put(f"/console/v1/users/{viewer_id}", json={}, headers=owner)
    assert again.status_code == 200
    assert again.json()["changed"] == []