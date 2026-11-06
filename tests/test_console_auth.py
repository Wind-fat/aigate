"""Console accounts: password hashing, signed sessions, role enforcement, audit trail."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select

from aigate.config import get_settings
from aigate.console_auth import hash_password, sign_session, verify_password, verify_session
from aigate.db import get_session_factory
from aigate.models import AuditLog

SECRET = "test-session-secret"
ADMIN_TOKEN = {"X-Admin-Token": get_settings().admin_token}


# ---------------------------------------------------------------- password (unit)


def test_password_hash_roundtrip():
    stored = hash_password("correct-horse-battery")
    assert stored.startswith("scrypt$")
    assert "correct-horse-battery" not in stored
    assert verify_password("correct-horse-battery", stored)
    assert not verify_password("wrong-password", stored)


def test_same_password_hashes_differently():
    a = hash_password("same-password-twice")
    b = hash_password("same-password-twice")
    assert a != b, "salt must be random per hash"


def test_garbage_hash_is_rejected_not_crashing():
    assert not verify_password("x", "not-a-valid-hash")
    assert not verify_password("x", "bcrypt$abc")


# ----------------------------------------------------------------- session (unit)


def test_session_roundtrip():
    token = sign_session("user-123", SECRET, ttl_s=60)
    assert verify_session(token, SECRET) == "user-123"


def test_session_rejects_wrong_secret_and_tampering():
    token = sign_session("user-123", SECRET, ttl_s=60)
    assert verify_session(token, "another-secret") is None
    body, sig = token.rsplit(".", 1)
    tampered = sign_session("user-999", SECRET, ttl_s=60).split(".")[0] + "." + sig
    assert verify_session(tampered, SECRET) is None
    assert verify_session("garbage", SECRET) is None
    assert verify_session(None, SECRET) is None


def test_expired_session_is_rejected():
    token = sign_session("user-123", SECRET, ttl_s=-1)
    assert verify_session(token, SECRET) is None


# ------------------------------------------------------------------- login (http)


async def test_login_sets_cookie_and_grants_access(client: AsyncClient):
    resp = await client.post(
        "/console/v1/login",
        json={"email": "owner@example.com", "password": "correct-horse-battery"},
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["role"] == "owner"
    assert "aigate_session" in resp.cookies

    # the cookie alone must be enough for the control plane
    overview = await client.get("/admin/v1/overview")
    assert overview.status_code == 200


async def test_wrong_password_and_unknown_user_share_one_message(client: AsyncClient):
    wrong = await client.post(
        "/console/v1/login",
        json={"email": "owner@example.com", "password": "nope-nope-nope"},
    )
    unknown = await client.post(
        "/console/v1/login",
        json={"email": "ghost@example.com", "password": "nope-nope-nope"},
    )
    assert wrong.status_code == unknown.status_code == 401
    # identical payload: no account enumeration oracle
    assert wrong.json() == unknown.json()


async def test_admin_endpoints_require_a_credential(client: AsyncClient):
    resp = await client.get("/admin/v1/overview")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "gateway.login_required"


async def test_viewer_can_read_but_not_write(client: AsyncClient):
    login = await client.post(
        "/console/v1/login",
        json={"email": "viewer@example.com", "password": "read-only-password"},
    )
    assert login.status_code == 200

    # A developer must not see company-wide cost. That is a 403, not an empty page:
    # "no data" and "not allowed" are different answers.
    overview = await client.get("/admin/v1/overview")
    assert overview.status_code == 403
    assert overview.json()["error"]["code"] == "gateway.forbidden"

    # ...but their own calls, their models list, and their project's keys are fine.
    assert (await client.get("/admin/v1/usage")).status_code == 200
    assert (await client.get("/admin/v1/models")).status_code == 200
    assert (await client.get("/admin/v1/keys")).status_code == 200

    write = await client.post("/admin/v1/keys?project_id=whatever&name=nope")
    assert write.status_code == 403


async def test_logout_clears_the_session(client: AsyncClient):
    await client.post(
        "/console/v1/login",
        json={"email": "owner@example.com", "password": "correct-horse-battery"},
    )
    assert (await client.get("/admin/v1/overview")).status_code == 200

    await client.post("/console/v1/logout")
    client.cookies.clear()
    assert (await client.get("/admin/v1/overview")).status_code == 401


async def test_admin_cannot_mint_an_owner_but_owner_can(client: AsyncClient):
    # an admin (not owner) is refused: otherwise privilege escalation is one POST away
    await client.post(
        "/console/v1/login",
        json={"email": "admin@example.com", "password": "plain-admin-password"},
    )
    denied = await client.post(
        "/console/v1/users",
        json={"email": "sneaky@example.com", "password": "another-long-password", "role": "owner"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "gateway.forbidden"

    # an admin may still create ordinary users
    allowed = await client.post(
        "/console/v1/users",
        json={"email": "new-admin@example.com", "password": "another-long-password", "role": "admin"},
    )
    assert allowed.status_code == 201

    # the machine token is an owner, so it can
    owner = await client.post(
        "/console/v1/users",
        headers=ADMIN_TOKEN,
        json={"email": "second-owner@example.com", "password": "another-long-password", "role": "owner"},
    )
    assert owner.status_code == 201


# ------------------------------------------------------------------- audit (http)


async def test_write_actions_are_audited(client: AsyncClient):
    projects = (await client.get("/admin/v1/projects", headers=ADMIN_TOKEN)).json()["items"]
    created = await client.post(
        f"/admin/v1/keys?project_id={projects[0]['id']}&name=audited",
        headers=ADMIN_TOKEN,
    )
    assert created.status_code == 201

    async with get_session_factory()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()

    actions = {row.action for row in rows}
    assert "key.create" in actions
    row = next(r for r in rows if r.action == "key.create")
    assert row.actor == "token:admin"
    assert row.status_code == 201
    assert row.request_id.startswith("req_")


async def test_audit_names_the_human_not_the_id(client: AsyncClient):
    """An audit row that says `user:c34e...` is useless. Signed-in actions must be
    attributed to the email."""
    await client.post(
        "/console/v1/login",
        json={"email": "owner@example.com", "password": "correct-horse-battery"},
    )
    projects = (await client.get("/admin/v1/projects")).json()["items"]
    created = await client.post(f"/admin/v1/keys?project_id={projects[0]['id']}&name=attributed")
    assert created.status_code == 201

    async with get_session_factory()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()

    key_rows = [r for r in rows if r.action == "key.create"]
    assert key_rows and key_rows[-1].actor == "owner@example.com"
    assert key_rows[-1].actor_role == "owner"


async def test_reads_are_not_audited(client: AsyncClient):
    await client.get("/admin/v1/usage", headers=ADMIN_TOKEN)
    await client.get("/admin/v1/overview", headers=ADMIN_TOKEN)

    async with get_session_factory()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    assert rows == [], "polling the dashboard must not bury the audit log"


async def test_audit_endpoint_returns_newest_first(client: AsyncClient):
    projects = (await client.get("/admin/v1/projects", headers=ADMIN_TOKEN)).json()["items"]
    for i in range(2):
        await client.post(f"/admin/v1/keys?project_id={projects[0]['id']}&name=a{i}", headers=ADMIN_TOKEN)

    body = (await client.get("/admin/v1/audit?limit=10", headers=ADMIN_TOKEN)).json()
    assert body["count"] == 2
    assert body["items"][0]["action"] == "key.create"
    assert body["items"][0]["created_at"] >= body["items"][1]["created_at"]


async def test_owner_can_disable_and_the_user_is_locked_out(client: AsyncClient):
    await client.post(
        "/console/v1/login",
        json={"email": "owner@example.com", "password": "correct-horse-battery"},
    )
    project_id = (await client.get("/admin/v1/projects")).json()["items"][0]["id"]
    created = await client.post(
        "/console/v1/users",
        json={
            "email": "temp@example.com",
            "password": "temp-password-123",
            "role": "viewer",
            "project_scope": [project_id],
        },
    )
    assert created.status_code == 201
    user_id = created.json()["id"]

    assert (await client.post(f"/console/v1/users/{user_id}/disable")).status_code == 200

    client.cookies.clear()
    relogin = await client.post(
        "/console/v1/login", json={"email": "temp@example.com", "password": "temp-password-123"}
    )
    # 403 (not 401): the password is right, the account is off. Telling the person that
    # is useful; it leaks nothing, because you already had to know the password.
    assert relogin.status_code == 403
    assert relogin.json()["error"]["code"] == "gateway.user_disabled"
    assert "aigate_session" not in relogin.cookies


async def test_disabled_user_with_a_live_cookie_is_rejected(client: AsyncClient):
    """A signed cookie must not outlive the account. This is the whole reason the
    session is validated against the row on every request."""
    from aigate.config import get_settings
    from aigate.console_auth import sign_session

    token = sign_session("nobody-in-particular", get_settings().session_secret, ttl_s=600)
    client.cookies.set("aigate_session", token)
    resp = await client.get("/admin/v1/overview")
    assert resp.status_code == 401


async def test_only_owner_can_disable(client: AsyncClient):
    await client.post(
        "/console/v1/login",
        json={"email": "admin@example.com", "password": "plain-admin-password"},
    )
    users = (await client.get("/console/v1/users")).json()["items"]
    target = next(u for u in users if u["email"] == "viewer@example.com")
    resp = await client.post(f"/console/v1/users/{target['id']}/disable")
    assert resp.status_code == 403


async def test_change_own_password(client: AsyncClient):
    await client.post(
        "/console/v1/login",
        json={"email": "viewer@example.com", "password": "read-only-password"},
    )

    wrong = await client.post(
        "/console/v1/me/password", json={"current": "nope-nope", "new": "brand-new-password"}
    )
    assert wrong.status_code == 401

    other_user = await client.post(
        "/console/v1/me/password",
        json={"current": "read-only-password", "new": "brand-new-password"},
    )
    assert other_user.status_code == 200

    client.cookies.clear()
    old = await client.post(
        "/console/v1/login", json={"email": "viewer@example.com", "password": "read-only-password"}
    )
    assert old.status_code == 401, "the old password must stop working immediately"

    new = await client.post(
        "/console/v1/login", json={"email": "viewer@example.com", "password": "brand-new-password"}
    )
    assert new.status_code == 200


# ------------------------------------------------------------- production guard


def test_production_guard_rejects_placeholder_credentials():
    import pytest

    from aigate.config import Settings, assert_production_ready

    with pytest.raises(RuntimeError, match="placeholder"):
        assert_production_ready(
            Settings(env="prod", admin_token="change-me-admin-token", session_secret="x" * 40)
        )


def test_production_guard_rejects_short_secrets():
    import pytest

    from aigate.config import Settings, assert_production_ready

    with pytest.raises(RuntimeError, match="shorter than"):
        assert_production_ready(Settings(env="prod", admin_token="a" * 64, session_secret="short"))


def test_production_guard_rejects_insecure_cookie():
    import pytest

    from aigate.config import Settings, assert_production_ready

    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        assert_production_ready(
            Settings(env="prod", admin_token="a" * 64, session_secret="b" * 64, cookie_secure=False)
        )


def test_production_guard_allows_a_proper_config():
    from aigate.config import Settings, assert_production_ready

    assert_production_ready(
        Settings(env="prod", admin_token="a" * 64, session_secret="b" * 64, cookie_secure=True)
    )


def test_dev_env_is_never_blocked():
    from aigate.config import Settings, assert_production_ready

    assert_production_ready(Settings(env="dev"))
