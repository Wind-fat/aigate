"""Control plane: what the console reads.

Auth is a single admin token (`X-Admin-Token`). That is deliberately crude: a real
deployment wants SSO + per-tenant scoping, and pretending otherwise with a half-built
RBAC would be worse than being explicit about the gap (see README "known limitations").

Aggregation note: the overview sums rows in Python rather than in SQL. At demo scale
(thousands of rows) that is microseconds and keeps the code backend-agnostic; at a
million rows it belongs in a materialised daily table. The comment is the honest part.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aigate.api.deps import require_admin
from aigate.db import get_session
from aigate.models import (
    ApiKey,
    AuditLog,
    ModelPrice,
    Project,
    RequestTrace,
    RoutePolicy,
    Team,
    UsageRecord,
    utcnow,
)
from aigate.security import generate_api_key

router = APIRouter(prefix="/admin/v1", tags=["control-plane"], dependencies=[Depends(require_admin)])


def _money(value: Any) -> float:
    return round(float(value or 0), 6)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _iso(dt: datetime | None) -> str | None:
    aware = _aware(dt)
    return aware.isoformat() if aware else None


def _p95(values: list[int]) -> int:
    """Linear-interpolated p95.

    Plain "take the k-th item" breaks on small samples: with 16 requests a single slow
    call sits outside the index, so p95 comes out *below* the mean and the dashboard
    looks broken. Interpolating between neighbours gives a number a human would accept.
    """
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = 0.95 * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return int(round(ordered[low] + (ordered[high] - ordered[low]) * frac))


@router.get("/overview")
async def overview(
    days: int = Query(7, ge=1, le=30),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    now = utcnow()
    # Window = the last N *calendar* days including today. Using `now - N days` makes
    # the chart show N+1 buckets whenever the clock isn't exactly midnight, and totals
    # then disagree with the bars — a bug you only notice on screenshots.
    since = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        (
            await session.execute(
                select(UsageRecord)
                .where(UsageRecord.created_at >= since)
                .order_by(UsageRecord.created_at.asc())
            )
        )
        .scalars()
        .all()
    )

    today = now.date()
    today_rows = [r for r in rows if _aware(r.created_at) and _aware(r.created_at).date() == today]

    def summary(subset: list[UsageRecord]) -> dict[str, Any]:
        total = len(subset)
        errors = sum(1 for r in subset if r.status == "error")
        cached = sum(1 for r in subset if r.status == "cached")
        return {
            "requests": total,
            "errors": errors,
            "cached": cached,
            "success_rate": round((total - errors) / total * 100, 2) if total else 100.0,
            "cache_hit_rate": round(cached / total * 100, 2) if total else 0.0,
            "cost_cny": _money(sum((r.cost_cny for r in subset), Decimal("0"))),
            "avg_latency_ms": round(sum(r.latency_ms for r in subset) / total, 1) if total else 0.0,
            "p95_latency_ms": _p95([r.latency_ms for r in subset]),
        }

    # Backfill every day in the window: a chart labelled "last 7 days" that only shows
    # days with traffic is quietly lying about the shape of the data.
    series_map: dict[str, dict[str, Any]] = {}
    for offset in range(days - 1, -1, -1):
        day = (now - timedelta(days=offset)).date().isoformat()
        series_map[day] = {"requests": 0, "cost_cny": 0.0, "errors": 0}

    for row in rows:
        aware = _aware(row.created_at)
        if aware is None:
            continue
        day = aware.date().isoformat()
        bucket = series_map.setdefault(day, {"requests": 0, "cost_cny": 0.0, "errors": 0})
        bucket["requests"] += 1
        bucket["cost_cny"] += float(row.cost_cny or 0)
        if row.status == "error":
            bucket["errors"] += 1

    model_counter: Counter[str] = Counter()
    model_cost: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in rows:
        model_counter[row.model_used or row.model_requested] += 1
        model_cost[row.model_used or row.model_requested] += row.cost_cny or Decimal("0")

    recent_errors = [
        {
            "request_id": r.request_id,
            "model": r.model_requested,
            "code": r.error_code,
            "created_at": _iso(r.created_at),
        }
        for r in sorted(rows, key=lambda x: _aware(x.created_at) or now, reverse=True)
        if r.status == "error"
    ][:5]

    return {
        "today": summary(today_rows),
        "window": {"days": days, **summary(rows)},
        "series": [
            {
                "date": day,
                "requests": v["requests"],
                "cost_cny": round(v["cost_cny"], 6),
                "errors": v["errors"],
            }
            for day, v in sorted(series_map.items())
        ],
        "top_models": [
            {"model": m, "requests": c, "cost_cny": _money(model_cost[m])}
            for m, c in model_counter.most_common(5)
        ],
        "recent_errors": recent_errors,
    }


@router.get("/usage")
async def usage(
    limit: int = Query(50, ge=1, le=200),
    status_filter: str | None = Query(None, alias="status"),
    model: str | None = None,
    only_cached: bool | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(UsageRecord).order_by(UsageRecord.created_at.desc()).limit(limit)
    if status_filter:
        stmt = stmt.where(UsageRecord.status == status_filter)
    if model:
        stmt = stmt.where(UsageRecord.model_requested == model)
    if only_cached:
        stmt = stmt.where(UsageRecord.cache_hit.is_not(None))

    rows = (await session.execute(stmt)).scalars().all()
    return {
        "items": [
            {
                "request_id": r.request_id,
                "team_id": r.team_id,
                "project_id": r.project_id,
                "provider": r.provider,
                "model_requested": r.model_requested,
                "model_used": r.model_used,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cost_cny": _money(r.cost_cny),
                "latency_ms": r.latency_ms,
                "ttft_ms": r.ttft_ms,
                "status": r.status,
                "error_code": r.error_code,
                "cache_hit": r.cache_hit,
                "degraded": r.degraded,
                "created_at": _iso(r.created_at),
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/traces/{request_id}")
async def trace(request_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = (
        await session.execute(select(RequestTrace).where(RequestTrace.request_id == request_id).limit(1))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "gateway.trace_not_found", "message": f"no trace for {request_id}"},
        )
    usage_row = (
        await session.execute(select(UsageRecord).where(UsageRecord.request_id == request_id).limit(1))
    ).scalar_one_or_none()

    return {
        "request_id": row.request_id,
        "project_id": row.project_id,
        "model_requested": row.model_requested,
        "prompt_preview": row.prompt_preview,
        "prompt_sha256": row.prompt_sha256,
        "pii_masked": row.pii_masked,
        "stage_timings": row.stage_timings,
        "route_path": row.route_path,
        "http_status": row.http_status,
        "created_at": _iso(row.created_at),
        "usage": None
        if usage_row is None
        else {
            "provider": usage_row.provider,
            "model_used": usage_row.model_used,
            "input_tokens": usage_row.input_tokens,
            "output_tokens": usage_row.output_tokens,
            "cost_cny": _money(usage_row.cost_cny),
            "latency_ms": usage_row.latency_ms,
            "ttft_ms": usage_row.ttft_ms,
            "status": usage_row.status,
            "cache_hit": usage_row.cache_hit,
            "degraded": usage_row.degraded,
        },
    }


@router.get("/keys")
async def list_keys(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(ApiKey, Project, Team)
            .join(Project, ApiKey.project_id == Project.id)
            .join(Team, Project.team_id == Team.id)
            .order_by(ApiKey.created_at.desc())
        )
    ).all()
    return {
        "items": [
            {
                "id": key.id,
                "name": key.name,
                "prefix": key.prefix,
                "team": team.slug,
                "project": project.slug,
                "status": key.status,
                "allowed_models": key.allowed_models,
                "created_at": _iso(key.created_at),
                "last_used_at": _iso(key.last_used_at),
                "revoked_at": _iso(key.revoked_at),
            }
            for key, project, team in rows
        ]
    }


@router.post("/keys", status_code=status.HTTP_201_CREATED)
async def create_key(
    project_id: str,
    name: str = "console-key",
    allowed_models: str = "",
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    project = (await session.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "gateway.project_not_found", "message": project_id},
        )

    raw, prefix, digest = generate_api_key()
    key = ApiKey(
        project_id=project.id,
        name=name,
        prefix=prefix,
        key_hash=digest,
        allowed_models=[m for m in allowed_models.split(",") if m],
    )
    session.add(key)
    await session.commit()

    # The plaintext key is returned exactly once — there is no endpoint to read it back.
    return {"id": key.id, "api_key": raw, "prefix": prefix, "note": "shown once, not recoverable"}


@router.post("/keys/{key_id}/revoke")
async def revoke_key(key_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    key = (await session.execute(select(ApiKey).where(ApiKey.id == key_id))).scalar_one_or_none()
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "gateway.key_not_found", "message": key_id},
        )
    key.status = "revoked"
    key.revoked_at = utcnow()
    await session.commit()
    return {"id": key.id, "status": key.status}


@router.get("/projects")
async def list_projects(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(Project, Team).join(Team, Project.team_id == Team.id).order_by(Team.slug, Project.slug)
        )
    ).all()
    return {
        "items": [
            {
                "id": project.id,
                "name": project.name,
                "slug": project.slug,
                "team": team.slug,
                "monthly_budget_cny": _money(project.monthly_budget_cny)
                if project.monthly_budget_cny is not None
                else None,
                "rpm_limit": project.rpm_limit,
            }
            for project, team in rows
        ]
    }


@router.get("/models")
async def list_models(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    prices = (await session.execute(select(ModelPrice).order_by(ModelPrice.provider))).scalars().all()
    policies = (await session.execute(select(RoutePolicy))).scalars().all()
    return {
        "prices": [
            {
                "provider": p.provider,
                "model": p.model,
                "input_price_per_1k": _money(p.input_price_per_1k),
                "output_price_per_1k": _money(p.output_price_per_1k),
                "context_window": p.context_window,
                "enabled": p.enabled,
            }
            for p in prices
        ],
        "policies": [
            {
                "match_model": p.match_model,
                "strategy": p.strategy,
                "candidates": p.candidates,
                "fallback": p.fallback,
                "enabled": p.enabled,
            }
            for p in policies
        ],
    }


@router.get("/audit")
async def audit(
    limit: int = Query(100, ge=1, le=500),
    action: str | None = None,
    actor: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Newest first. Append-only: there is no endpoint to edit or delete audit rows."""
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if actor:
        stmt = stmt.where(AuditLog.actor == actor)
    rows = (await session.execute(stmt)).scalars().all()
    return {
        "items": [
            {
                "id": row.id,
                "actor": row.actor,
                "actor_role": row.actor_role,
                "action": row.action,
                "target": row.target,
                "method": row.method,
                "status_code": row.status_code,
                "ip": row.ip,
                "request_id": row.request_id,
                "detail": row.detail,
                "created_at": _iso(row.created_at),
            }
            for row in rows
        ],
        "count": len(rows),
    }


@router.get("/stats")
async def stats(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    total = (await session.execute(select(func.count()).select_from(UsageRecord))).scalar_one()
    return {"usage_records": total, "generated_at": _iso(utcnow())}
