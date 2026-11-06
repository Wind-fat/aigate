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
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aigate.api.deps import Actor, get_actor, require_admin, require_capability
from aigate.db import get_session
from aigate.models import (
    Alert,
    ApiKey,
    ModelPrice,
    Project,
    RoutePolicy,
    ShadowRun,
    Team,
    UsageRecord,
    utcnow,
)

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
    # A daily cap stops "the loop ran all night" from becoming a monthly incident.
    daily_budget_cny: float | None = Field(default=None, ge=0)
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
        "daily_budget_cny": _money(project.daily_budget_cny)
        if project.daily_budget_cny is not None
        else None,
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
    if "daily_budget_cny" in fields:
        project.daily_budget_cny = (
            Decimal(str(patch.daily_budget_cny)) if patch.daily_budget_cny is not None else None
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
    # Shadow candidates: called in the background, recorded, never returned to the caller.
    shadow: list[Candidate] | None = None
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
        "shadow": policy.shadow or [],
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
    if "shadow" in fields and patch.shadow is not None:
        if len(patch.shadow) > 2:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "gateway.too_many_shadows",
                    "message": "at most 2 shadow candidates (each one costs real money)",
                },
            )
        warnings += await _validate_candidates(session, patch.shadow, "shadow")
        policy.shadow = [c.model_dump() for c in patch.shadow]
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


# ------------------------------------------------------------------------ teams


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    slug: str = Field(min_length=2, max_length=40)
    rpm_limit: int = Field(default=0, ge=0, le=600_000)
    monthly_budget_cny: float | None = Field(default=None, ge=0)


class ProjectIn(BaseModel):
    team_id: str
    name: str = Field(min_length=1, max_length=80)
    slug: str = Field(min_length=2, max_length=40)
    rpm_limit: int = Field(default=60, ge=0, le=600_000)
    daily_budget_cny: float | None = Field(default=None, ge=0)
    monthly_budget_cny: float | None = Field(default=None, ge=0)


@router.post("/teams", status_code=status.HTTP_201_CREATED)
async def create_team(
    payload: TeamIn,
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """建一个部门。

    `slug` 是账单和 URL 里的身份，所以只允许小写字母/数字/连字符：中文或空格 slug
    会在 CSV 导出和 CLI 里变成一团需要转义的字符。
    """
    require_capability(actor, "projects:write")
    slug = payload.slug.strip().lower()
    if not SLUG_RE.match(slug):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "gateway.bad_slug",
                "message": "slug 只能用小写字母、数字和连字符，长度 2-40",
            },
        )
    exists = (await session.execute(select(Team).where(Team.slug == slug))).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "gateway.team_exists", "message": slug, "existing_id": exists.id},
        )
    team = Team(
        name=payload.name.strip(),
        slug=slug,
        rpm_limit=payload.rpm_limit,
        monthly_budget_cny=Decimal(str(payload.monthly_budget_cny))
        if payload.monthly_budget_cny is not None
        else None,
    )
    session.add(team)
    await session.commit()
    return {"id": team.id, "slug": team.slug, "name": team.name}


@router.post("/projects", status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectIn,
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_capability(actor, "projects:write")
    slug = payload.slug.strip().lower()
    if not SLUG_RE.match(slug):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "gateway.bad_slug",
                "message": "slug 只能用小写字母、数字和连字符，长度 2-40",
            },
        )
    team = (await session.execute(select(Team).where(Team.id == payload.team_id))).scalar_one_or_none()
    if team is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "gateway.team_not_found", "message": payload.team_id},
        )
    # slug 在项目内部唯一即可（同一 slug 跨团队是常见做法），但同一团队内重名会让
    # 账单上出现两行一模一样的项目名。
    exists = (
        await session.execute(
            select(Project).where(Project.team_id == team.id, Project.slug == slug)
        )
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "gateway.project_exists",
                "message": f"{team.slug}/{slug} exists",
                "existing_id": exists.id,
            },
        )
    project = Project(
        team_id=team.id,
        name=payload.name.strip(),
        slug=slug,
        rpm_limit=payload.rpm_limit,
        daily_budget_cny=Decimal(str(payload.daily_budget_cny))
        if payload.daily_budget_cny is not None
        else None,
        monthly_budget_cny=Decimal(str(payload.monthly_budget_cny))
        if payload.monthly_budget_cny is not None
        else None,
    )
    session.add(project)
    await session.commit()
    return {
        "id": project.id,
        "slug": project.slug,
        "team": team.slug,
        "name": project.name,
        "note": "下一步：给项目签一把 key，并在「模型路由」里确认它用的虚拟模型",
    }


class TeamPatch(BaseModel):
    rpm_limit: int | None = Field(default=None, ge=0, le=600_000)
    monthly_budget_cny: float | None = Field(default=None, ge=0)


@router.get("/teams")
async def list_teams(
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Department-level caps plus this month's spend across all their projects."""
    require_capability(actor, "projects:read")
    rows = (await session.execute(select(Team).order_by(Team.slug))).scalars().all()
    out = []
    for team in rows:
        spent = (
            await session.execute(
                select(func.coalesce(func.sum(UsageRecord.cost_cny), 0)).where(
                    UsageRecord.team_id == team.id, UsageRecord.created_at >= _month_start()
                )
            )
        ).scalar_one()
        limit = team.monthly_budget_cny
        spent_dec = Decimal(str(spent or 0))
        out.append(
            {
                "id": team.id,
                "slug": team.slug,
                "name": team.name,
                "rpm_limit": team.rpm_limit or 0,
                "monthly_budget_cny": _money(limit) if limit is not None else None,
                "month_spend_cny": _money(spent_dec),
                "budget_used_pct": round(float(spent_dec / limit * 100), 1) if limit else None,
            }
        )
    return {"items": out}


@router.put("/teams/{team_id}")
async def update_team(
    team_id: str,
    patch: TeamPatch,
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_capability(actor, "projects:write")
    team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
    if team is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "gateway.team_not_found", "message": team_id},
        )
    fields = patch.model_fields_set
    if "rpm_limit" in fields and patch.rpm_limit is not None:
        team.rpm_limit = patch.rpm_limit
    if "monthly_budget_cny" in fields:
        team.monthly_budget_cny = (
            Decimal(str(patch.monthly_budget_cny)) if patch.monthly_budget_cny is not None else None
        )
    await session.commit()
    return {
        "id": team.id,
        "rpm_limit": team.rpm_limit,
        "monthly_budget_cny": _money(team.monthly_budget_cny)
        if team.monthly_budget_cny is not None
        else None,
    }


# ----------------------------------------------------------------------- alerts


@router.get("/alerts")
async def list_alerts(
    include_acknowledged: bool = False,
    limit: int = Query(50, ge=1, le=200),
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Budget warnings. Deduplicated per (scope, metric, period) by the writer."""
    require_capability(actor, "costs")
    stmt = select(Alert).order_by(Alert.created_at.desc()).limit(limit)
    if not include_acknowledged:
        stmt = stmt.where(Alert.acknowledged_at.is_(None))
    rows = (await session.execute(stmt)).scalars().all()
    return {
        "items": [
            {
                "id": a.id,
                "level": a.level,
                "scope_type": a.scope_type,
                "scope_slug": a.scope_slug,
                "metric": a.metric,
                "period_key": a.period_key,
                "value": _money(a.value),
                "limit_value": _money(a.limit_value),
                "message": a.message,
                "created_at": _iso(a.created_at),
                "acknowledged_at": _iso(a.acknowledged_at),
            }
            for a in rows
        ],
        "count": len(rows),
    }


@router.post("/alerts/{alert_id}/ack")
async def acknowledge_alert(
    alert_id: str,
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_capability(actor, "projects:write")
    alert = (await session.execute(select(Alert).where(Alert.id == alert_id))).scalar_one_or_none()
    if alert is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "gateway.alert_not_found", "message": alert_id},
        )
    alert.acknowledged_at = utcnow()
    await session.commit()
    return {"id": alert.id, "acknowledged_at": _iso(alert.acknowledged_at)}


# ----------------------------------------------------------------------- shadow


@router.get("/shadow")
async def list_shadow_runs(
    request_id: str | None = None,
    route: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """影子和主回答的对照。相似度只是烟雾信号，不是评分。"""
    require_capability(actor, "trace:all")
    stmt = select(ShadowRun).order_by(ShadowRun.created_at.desc()).limit(limit)
    if request_id:
        stmt = stmt.where(ShadowRun.request_id == request_id)
    if route:
        stmt = stmt.where(ShadowRun.route == route)
    rows = (await session.execute(stmt)).scalars().all()
    return {
        "items": [
            {
                "request_id": r.request_id,
                "route": r.route,
                "provider": r.provider,
                "model": r.model,
                "ok": r.ok,
                "error_code": r.error_code,
                "latency_ms": r.latency_ms,
                "cost_cny": _money(r.cost_cny),
                "output_chars": r.output_chars,
                "similarity": r.similarity,
                "preview": r.output_preview[:300],
                "created_at": _iso(r.created_at),
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/shadow/summary")
async def shadow_summary(
    days: int = Query(7, ge=1, le=90),
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按模型汇总：跑了几次、成功率、平均相似度、影子花的钱。

    换模型前看这张表：相似度一路 0.9 且没有失败，才谈得上“可以切”。
    """
    require_capability(actor, "trace:all")
    since = utcnow() - timedelta(days=days)
    rows = (
        await session.execute(select(ShadowRun).where(ShadowRun.created_at >= since))
    ).scalars().all()
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r.provider, r.model)
        b = buckets.setdefault(
            key, {"runs": 0, "ok": 0, "sim": [], "cost": Decimal("0"), "latency": []}
        )
        b["runs"] += 1
        b["ok"] += 1 if r.ok else 0
        b["cost"] += r.cost_cny or Decimal("0")
        b["latency"].append(r.latency_ms)
        if r.similarity is not None:
            b["sim"].append(r.similarity)
    items = [
        {
            "provider": provider,
            "model": model,
            "runs": b["runs"],
            "success_rate": round(b["ok"] / b["runs"] * 100, 1) if b["runs"] else 0.0,
            "avg_similarity": round(sum(b["sim"]) / len(b["sim"]), 3) if b["sim"] else None,
            "avg_latency_ms": round(sum(b["latency"]) / len(b["latency"]), 0) if b["latency"] else 0,
            "shadow_cost_cny": _money(b["cost"]),
        }
        for (provider, model), b in sorted(buckets.items(), key=lambda kv: -kv[1]["runs"])
    ]
    return {"items": items, "window_days": days}
