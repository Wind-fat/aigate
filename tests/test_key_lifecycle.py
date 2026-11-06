"""Key 的生命周期：签发、归属、过期、吊销，以及"谁能动谁的 key"。

这一组测试的起点是一个真实缺陷：`POST /admin/v1/keys` 早期没有 `actor` 依赖，
于是任何登录用户（包括只读的开发者）都能给任意项目签发 key、也能吊销任意 key。
接口返回 201，看起来一切正常——这类问题只能靠"用低权限账号试一遍"发现。
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from aigate.config import get_settings
from aigate.db import get_session_factory
from aigate.models import ApiKey, Project, Team, utcnow

DEV = {"X-Admin-Token": get_settings().admin_token}
VIEWER_EMAIL = "viewer@example.com"
VIEWER_PASSWORD = "read-only-password"


async def _project_id() -> str:
    async with get_session_factory()() as session:
        return (await session.execute(select(Project))).scalars().first().id


async def _cookie_for(client, email: str = VIEWER_EMAIL) -> dict[str, str]:
    """用真实登录换 cookie，而不是伪造 session —— 伪造的 cookie 测不到权限规则。"""
    resp = await client.post(
        "/console/v1/login", json={"email": email, "password": VIEWER_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    token = resp.cookies.get("aigate_session")
    assert token
    return {"Cookie": f"aigate_session={token}"}


async def test_issue_key_records_who_issued_it(client):
    project_id = await _project_id()
    resp = await client.post(
        f"/admin/v1/keys?project_id={project_id}&name=order-svc&environment=dev"
        "&expires_in_days=30&description=订单服务",
        headers=DEV,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["api_key"].startswith("sk-ag-")
    assert body["environment"] == "dev"
    assert body["expires_at"] is not None

    async with get_session_factory()() as session:
        key = (await session.execute(select(ApiKey).where(ApiKey.id == body["id"]))).scalar_one()
        assert key.created_by_email == "token:admin"
        assert key.description == "订单服务"
        assert key.expires_at is not None


async def test_prod_key_must_expire(client):
    project_id = await _project_id()
    resp = await client.post(
        f"/admin/v1/keys?project_id={project_id}&name=prod-svc&environment=prod", headers=DEV
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "gateway.key_prod_needs_expiry"


async def test_duplicate_active_name_is_refused(client):
    project_id = await _project_id()
    first = await client.post(f"/admin/v1/keys?project_id={project_id}&name=dup", headers=DEV)
    assert first.status_code == 201
    second = await client.post(f"/admin/v1/keys?project_id={project_id}&name=dup", headers=DEV)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "gateway.key_name_taken"


async def test_model_allow_list_is_trimmed(client):
    """`a, b` 里的空格会让白名单永远不匹配，表现为「key 用不了」。"""
    project_id = await _project_id()
    resp = await client.post(
        f"/admin/v1/keys?project_id={project_id}&name=trimmed&allowed_models=chat-default,%20chat-cheap",
        headers=DEV,
    )
    assert resp.status_code == 201
    async with get_session_factory()() as session:
        key = (await session.execute(select(ApiKey).where(ApiKey.id == resp.json()["id"]))).scalar_one()
        assert key.allowed_models == ["chat-default", "chat-cheap"]


async def test_expired_key_is_rejected_with_a_specific_code(client):
    project_id = await _project_id()
    issued = await client.post(
        f"/admin/v1/keys?project_id={project_id}&name=short-lived&expires_in_days=1", headers=DEV
    )
    raw = issued.json()["api_key"]

    # 把过期时间挪到过去，模拟"三个月后"
    async with get_session_factory()() as session:
        key = (await session.execute(select(ApiKey).where(ApiKey.id == issued.json()["id"]))).scalar_one()
        key.expires_at = utcnow() - timedelta(seconds=1)
        await session.commit()

    call = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw}"},
        json={
            "model": "chat-default",
            "messages": [{"role": "user", "content": "still there?"}],
            "temperature": 0,
        },
    )
    assert call.status_code == 401
    # 关键不是 401，而是能指向动作的错误码：invalid_key 会让人去查拼写
    assert call.json()["error"]["code"] == "gateway.key_expired"


async def test_viewer_can_issue_for_their_own_project_only(client):
    """开发者能自助签发（否则只能找管理员贴 key 到聊天里），但只能给自己项目签。"""
    cookies = await _cookie_for(client)
    me = (await client.get("/console/v1/me", headers=cookies)).json()
    own = me["project_scope"][0]

    # 另建一个项目，确保"别人的项目"真的存在于库里
    async with get_session_factory()() as session:
        team = (await session.execute(select(Team))).scalars().first()
        other_project = Project(team_id=team.id, name="Other", slug="other", rpm_limit=60)
        session.add(other_project)
        await session.commit()
        other = other_project.id

    mine = await client.post(f"/admin/v1/keys?project_id={own}&name=dev-self", headers=cookies)
    assert mine.status_code == 201, mine.text
    assert mine.json()["created_by_email"] == VIEWER_EMAIL

    theirs = await client.post(f"/admin/v1/keys?project_id={other}&name=dev-steal", headers=cookies)
    assert theirs.status_code == 403
    assert theirs.json()["error"]["code"] == "gateway.key_scope_denied"


async def test_viewer_can_revoke_only_their_own_key(client):
    cookies = await _cookie_for(client)
    own = (await client.get("/console/v1/me", headers=cookies)).json()["project_scope"][0]

    mine = await client.post(f"/admin/v1/keys?project_id={own}&name=dev-own", headers=cookies)
    assert mine.status_code == 201
    revoked = await client.post(
        f"/admin/v1/keys/{mine.json()['id']}/revoke?reason=泄露", headers=cookies
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"

    # 管理员签的 key，开发者不能吊销——否则只读角色成了打扰工具
    admin_key = await client.post(f"/admin/v1/keys?project_id={own}&name=admin-issued", headers=DEV)
    assert admin_key.status_code == 201
    denied = await client.post(f"/admin/v1/keys/{admin_key.json()['id']}/revoke", headers=cookies)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "gateway.key_revoke_denied"


async def test_revoke_records_reason_and_actor(client):
    project_id = await _project_id()
    issued = await client.post(f"/admin/v1/keys?project_id={project_id}&name=audit-me", headers=DEV)
    resp = await client.post(
        f"/admin/v1/keys/{issued.json()['id']}/revoke?reason=已迁移到KMS", headers=DEV
    )
    assert resp.status_code == 200
    assert resp.json()["revoked_reason"] == "已迁移到KMS"
    assert resp.json()["revoked_by"] == "token:admin"

    again = await client.post(f"/admin/v1/keys/{issued.json()['id']}/revoke", headers=DEV)
    assert again.status_code == 200
    assert again.json()["already"] is True


async def test_list_keys_carries_ownership_and_idle_days(client):
    project_id = await _project_id()
    await client.post(
        f"/admin/v1/keys?project_id={project_id}&name=listed&environment=staging"
        "&expires_in_days=7&description=链路压测",
        headers=DEV,
    )
    resp = await client.get("/admin/v1/keys?q=listed", headers=DEV)
    assert resp.status_code == 200
    row = next(k for k in resp.json()["items"] if k["name"] == "listed")
    assert row["environment"] == "staging"
    assert row["description"] == "链路压测"
    assert row["created_by_email"] == "token:admin"
    assert row["expires_at"] is not None
    assert row["idle_days"] is not None


async def test_key_list_is_filterable_by_status(client):
    project_id = await _project_id()
    issued = await client.post(f"/admin/v1/keys?project_id={project_id}&name=to-revoke", headers=DEV)
    await client.post(f"/admin/v1/keys/{issued.json()['id']}/revoke?reason=test", headers=DEV)
    resp = await client.get("/admin/v1/keys?status=revoked", headers=DEV)
    assert resp.status_code == 200
    assert all(k["status"] == "revoked" for k in resp.json()["items"])