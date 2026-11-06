"""Mutating control-plane endpoints: budgets, rate limits, routing, exports.

Split out of `admin.py` because that module is the read side: the console's dashboard
should not be able to change anything by accident, and a reviewer can see at a glance
which endpoints write.

Three rules these endpoints keep:

1. **Validate against reality, not intent.** A route candidate that is not in
   `model_prices` is rejected at write time. Otherwise a typo becomes a 02:00 incident:
   the request arrives, the router finds nothing, and the caller gets a 502.
2. **Return the new state.** The console re-renders from the response, so what the
   operator sees is what the database kept.
3. **Cheap to audit.** Every write lands in `audit_logs` via middleware; nothing here
   needs to remember to do it.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aigate.api.deps import Actor, get_actor, require_admin, require_capability
from aigate.db import get_session
from aigate.models import ApiKey, ModelPrice, Project, RoutePolicy, Team, UsageRecord, utcnow

router = APIRouter(prefix="/admin/v1", tags=["control-plane"], dependencies=[Depends(require_admin)])

CSV_MAX_ROWS = 50_000


def _money(value: Any) -> float:
    return round(float(value or 0), 6)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _iso(dt: datetime | None) -> str | None:
    aware = _aware(dt)
    return aware.isoformat() if aware else None


def _month_start() -> datetime:
    now = utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


# --------------------------------------------------------------------- projects


class ProjectPatch(BaseModel):
    rpm_limit: int | None = Field(default=None, ge=1, le=60_000)
    # Explicit `null` means "no cap"; omitting the field means "leave it alone".
    # That distinction is why the handler looks at `model_fields_set` instead of
    # `is not None` — a budget you cannot remove is a support ticket waiting to happen.
    monthly_budget_cny: float | None = Field(default=None, ge=0)
    status: Literal["active", "paused"] | None = None


async def _project_row(session: AsyncSession, project: Project) -> dict[str, Any]:
    spent = (
        await session.execute(
            select(func.coalesce(func.sum(UsageRecord.cost_cny), 0)).where(
                UsageRecord.project_id == project.id, UsageRecord.created_at >= _month_start()
            )
        )
    ).scalar_one()
    budget = project.monthly_budget_cny
    spent_dec = Decimal(str(spent or 0))
    pct = float(spent_dec / budget * 100) if budget and budget > 0 else None
    keys = (
        await session.execute(
            select(func.count())
            .select_from(ApiKey)
            .where(ApiKey.project_id == project.id, ApiKey.status == "active")
        )
    ).scalar_one()
    return {
        "id": project.id,
        "slug": project.slug,
        "name": project.name,
        "status": project.status,
        "rpm_limit": project.rpm_limit,
        "monthly_budget_cny": _money(budget) if budget is not None else None,
        "month_spend_cny": _money(spent_dec),
        "budget_used_pct": round(pct, 1) if pct is not None else None,
        "keys_active": keys,
    }


@router.get("/projects/summary")
async def projects_summary(
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Projects with this month's spend and budget headroom — the finance view."""
    require_capability(actor, "projects:read")
    rows = (
        await session.execute(
            select(Project, Team).join(Team, Project.team_id == Team.id).order_by(Team.slug, Project.slug)
        )
    ).all()
    items = []
    for project, team in rows:
        item = await _project_row(session, project)
        item["team"] = team.slug
        items.append(item)
    return {"items": items, "month": _month_start().date().isoformat()}


@router.put("/projects/{project_id}")
async def update_project(
    project_id: str,
    patch: ProjectPatch,
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_capability(actor, "projects:write")
    project = (await session.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "gateway.project_not_found", "message": project_id},
        )

    fields = patch.model_fields_set
    if "rpm_limit" in fields and patch.rpm_limit is not None:
        project.rpm_limit = patch.rpm_limit
    if "monthly_budget_cny" in fields:
        project.monthly_budget_cny = (
            Decimal(str(patch.monthly_budget_cny)) if patch.monthly_budget_cny is not None else None
        )
    if "status" in fields and patch.status is not None:
        project.status = patch.status

    await session.commit()
    return {"item": await _project_row(session, project)}


# ------------------------------------------------------------------------ routes


class Candidate(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    model: str = Field(min_length=1, max_length=80)


class RoutePatch(BaseModel):
    candidates: list[Candidate] | None = None
    fallback: list[Candidate] | None = None
    strategy: Literal["static", "cost_first", "latency_first"] | None = None
    timeout_ms: int | None = Field(default=None, ge=100, le=600_000)
    max_retries: int | None = Field(default=None, ge=0, le=5)
    enabled: bool | None = None


async def _validate_candidates(session: AsyncSession, pairs: list[Candidate], field: str) -> list[str]:
    """Reject what cannot work, warn about what merely costs nothing.

    Two different mistakes, two different answers:

    * **unknown provider** -> 400. Nothing can route to it; accepting it means a 502 at
      request time and nobody knows which edit caused it.
    * **provider known but model not priced** -> warning. Offline mocks and freshly
      onboarded models legitimately have no price row yet; the gateway records cost 0
      and marks the response `priced: false`. Blocking the save would make the built-in
      demo routes uneditable, which is worse than a warning.
    """
    if not pairs:
        return []

    from aigate.providers import known_providers

    registry = set(known_providers())
    unknown_provider = [f"{c.provider}/{c.model}" for c in pairs if c.provider not in registry]
    if unknown_provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "gateway.unknown_provider",
                "message": f"{field}: provider not registered: {', '.join(unknown_provider)}",
                "unknown": unknown_provider,
            },
        )

    priced = {
        (p.provider, p.model)
        for p in (await session.execute(select(ModelPrice).where(ModelPrice.enabled.is_(True)))).scalars()
    }
    return [
        f"{c.provider}/{c.model} 未定价，成本按 0 计" for c in pairs if (c.provider, c.model) not in priced
    ]


def _route_out(policy: RoutePolicy) -> dict[str, Any]:
    return {
        "match_model": policy.match_model,
        "name": policy.name,
        "strategy": policy.strategy,
        "candidates": policy.candidates,
        "fallback": policy.fallback,
        "timeout_ms": policy.timeout_ms,
        "max_retries": policy.max_retries,
        "enabled": policy.enabled,
    }


@router.put("/routes/{match_model}")
async def update_route(
    match_model: str,
    patch: RoutePatch,
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_capability(actor, "routes:write")
    policy = (
        await session.execute(select(RoutePolicy).where(RoutePolicy.match_model == match_model))
    ).scalar_one_or_none()
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "gateway.route_not_found", "message": match_model},
        )

    fields = patch.model_fields_set
    warnings: list[str] = []
    if "candidates" in fields and patch.candidates is not None:
        if not patch.candidates:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "gateway.empty_candidates",
                    "message": "a route needs at least one candidate",
                },
            )
        warnings += await _validate_candidates(session, patch.candidates, "candidates")
        policy.candidates = [c.model_dump() for c in patch.candidates]
    if "fallback" in fields and patch.fallback is not None:
        warnings += await _validate_candidates(session, patch.fallback, "fallback")
        policy.fallback = [c.model_dump() for c in patch.fallback]
    if "strategy" in fields and patch.strategy is not None:
        policy.strategy = patch.strategy
    if "timeout_ms" in fields and patch.timeout_ms is not None:
        policy.timeout_ms = patch.timeout_ms
    if "max_retries" in fields and patch.max_retries is not None:
        policy.max_retries = patch.max_retries
    if "enabled" in fields and patch.enabled is not None:
        policy.enabled = patch.enabled

    await session.commit()
    return {"item": _route_out(policy), "warnings": warnings}


@router.post("/routes", status_code=status.HTTP_201_CREATED)
async def create_route(
    match_model: str = Query(..., min_length=1, max_length=80),
    name: str = Query("", max_length=80),
    strategy: Literal["static", "cost_first", "latency_first"] = "static",
    candidates: str = Query(..., description="provider/model,provider/model"),
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Add a virtual model. `candidates` is a comma list of `provider/model`."""
    require_capability(actor, "routes:write")
    exists = (
        await session.execute(select(RoutePolicy).where(RoutePolicy.match_model == match_model))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "gateway.route_exists", "message": match_model},
        )

    parsed: list[Candidate] = []
    for chunk in candidates.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        provider, _, model = chunk.partition("/")
        if not provider or not model:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "gateway.bad_candidate", "message": f"expected provider/model: {chunk}"},
            )
        parsed.append(Candidate(provider=provider, model=model))
    if not parsed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "gateway.empty_candidates", "message": "at least one candidate required"},
        )
    warnings = await _validate_candidates(session, parsed, "candidates")

    policy = RoutePolicy(
        name=name or match_model,
        match_model=match_model,
        strategy=strategy,
        candidates=[c.model_dump() for c in parsed],
        fallback=[],
    )
    session.add(policy)
    await session.commit()
    return {"item": _route_out(policy), "warnings": warnings}


# ----------------------------------------------------------------------- exports


def _csv_safe(value: Any) -> str:
    """Neutralise spreadsheet formula injection.

    A prompt preview that starts with `=` is a formula when finance opens the export in
    Excel. Prefixing with an apostrophe keeps the text and kills the execution.
    """
    text = "" if value is None else str(value)
    if text and text[0] in "=+-@\t\r":
        return "'" + text
    return text


@router.get("/usage/export.csv")
async def export_usage_csv(
    days: int = Query(30, ge=1, le=365),
    project_id: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    model: str | None = None,
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> Response:
    require_capability(actor, "export")
    since = utcnow() - timedelta(days=days)
    stmt = (
        select(UsageRecord)
        .where(UsageRecord.created_at >= since)
        .order_by(UsageRecord.created_at.desc())
        .limit(CSV_MAX_ROWS)
    )
    if project_id:
        stmt = stmt.where(UsageRecord.project_id == project_id)
    if status_filter:
        stmt = stmt.where(UsageRecord.status == status_filter)
    if model:
        stmt = stmt.where(UsageRecord.model_requested == model)

    rows = (await session.execute(stmt)).scalars().all()
    projects = {p.id: p.slug for p in (await session.execute(select(Project))).scalars()}

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "created_at",
            "request_id",
            "project",
            "provider",
            "model_requested",
            "model_used",
            "input_tokens",
            "output_tokens",
            "cost_cny",
            "latency_ms",
            "ttft_ms",
            "status",
            "error_code",
            "cache_hit",
            "degraded",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                _iso(r.created_at),
                r.request_id,
                projects.get(r.project_id, r.project_id),
                r.provider,
                r.model_requested,
                r.model_used,
                r.input_tokens,
                r.output_tokens,
                f"{float(r.cost_cny or 0):.6f}",
                r.latency_ms,
                r.ttft_ms or "",
                r.status,
                r.error_code or "",
                r.cache_hit or "",
                int(bool(r.degraded)),
            ]
        )

    filename = f"aigate-usage-{utcnow().strftime('%Y%m%d-%H%M')}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Excel on a Chinese Windows opens UTF-8 CSV as mojibake without this.
            "X-Export-Rows": str(len(rows)),
        },
    )
