"""组织管理：建团队、建项目。

在这之前，「开一个新项目」只能改数据库或用种子脚本——一个管理员做不到的事，
在文档里写「支持多租户」是没有意义的。
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select

from aigate.config import get_settings
from aigate.db import get_session_factory
from aigate.models import Project, Team

ADMIN = {"X-Admin-Token": get_settings().admin_token}


async def _team_id(slug: str = "test-team") -> str:
    async with get_session_factory()() as session:
        return (await session.execute(select(Team).where(Team.slug == slug))).scalar_one().id


async def test_create_team_with_budget(client: AsyncClient):
    resp = await client.post(
        "/admin/v1/teams",
        json={"name": "增长部", "slug": "growth", "rpm_limit": 120, "monthly_budget_cny": 50},
        headers=ADMIN,
    )
    assert resp.status_code == 201, resp.text
    team_id = resp.json()["id"]

    listed = await client.get("/admin/v1/teams", headers=ADMIN)
    row = next(t for t in listed.json()["items"] if t["id"] == team_id)
    assert row["slug"] == "growth"
    assert row["monthly_budget_cny"] == 50.0
    assert row["month_spend_cny"] == 0.0


async def test_duplicate_team_slug_is_refused(client: AsyncClient):
    body = {"name": "重复", "slug": "dup-team"}
    assert (await client.post("/admin/v1/teams", json=body, headers=ADMIN)).status_code == 201
    again = await client.post("/admin/v1/teams", json=body, headers=ADMIN)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "gateway.team_exists"


async def test_slug_must_be_url_safe(client: AsyncClient):
    """slug 会出现在账单、CSV 和 CLI 里；中文或空格会变成一团需要转义的字符。"""
    resp = await client.post(
        "/admin/v1/teams", json={"name": "增长部", "slug": "增长 部门"}, headers=ADMIN
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "gateway.bad_slug"


async def test_create_project_under_a_team(client: AsyncClient):
    team_id = await _team_id()
    resp = await client.post(
        "/admin/v1/projects",
        json={
            "team_id": team_id,
            "name": "订单助手",
            "slug": "order-bot",
            "rpm_limit": 30,
            "daily_budget_cny": 2,
            "monthly_budget_cny": 40,
        },
        headers=ADMIN,
    )
    assert resp.status_code == 201, resp.text

    async with get_session_factory()() as session:
        project = (
            await session.execute(select(Project).where(Project.slug == "order-bot"))
        ).scalar_one()
        assert str(project.daily_budget_cny) == "2.000000"


async def test_project_slug_is_unique_within_a_team(client: AsyncClient):
    team_id = await _team_id()
    body = {"team_id": team_id, "name": "X", "slug": "twice"}
    assert (await client.post("/admin/v1/projects", json=body, headers=ADMIN)).status_code == 201
    again = await client.post("/admin/v1/projects", json=body, headers=ADMIN)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "gateway.project_exists"


async def test_project_needs_an_existing_team(client: AsyncClient):
    resp = await client.post(
        "/admin/v1/projects",
        json={"team_id": "no-such-team", "name": "X", "slug": "orphan"},
        headers=ADMIN,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "gateway.team_not_found"


async def test_viewer_cannot_create_org_units(client: AsyncClient):
    login = await client.post(
        "/console/v1/login", json={"email": "viewer@example.com", "password": "read-only-password"}
    )
    cookies = {"Cookie": f"aigate_session={login.cookies.get('aigate_session')}"}
    resp = await client.post(
        "/admin/v1/teams", json={"name": "偷偷加", "slug": "sneaky"}, headers=cookies
    )
    assert resp.status_code == 403