"""Control-plane writes: budgets, rate limits, route edits, CSV export.

These endpoints are the difference between a dashboard and a tool: an operator who can
change a budget from the console does not need a database client at 2am.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal

from sqlalchemy import select

from aigate.api.admin_ops import _csv_safe
from aigate.config import get_settings
from aigate.db import get_session_factory
from aigate.models import AuditLog, Project, RoutePolicy

ADMIN = {"X-Admin-Token": get_settings().admin_token}


async def _project(client) -> dict:
    items = (await client.get("/admin/v1/projects", headers=ADMIN)).json()["items"]
    return items[0]


# ------------------------------------------------------------------ project budget


async def test_budget_and_rpm_are_editable(client):
    project = await _project(client)

    resp = await client.put(
        f"/admin/v1/projects/{project['id']}",
        headers=ADMIN,
        json={"rpm_limit": 120, "monthly_budget_cny": 25.5},
    )
    assert resp.status_code == 200
    item = resp.json()["item"]
    assert item["rpm_limit"] == 120
    assert item["monthly_budget_cny"] == 25.5

    # and it survived the round trip
    again = (await client.get("/admin/v1/projects", headers=ADMIN)).json()["items"][0]
    assert again["rpm_limit"] == 120
    assert again["monthly_budget_cny"] == 25.5


async def test_budget_can_be_cleared_with_explicit_null(client):
    """`null` means unlimited, `absent` means leave it alone. Conflating the two makes a
    cap you can set but never remove."""
    project = await _project(client)
    await client.put(f"/admin/v1/projects/{project['id']}", headers=ADMIN, json={"monthly_budget_cny": 10})
    resp = await client.put(
        f"/admin/v1/projects/{project['id']}", headers=ADMIN, json={"monthly_budget_cny": None}
    )
    assert resp.status_code == 200
    assert resp.json()["item"]["monthly_budget_cny"] is None

    # omitting the field must not clear it
    await client.put(f"/admin/v1/projects/{project['id']}", headers=ADMIN, json={"monthly_budget_cny": 7})
    kept = await client.put(f"/admin/v1/projects/{project['id']}", headers=ADMIN, json={"rpm_limit": 30})
    assert kept.json()["item"]["monthly_budget_cny"] == 7


async def test_paused_project_stops_spending_and_resumes(client, auth_headers, chat_payload):
    project = await _project(client)

    paused = await client.put(f"/admin/v1/projects/{project['id']}", headers=ADMIN, json={"status": "paused"})
    assert paused.status_code == 200

    blocked = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "gateway.project_paused"

    await client.put(f"/admin/v1/projects/{project['id']}", headers=ADMIN, json={"status": "active"})
    ok = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert ok.status_code == 200


async def test_unknown_project_is_404(client):
    resp = await client.put("/admin/v1/projects/nope", headers=ADMIN, json={"rpm_limit": 10})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "gateway.project_not_found"


async def test_budget_rejects_negative(client):
    project = await _project(client)
    resp = await client.put(
        f"/admin/v1/projects/{project['id']}", headers=ADMIN, json={"monthly_budget_cny": -1}
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------- route edits


async def test_route_rejects_an_unregistered_provider(client):
    """Nothing can route to a provider that does not exist — fail at write time."""
    resp = await client.put(
        "/admin/v1/routes/chat-default",
        headers=ADMIN,
        json={"candidates": [{"provider": "openai-typo", "model": "gpt"}]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "gateway.unknown_provider"
    assert "openai-typo/gpt" in body["error"]["unknown"]


async def test_unpriced_model_saves_with_a_warning(client):
    """A known provider with no price row is legitimate (offline mocks, a model in
    onboarding). It saves, and says what it costs: zero."""
    resp = await client.put(
        "/admin/v1/routes/chat-default",
        headers=ADMIN,
        json={"candidates": [{"provider": "mock", "model": "mock-unpriced"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["item"]["candidates"] == [{"provider": "mock", "model": "mock-unpriced"}]
    assert any("未定价" in w for w in body["warnings"])


async def test_priced_route_has_no_warnings(client):
    resp = await client.put(
        "/admin/v1/routes/chat-default",
        headers=ADMIN,
        json={"candidates": [{"provider": "mock", "model": "mock-ok"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["warnings"] == []


async def test_route_edit_takes_effect_on_the_next_request(client, auth_headers, chat_payload):
    resp = await client.put(
        "/admin/v1/routes/chat-default",
        headers=ADMIN,
        json={"candidates": [{"provider": "mock-alt", "model": "mock-ok"}]},
    )
    assert resp.status_code == 200

    out = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert out.json()["x_aigate"]["provider"] == "mock-alt"


async def test_route_cannot_be_emptied(client):
    resp = await client.put("/admin/v1/routes/chat-default", headers=ADMIN, json={"candidates": []})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "gateway.empty_candidates"


async def test_route_unknown_is_404(client):
    resp = await client.put("/admin/v1/routes/nope", headers=ADMIN, json={"enabled": False})
    assert resp.status_code == 404


async def test_create_route_then_conflict(client):
    created = await client.post(
        "/admin/v1/routes?match_model=cheap-model&candidates=mock/mock-ok&strategy=cost_first",
        headers=ADMIN,
    )
    assert created.status_code == 201
    item = created.json()["item"]
    assert item["match_model"] == "cheap-model"
    assert item["strategy"] == "cost_first"
    assert item["candidates"] == [{"provider": "mock", "model": "mock-ok"}]

    dup = await client.post("/admin/v1/routes?match_model=cheap-model&candidates=mock/mock-ok", headers=ADMIN)
    assert dup.status_code == 409


async def test_create_route_bad_candidate_format(client):
    resp = await client.post("/admin/v1/routes?match_model=x&candidates=justamodel", headers=ADMIN)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "gateway.bad_candidate"


# ------------------------------------------------------------------------ summary


async def test_projects_summary_reports_spend_and_headroom(client, auth_headers, chat_payload):
    project = await _project(client)
    # A tiny cap so one mock call actually moves the needle: with ¥100 the percentage
    # rounds to 0.0 and the assertion would pass for the wrong reason.
    await client.put(f"/admin/v1/projects/{project['id']}", headers=ADMIN, json={"monthly_budget_cny": 0.001})
    await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)

    body = (await client.get("/admin/v1/projects/summary", headers=ADMIN)).json()
    item = next(i for i in body["items"] if i["id"] == project["id"])
    assert item["month_spend_cny"] > 0
    assert item["monthly_budget_cny"] == 0.001
    assert 0 < item["budget_used_pct"] < 100
    assert item["keys_active"] >= 1


async def test_summary_without_budget_has_no_percentage(client):
    project = await _project(client)
    await client.put(f"/admin/v1/projects/{project['id']}", headers=ADMIN, json={"monthly_budget_cny": None})
    body = (await client.get("/admin/v1/projects/summary", headers=ADMIN)).json()
    item = next(i for i in body["items"] if i["id"] == project["id"])
    assert item["budget_used_pct"] is None


# -------------------------------------------------------------------------- export


async def test_csv_export_headers_and_rows(client, auth_headers, chat_payload):
    await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)

    resp = await client.get("/admin/v1/usage/export.csv?days=7", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["x-export-rows"] == "1"

    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0][0] == "created_at"
    assert "cost_cny" in rows[0]
    assert len(rows) == 2
    assert rows[1][2] == "test-project"


async def test_csv_export_respects_filters(client, auth_headers, chat_payload):
    await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)

    assert (await client.get("/admin/v1/usage/export.csv?status=error", headers=ADMIN)).text.count(
        "\n"
    ) == 1  # header only
    assert (await client.get("/admin/v1/usage/export.csv?model=failover-demo", headers=ADMIN)).headers[
        "x-export-rows"
    ] == "0"


def test_csv_safe_neutralises_formulas():
    assert _csv_safe("=1+1") == "'=1+1"
    assert _csv_safe("+86") == "'+86"
    assert _csv_safe("@cmd") == "'@cmd"
    assert _csv_safe("-5") == "'-5"
    assert _csv_safe("normal") == "normal"
    assert _csv_safe(None) == ""


# ---------------------------------------------------------------------- usage list


async def test_usage_supports_offset_and_project_filter(client, auth_headers, chat_payload):
    for _ in range(3):
        await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)

    first = (await client.get("/admin/v1/usage?limit=2", headers=ADMIN)).json()
    assert first["total"] == 3
    assert first["count"] == 2
    assert first["limit"] == 2

    second = (await client.get("/admin/v1/usage?limit=2&offset=2", headers=ADMIN)).json()
    assert second["count"] == 1

    project = await _project(client)
    filtered = (await client.get(f"/admin/v1/usage?project_id={project['id']}", headers=ADMIN)).json()
    assert filtered["total"] == 3

    none = (await client.get("/admin/v1/usage?project_id=nope", headers=ADMIN)).json()
    assert none["total"] == 0


# ------------------------------------------------------------------------- audit


async def test_writes_are_audited_with_action_names(client):
    project = await _project(client)
    await client.put(f"/admin/v1/projects/{project['id']}", headers=ADMIN, json={"rpm_limit": 45})
    await client.put("/admin/v1/routes/chat-default", headers=ADMIN, json={"max_retries": 2})

    async with get_session_factory()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()

    actions = {r.action for r in rows}
    assert "project.update" in actions
    assert "route.update" in actions
    assert all(r.actor == "token:admin" for r in rows)


async def test_route_edit_persists_without_restart(client):
    """The router reads RoutePolicy per request, so an edit must not need a restart."""
    async with get_session_factory()() as session:
        before = (
            await session.execute(select(RoutePolicy).where(RoutePolicy.match_model == "chat-default"))
        ).scalar_one()
        assert before.fallback == []

    await client.put(
        "/admin/v1/routes/chat-default",
        headers=ADMIN,
        json={"fallback": [{"provider": "mock-alt", "model": "mock-ok"}]},
    )
    async with get_session_factory()() as session:
        after = (
            await session.execute(select(RoutePolicy).where(RoutePolicy.match_model == "chat-default"))
        ).scalar_one()
        assert after.fallback == [{"provider": "mock-alt", "model": "mock-ok"}]


async def test_project_row_survives_commit_as_decimal(client):
    """A budget stored as float would drift on the boundary; it is Numeric(14,6)."""
    project = await _project(client)
    await client.put(f"/admin/v1/projects/{project['id']}", headers=ADMIN, json={"monthly_budget_cny": 0.1})
    async with get_session_factory()() as session:
        row = (await session.execute(select(Project).where(Project.id == project["id"]))).scalar_one()
        assert isinstance(row.monthly_budget_cny, Decimal)
        assert row.monthly_budget_cny == Decimal("0.1")
