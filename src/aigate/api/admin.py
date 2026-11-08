"""Control plane: what the console reads.

Auth is a single admin token (`X-Admin-Token`). That is deliberately crude: a real
deployment wants SSO + per-tenant scoping, and pretending otherwise with a half-built
RBAC would be worse than being explicit about the gap (see README "known limitations").

Aggregation note: the overview sums rows in Python rather than in SQL. At demo scale
(thousands of rows) that is microseconds and keeps the code backend-agnostic; at a
million rows it belongs in a materialised daily table. The comment is the honest part.
"""

from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aigate import timeutil
from aigate.api.deps import Actor, get_actor, require_admin, require_capability
from aigate.config import get_settings
from aigate.db import get_session
from aigate.models import (
    Alert,
    ApiKey,
    AuditLog,
    ModelPrice,
    Project,
    RequestTrace,
    RoutePolicy,
    ShadowRun,
    Team,
    UsageRecord,
    as_aware,
    utcnow,
)
from aigate.security import generate_api_key
from aigate.usage_stats import count_success, is_success, success_rate

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


def _p95(values: list[int]) -> int | None:
    """Linear-interpolated p95.

    Plain "take the k-th item" breaks on small samples: with 16 requests a single slow
    call sits outside the index, so p95 comes out *below* the mean and the dashboard
    looks broken. Interpolating between neighbours gives a number a human would accept.
    """
    if not values:
            return None
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
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_capability(actor, "costs")
    now = utcnow()
    # Window = the last N *local* calendar days including today. Two decisions here:
    #   - 自然日而不是 `now - N days`：否则时钟不是刚好 0 点时图里会多一个桶，
    #     合计与柱子对不上（只在截图时发现的 bug）。
    #   - 本地日而不是 UTC 日：中国团队早上 8 点前看"今日"会看到昨天。
    tz_name = get_settings().timezone
    since = timeutil.day_start(tz_name, days_back=days - 1, now=now)
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

    today = timeutil.local_date(now, tz_name)
    today_rows = [
        r
        for r in rows
        if _aware(r.created_at)
        and timeutil.local_date(_aware(r.created_at), tz_name) == today
    ]

    def summary(subset: list[UsageRecord]) -> dict[str, Any]:
        total = len(subset)
        # 成功率的白名单/分母/精度全部来自 aigate.usage_stats，与 /model-compare
        # （模型对比页）同源：同一批 usage 行在两个端点上只能是同一个数。
        # 空窗口时 success_rate 是 None，不是 100.0——"没有调用"不是"全成功"。
        ok, total = count_success(r.status for r in subset)
        errors = total - ok
        cached = sum(1 for r in subset if r.status == "cached")
        # 没有调用 ≠ 100% 成功、0 ms。
        # 之前 `if total else 100.0` 让一个空库的第一屏写着"成功率 100.0% / P95 0 ms"，
        # 值班读者会认为"服务很快且全成功"，而事实是**窗口内没有任何数据**。
        # 比率与延迟在 total==0 时是未知，返回 null；前端渲染「—」。
        # `has_data` 是给前端的显式信号（不必靠 null 猜），也是测试的锚点。
        return {
            "requests": total,
            "errors": errors,
            "cached": cached,
            "has_data": total > 0,
            "success_rate": success_rate(ok, total),
            "cache_hit_rate": round(cached / total * 100, 2) if total else None,
            "cost_cny": _money(sum((r.cost_cny for r in subset), Decimal("0"))),
            "avg_latency_ms": round(sum(r.latency_ms for r in subset) / total, 1) if total else None,
            "p95_latency_ms": _p95([r.latency_ms for r in subset]),
        }

    # Backfill every day in the window: a chart labelled "last 7 days" that only shows
    # days with traffic is quietly lying about the shape of the data.
    series_map: dict[str, dict[str, Any]] = {}
    for offset in range(days - 1, -1, -1):
        day = timeutil.local_date(now - timedelta(days=offset), tz_name)
        series_map[day] = {"requests": 0, "cost_cny": 0.0, "errors": 0}

    for row in rows:
        aware = _aware(row.created_at)
        if aware is None:
            continue
        day = timeutil.local_date(aware, tz_name)
        bucket = series_map.setdefault(day, {"requests": 0, "cost_cny": 0.0, "errors": 0})
        bucket["requests"] += 1
        bucket["cost_cny"] += float(row.cost_cny or 0)
        if row.status == "error":
            bucket["errors"] += 1

    team_counter: Counter[str] = Counter()
    team_cost: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in rows:
        team_counter[row.team_id] += 1
        team_cost[row.team_id] += row.cost_cny or Decimal("0")
    team_names = {t.id: t.slug for t in (await session.execute(select(Team))).scalars().all()}

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
        "by_team": [
            {
                "team": team_names.get(tid, tid),
                "requests": count,
                "cost_cny": _money(team_cost[tid]),
            }
            for tid, count in team_counter.most_common()
        ],
        "open_alerts": (
            await session.execute(
                select(func.count())
                .select_from(Alert)
                .where(Alert.acknowledged_at.is_(None), Alert.resolved_at.is_(None))
            )
        ).scalar_one(),
    }


@router.get("/usage")
async def usage(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: str | None = Query(None, alias="status"),
    model: str | None = None,
    project_id: str | None = None,
    team_id: str | None = None,
    prompt_version: str | None = None,
    hours: int | None = Query(None, ge=1, le=24 * 90),
    only_cached: bool | None = None,
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    # A developer sees the rows of the projects they were put in — and nothing else.
    # Enforced here, not in the UI: the console is not the security boundary.
    scope = actor.scoped_projects
    if scope is None:
        require_capability(actor, "usage:all")
    elif not scope:
        require_capability(actor, "usage:own")

    stmt = select(UsageRecord).order_by(UsageRecord.created_at.desc()).offset(offset).limit(limit)
    if status_filter:
        stmt = stmt.where(UsageRecord.status == status_filter)
    if model:
        stmt = stmt.where(UsageRecord.model_requested == model)
    if project_id:
        stmt = stmt.where(UsageRecord.project_id == project_id)
    if team_id:
        stmt = stmt.where(UsageRecord.team_id == team_id)
    if prompt_version:
        stmt = stmt.where(UsageRecord.prompt_version == prompt_version)
    if hours:
        stmt = stmt.where(UsageRecord.created_at >= utcnow() - timedelta(hours=hours))
    if only_cached:
        stmt = stmt.where(UsageRecord.cache_hit.is_not(None))
    if scope is not None:
        stmt = stmt.where(UsageRecord.project_id.in_(scope))

    rows = (await session.execute(stmt)).scalars().all()
    # A total for "is there another page?" without a second round trip on every row.
    count_stmt = select(func.count()).select_from(UsageRecord)
    if status_filter:
        count_stmt = count_stmt.where(UsageRecord.status == status_filter)
    if model:
        count_stmt = count_stmt.where(UsageRecord.model_requested == model)
    if project_id:
        count_stmt = count_stmt.where(UsageRecord.project_id == project_id)
    if team_id:
        count_stmt = count_stmt.where(UsageRecord.team_id == team_id)
    if prompt_version:
        count_stmt = count_stmt.where(UsageRecord.prompt_version == prompt_version)
    if hours:
        count_stmt = count_stmt.where(UsageRecord.created_at >= utcnow() - timedelta(hours=hours))
    if scope is not None:
        count_stmt = count_stmt.where(UsageRecord.project_id.in_(scope))
    total = (await session.execute(count_stmt)).scalar_one()
    return {
        "items": [
            {
                "request_id": r.request_id,
                "team_id": r.team_id,
                "project_id": r.project_id,
                "prompt_version": r.prompt_version,
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
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/traces")
async def list_traces(
    limit: int = Query(20, ge=1, le=200),
    status_filter: str | None = Query(None, alias="status"),
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """最近的调用链。

    详情页按 request_id 查，但"最近哪些请求失败了"是值班的第一句话——
    没有列表就只能去翻日志。trace 表只存诊断信息（prompt、路由路径、HTTP 状态），
    provider / 成本 / 延迟这些在 usage_records 里，所以这里做一次左连接：
    账单可能还没落库（异步记账），那样就退化成一个只有状态与耗时的行。
    """
    require_capability(actor, "trace:all")
    stmt = (
        select(RequestTrace, UsageRecord)
        .outerjoin(UsageRecord, UsageRecord.request_id == RequestTrace.request_id)
        .order_by(RequestTrace.created_at.desc())
        .limit(limit)
    )
    if status_filter:
        stmt = stmt.where(UsageRecord.status == status_filter)
    rows = (await session.execute(stmt)).all()

    items = []
    for trace, usage in rows:
        path = trace.route_path or []
        last = path[-1] if path else {}
        items.append(
            {
                "request_id": trace.request_id,
                "route": trace.model_requested,
                "provider": (usage.provider if usage else last.get("provider")),
                "model": (usage.model_used if usage else last.get("model")),
                "status": usage.status if usage else ("ok" if trace.http_status < 400 else "error"),
                "error_code": usage.error_code if usage else None,
                "latency_ms": usage.latency_ms if usage else (trace.stage_timings or {}).get("gateway_total"),
                "cache_hit": usage.cache_hit if usage else None,
                "degraded": usage.degraded if usage else None,
                "cost_cny": _money(usage.cost_cny) if usage and usage.cost_cny is not None else None,
                "created_at": _iso(trace.created_at),
            }
        )
    return {"items": items, "count": len(items)}


@router.get("/traces/{request_id}")
async def trace(
    request_id: str,
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = (
        await session.execute(select(RequestTrace).where(RequestTrace.request_id == request_id).limit(1))
    ).scalar_one_or_none()
    scope = actor.scoped_projects
    if scope is None:
        require_capability(actor, "trace:all")
    elif not scope:
        require_capability(actor, "trace:own")
    if row is None or (scope is not None and row.project_id not in scope):
        # 404 rather than 403: whether a request id exists in another team's project is
        # itself information we are not handing out.
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
        # NULL unless AIGATE_TRACE_PROMPT_MODE=raw
        "prompt_raw": row.prompt_raw,
        "prompt_version": row.prompt_version,
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
async def list_keys(
    actor: Actor = Depends(get_actor),
    status_filter: str | None = Query(None, alias="status"),
    q: str | None = Query(None, description="按名称 / 归属人 / 描述模糊搜索"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    scope = actor.scoped_projects
    if scope is None:
        require_capability(actor, "keys:read")
    else:
        require_capability(actor, "keys:own")
    rows = (
        await session.execute(
            select(ApiKey, Project, Team)
            .join(Project, ApiKey.project_id == Project.id)
            .join(Team, Project.team_id == Team.id)
            .order_by(ApiKey.created_at.desc())
        )
    ).all()
    if scope is not None:
        rows = [r for r in rows if r[0].project_id in scope]
    if status_filter:
        rows = [r for r in rows if r[0].status == status_filter]
    if q:
        needle = q.strip().lower()

        def _hit(row: tuple[ApiKey, Project, Team]) -> bool:
            key_row = row[0]
            haystack = " ".join(
                [key_row.name, key_row.created_by_email, key_row.description, key_row.prefix]
            ).lower()
            return needle in haystack

        rows = [r for r in rows if _hit(r)]

    now = utcnow()
    items = []
    for key, project, team in rows:
        # A key nobody has used is the most common leak: it sits in a .env for two years.
        idle_days = None
        last_used = as_aware(key.last_used_at)
        created = as_aware(key.created_at)
        if last_used is not None:
            idle_days = (now - last_used).days
        elif created is not None:
            idle_days = (now - created).days
        items.append(
            {
                "id": key.id,
                "name": key.name,
                "prefix": key.prefix,
                "team": team.slug,
                "project": project.slug,
                "project_id": project.id,
                "status": key.status,
                "allowed_models": key.allowed_models,
                "created_at": _iso(key.created_at),
                "last_used_at": _iso(key.last_used_at),
                "revoked_at": _iso(key.revoked_at),
                "created_by_email": key.created_by_email,
                "description": key.description,
                "environment": key.environment,
                "expires_at": _iso(key.expires_at),
                "expired": bool(as_aware(key.expires_at) and as_aware(key.expires_at) <= now),
                "revoked_reason": key.revoked_reason,
                "idle_days": idle_days,
            }
        )
    return {"items": items}


@router.post("/keys", status_code=status.HTTP_201_CREATED)
async def create_key(
    actor: Actor = Depends(get_actor),
    project_id: str = Query(...),
    name: str = Query("console-key"),
    allowed_models: str = Query("", description="逗号分隔；留空=跟随项目路由"),
    environment: str = Query("dev", description="dev | staging | prod"),
    expires_in_days: int | None = Query(None, ge=1, le=3650),
    description: str = Query("", max_length=200),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Issue a key.

    Two ways in, deliberately:

    - `keys:write` (admin/owner) — any project.
    - `keys:own` (developer) — *their own* project only. A developer waiting for an owner
      to paste a key into a chat is how keys end up in chat logs.
    """
    if not actor.can("keys:write"):
        scope = actor.scoped_projects
        if not actor.can("keys:own") or scope is None or project_id not in scope:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "gateway.key_scope_denied",
                    "message": "you can only issue keys for projects assigned to you",
                },
            )

    project = (await session.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "gateway.project_not_found", "message": project_id},
        )
    if project.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "gateway.project_paused",
                "message": f"project {project.slug} is {project.status}; resume it first",
            },
        )

    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "gateway.key_name_required", "message": "name cannot be empty"},
        )
    if len(clean_name) > 120:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "gateway.key_name_too_long", "message": "name must be <= 120 chars"},
        )
    if environment not in ("dev", "staging", "prod"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "gateway.key_bad_environment",
                "message": "environment must be dev, staging or prod",
            },
        )
    # A prod key with no expiry is the one nobody ever rotates.
    if environment == "prod" and expires_in_days is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "gateway.key_prod_needs_expiry",
                "message": "prod keys must have expires_in_days (rotation is not optional)",
            },
        )

    # Same project + same name + still active = almost always a double-click or a
    # forgotten key. Refuse instead of creating a second one nobody can tell apart.
    duplicate = (
        await session.execute(
            select(ApiKey).where(
                ApiKey.project_id == project.id,
                ApiKey.name == clean_name,
                ApiKey.status == "active",
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "gateway.key_name_taken",
                "message": f"an active key named '{clean_name}' already exists in {project.slug}",
                "existing_id": duplicate.id,
            },
        )

    # "a, b ,,c" -> ["a","b","c"]; the strip is the whole point — a stray space makes the
    # allow-list silently reject every request with `model_not_allowed`.
    models = [m.strip() for m in allowed_models.split(",") if m.strip()]

    raw, prefix, digest = generate_api_key()
    key = ApiKey(
        project_id=project.id,
        name=clean_name,
        prefix=prefix,
        key_hash=digest,
        allowed_models=models,
        created_by=actor.user_id or "",
        created_by_email=actor.name,
        description=description.strip(),
        environment=environment,
        expires_at=utcnow() + timedelta(days=expires_in_days) if expires_in_days else None,
    )
    session.add(key)
    await session.commit()

    # The plaintext key is returned exactly once — there is no endpoint to read it back.
    return {
        "id": key.id,
        "api_key": raw,
        "prefix": prefix,
        "name": key.name,
        "environment": key.environment,
        "expires_at": _iso(key.expires_at),
        "created_by_email": key.created_by_email,
        "note": "shown once, not recoverable",
    }


@router.post("/keys/{key_id}/revoke")
async def revoke_key(
    key_id: str,
    actor: Actor = Depends(get_actor),
    reason: str = Query("", max_length=200),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Revoke. Owners/admins can revoke anything; a developer only what they issued.

    The rule is "you can undo your own mistake, you cannot undo someone else's" — the
    alternative (anyone with keys:own can revoke any key) turns a read-only role into a
    denial-of-service tool.
    """
    key = (await session.execute(select(ApiKey).where(ApiKey.id == key_id))).scalar_one_or_none()
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "gateway.key_not_found", "message": key_id},
        )

    allowed = actor.can("keys:write")
    if not allowed:
        scope = actor.scoped_projects
        allowed = (
            actor.can("keys:own")
            and scope is not None
            and key.project_id in scope
            and key.created_by
            and key.created_by == actor.user_id
        )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "gateway.key_revoke_denied",
                "message": "you can only revoke keys you issued yourself",
            },
        )

    if key.status == "revoked":
        return {"id": key.id, "status": key.status, "already": True}

    key.status = "revoked"
    key.revoked_at = utcnow()
    key.revoked_reason = reason.strip()
    key.revoked_by = actor.name
    await session.commit()
    return {
        "id": key.id,
        "status": key.status,
        "revoked_by": key.revoked_by,
        "revoked_reason": key.revoked_reason,
    }


@router.get("/projects")
async def list_projects(
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(Project, Team).join(Team, Project.team_id == Team.id).order_by(Team.slug, Project.slug)
        )
    ).all()
    scope = actor.scoped_projects
    if scope is not None:
        rows = [r for r in rows if r[0].id in scope]
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


@router.get("/model-compare")
async def model_compare(
    days: int = Query(7, ge=1, le=90),
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """质量-成本对照：用真实调用回答「换更便宜的模型值不值」。

    这不是 benchmark：只有跑过影子流量的模型才有质量轴（similarity），
    其余的 `similarity` 为 null——不拿行业平均值冒充自己的数据。
    """
    require_capability(actor, "usage:all")
    since = utcnow() - timedelta(days=days)
    rows = (
        (await session.execute(select(UsageRecord).where(UsageRecord.created_at >= since)))
        .scalars()
        .all()
    )
    shadows = (
        (await session.execute(select(ShadowRun).where(ShadowRun.created_at >= since)))
        .scalars()
        .all()
    )

    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        bucket = buckets.setdefault(
            (r.provider, r.model_used), {"n": 0, "ok": 0, "cost": 0.0, "lat": [], "tokens": 0}
        )
        bucket["n"] += 1
        # 与概览同一份白名单（aigate.usage_stats.is_success）：未知状态算未成功。
        bucket["ok"] += 1 if is_success(r.status) else 0
        bucket["cost"] += float(r.cost_cny or 0)
        bucket["lat"].append(r.latency_ms)
        bucket["tokens"] += (r.input_tokens or 0) + (r.output_tokens or 0)

    shadow_map: dict[tuple[str, str], dict[str, Any]] = {}
    for s_row in shadows:
        bucket = shadow_map.setdefault((s_row.provider, s_row.model), {"n": 0, "sims": []})
        bucket["n"] += 1
        if s_row.similarity is not None:
            bucket["sims"].append(float(s_row.similarity))

    items = []
    for (provider, model), b in buckets.items():
        shadow = shadow_map.get((provider, model))
        per_call = b["cost"] / b["n"] if b["n"] else 0.0
        items.append(
            {
                "provider": provider,
                "model": model,
                "calls": b["n"],
                # 无调用 → null（未知），不是 0.0；精度与概览一致。
                "success_rate": success_rate(b["ok"], b["n"]),
                "p95_ms": _p95(b["lat"]),
                "avg_ms": round(sum(b["lat"]) / len(b["lat"]), 1) if b["lat"] else 0,
                "cost_per_call": round(per_call, 6),
                "cost_per_1k": round(per_call * 1000, 2),
                "avg_tokens": round(b["tokens"] / b["n"]) if b["n"] else 0,
                "similarity": (
                    round(sum(shadow["sims"]) / len(shadow["sims"]), 2)
                    if shadow and shadow["sims"]
                    else None
                ),
                "shadow_runs": shadow["n"] if shadow else 0,
            }
        )
    items.sort(key=lambda item: item["cost_per_1k"])
    return {"days": days, "items": items}


@router.get("/models")
async def list_models(
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_capability(actor, "models:read")
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


@router.get("/audit.csv")
async def export_audit(
    action: str | None = None,
    actor_name: str | None = Query(None, alias="actor"),
    limit: int = Query(5000, ge=1, le=50000),
    viewer: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """审计导出。只增不改的表，导出的意义是"带走对账/交接"，不是备份。

    审计行里没有密钥、没有 prompt —— 只有"谁、什么时候、做了什么、结果如何"。
    """
    require_capability(viewer, "audit:read")
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if actor_name:
        stmt = stmt.where(AuditLog.actor == actor_name)
    rows = (await session.execute(stmt)).scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "created_at",
            "actor",
            "role",
            "action",
            "target",
            "method",
            "path",
            "status_code",
            "ip",
            "request_id",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                _iso(r.created_at),
                r.actor,
                r.actor_role,
                r.action,
                r.target,
                r.method,
                r.path,
                r.status_code,
                r.ip,
                r.request_id,
            ]
        )
    stamp = utcnow().strftime("%Y%m%d-%H%M")
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="aigate-audit-{stamp}.csv"',
            "X-Export-Rows": str(len(rows)),
        },
    )


@router.get("/audit")
async def audit(
    limit: int = Query(100, ge=1, le=500),
    action: str | None = None,
    # the query parameter is still `?actor=`; the local name must not shadow the dependency
    actor_name: str | None = Query(None, alias="actor"),
    viewer: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Newest first. Append-only: there is no endpoint to edit or delete audit rows."""
    require_capability(viewer, "audit:read")
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if actor_name:
        stmt = stmt.where(AuditLog.actor == actor_name)
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
async def stats(
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_capability(actor, "audit:read")
    total = (await session.execute(select(func.count()).select_from(UsageRecord))).scalar_one()
    return {"usage_records": total, "generated_at": _iso(utcnow())}
