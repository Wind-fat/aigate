"""Data plane: POST /v1/chat/completions.

Pipeline (Stage 2):
  auth(dep) -> model scope -> rate limit -> budget -> PII mask -> route
  -> [non-stream] provider.chat        -> cost -> usage + trace -> json
  -> [stream]     provider.stream(SSE) -> usage + trace -> event stream
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aigate import metering_queue as meter
from aigate import metrics, timeutil
from aigate import shadow as shadow_traffic
from aigate.api.deps import Principal, get_principal
from aigate.config import Settings, get_settings
from aigate.core import circuit, incidents, quota
from aigate.core.cache import get_cache
from aigate.db import get_session, get_session_factory
from aigate.guard import mask_messages
from aigate.metering import compute_cost, estimate_messages_tokens, estimate_tokens, mask_pii
from aigate.metering_queue import UsageEvent
from aigate.models import ModelPrice, RoutePolicy, UsageRecord, utcnow
from aigate.provider_config import get as get_provider_config
from aigate.providers import ProviderError, ProviderUsage, get_configured_provider, get_provider
from aigate.schemas import SUPPORTED_PARAMS, ChatCompletionRequest

router = APIRouter(prefix="/v1", tags=["data-plane"])

DEFAULT_POLICY = "__default__"


def _ms(since: float) -> int:
    return int((time.perf_counter() - since) * 1000)


def _content_to_text(messages: list[dict]) -> str:
    parts: list[str] = []
    for m in messages:
        c = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts.append(str(c))
    return "\n".join(parts)


def _check_model_scope(model: str, principal: Principal) -> None:
    allowed = principal.allowed_models
    if allowed and model not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "gateway.model_not_allowed",
                "message": f"key is not allowed to use model '{model}'",
            },
        )


def _reject_unsupported_parameters(payload: ChatCompletionRequest) -> None:
    """Refuse request fields the gateway would otherwise accept and drop.

    This runs *first* in the handler — before auth-dependent work, before the rate
    limiter, before any upstream call — because the one thing worse than a 400 is a 200
    that quietly ignored `tools`, `stop` or `top_p`. `model_extra` holds everything the
    whitelist in `schemas.SUPPORTED_PARAMS` does not name; that set is exactly what a
    caller can rely on.
    """
    extra = sorted(payload.model_extra or {})
    if extra:
        supported = ", ".join(SUPPORTED_PARAMS)
        # No parameter name in the label on purpose: that would be one time series per
        # string a client sends us, i.e. an unbounded metric from untrusted input.
        metrics.inc("aigate_unsupported_parameter_total")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "gateway.unsupported_parameter",
                "message": (
                    f"unsupported parameter(s): {', '.join(extra)}. "
                    f"This gateway forwards only: {supported}. "
                    "Refused instead of ignored, so a client never gets a 200 for a "
                    "parameter that had no effect."
                ),
                "unsupported_parameters": extra,
                "supported_parameters": list(SUPPORTED_PARAMS),
            },
        )

    # A top_p outside [0, 1] is a client bug on every vendor; catching it here gives a
    # stable code instead of an upstream_4xx that names the vendor's own field.
    if payload.top_p is not None and not 0.0 <= payload.top_p <= 1.0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "gateway.invalid_parameter",
                "message": f"top_p must be within [0, 1], got {payload.top_p}",
                "parameter": "top_p",
            },
        )

    # `stream_options` is honoured locally (the usage chunk), so its inner keys have to be
    # checked by hand: a dict field swallows anything, and `{"include_usage": "yes"}`
    # silently meaning "no" is exactly the kind of quiet lie this file exists to avoid.
    if payload.stream_options is not None:
        unknown = sorted(k for k in payload.stream_options if k != "include_usage")
        if unknown:
            metrics.inc("aigate_unsupported_parameter_total")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "gateway.unsupported_parameter",
                    "message": (
                        f"unsupported parameter(s): {', '.join('stream_options.' + k for k in unknown)}. "
                        "stream_options supports only include_usage (boolean)."
                    ),
                    "unsupported_parameters": [f"stream_options.{k}" for k in unknown],
                    "supported_parameters": list(SUPPORTED_PARAMS),
                },
            )
        include_usage = payload.stream_options.get("include_usage", False)
        if not isinstance(include_usage, bool):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "gateway.invalid_parameter",
                    "message": f"stream_options.include_usage must be a boolean, got {include_usage!r}",
                    "parameter": "stream_options.include_usage",
                },
            )


def _finish_reason(upstream: str | None, payload: ChatCompletionRequest, output_tokens: int) -> str:
    """Say why the answer ended instead of always claiming "stop".

    Pass-through first: when the vendor named a reason — `stop`, `length`,
    `content_filter`, `tool_calls`, or a vendor-specific string — that *is* the truth and
    the gateway has no business overwriting it. Only literal null-ish values are treated
    as "the vendor said nothing", because several Chinese vendors put `"null"` there.

    Derivation is a fallback and is documented as a heuristic, not as OpenAI semantics:
    if the answer used up `max_tokens`, it was cut off (`length`); otherwise the model
    finished on its own (`stop`). Strictly more informative than the flat "stop" it
    replaces, and wrong only in the direction of "claims finished when truncated" for a
    vendor that reports neither a reason nor token counts.
    """
    if upstream is not None:
        cleaned = upstream.strip()
        if cleaned and cleaned.lower() not in {"null", "none", "unknown"}:
            return cleaned
    if payload.max_tokens is not None and output_tokens >= payload.max_tokens:
        return "length"
    return "stop"


def _wants_usage(payload: ChatCompletionRequest) -> bool:
    return bool((payload.stream_options or {}).get("include_usage"))


async def _order_by_strategy(
    session: AsyncSession, policy: RoutePolicy, primaries: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Apply the policy's strategy to the *primary* candidates.

    Fallbacks are never reordered: they are a fixed, deliberate order ("if qwen fails,
    go to deepseek, then to the local model"). Reordering them by cost would quietly
    change which vendor you fall back to, which is exactly the thing that must not drift.

    - static: as configured (a business that pins a model means it)
    - cost_first: cheapest blended price first; unpriced models go last
    - latency_first: fastest p95 over the last 24h first; models with no history go last,
      because "unknown" is not "fast"
    """
    strategy = (policy.strategy or "static").lower()
    if strategy == "static" or len(primaries) < 2:
        return primaries

    if strategy == "cost_first":
        prices = {
            (p.provider, p.model): float(p.input_price_per_1k) + float(p.output_price_per_1k)
            for p in (await session.execute(select(ModelPrice).where(ModelPrice.enabled.is_(True)))).scalars()
        }
        return sorted(primaries, key=lambda c: prices.get((c["provider"], c["model"]), 1e9))

    if strategy == "latency_first":
        since = utcnow() - timedelta(hours=24)
        rows = (
            await session.execute(
                select(
                    UsageRecord.provider,
                    UsageRecord.model_used,
                    func.avg(UsageRecord.latency_ms),
                )
                .where(
                    UsageRecord.created_at >= since,
                    UsageRecord.status.in_(("ok", "fallback")),
                )
                .group_by(UsageRecord.provider, UsageRecord.model_used)
            )
        ).all()
        avg = {(r[0], r[1]): float(r[2] or 0) for r in rows}
        return sorted(primaries, key=lambda c: avg.get((c["provider"], c["model"]), 1e9))

    return primaries


async def _resolve_candidates(
    session: AsyncSession, virtual_model: str, settings: Settings
) -> list[dict[str, str]]:
    """Virtual model -> ordered provider candidates. Fallbacks are explicit, never implicit."""
    stmt = select(RoutePolicy).where(RoutePolicy.match_model == virtual_model, RoutePolicy.enabled.is_(True))
    policy = (await session.execute(stmt)).scalar_one_or_none()

    if policy is None:
        default_policy = (
            await session.execute(
                select(RoutePolicy).where(
                    RoutePolicy.match_model == DEFAULT_POLICY, RoutePolicy.enabled.is_(True)
                )
            )
        ).scalar_one_or_none()
        if default_policy is not None:
            primaries = list(default_policy.candidates or [])
            if primaries:
                ordered = await _order_by_strategy(session, default_policy, primaries)
                return ordered + list(default_policy.fallback or [])
        return [{"provider": settings.default_provider, "model": virtual_model}]

    primaries = list(policy.candidates or [])
    ordered = await _order_by_strategy(session, policy, primaries)
    return ordered + list(policy.fallback or [])


async def _price_for(session: AsyncSession, provider: str, model: str) -> ModelPrice | None:
    stmt = (
        select(ModelPrice)
        .where(ModelPrice.provider == provider, ModelPrice.model == model, ModelPrice.enabled.is_(True))
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _estimate_cost(price: ModelPrice | None, input_tokens: int, output_tokens: int) -> Decimal | None:
    if price is None:
        return None
    return compute_cost(input_tokens, output_tokens, price.input_price_per_1k, price.output_price_per_1k)


@router.get("/models")
async def list_models(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Only models this key may actually call — the list is a permission surface."""
    allowed = principal.allowed_models
    policies = (
        (await session.execute(select(RoutePolicy).where(RoutePolicy.enabled.is_(True)))).scalars().all()
    )
    return {
        "object": "list",
        "data": [
            {
                "id": p.match_model,
                "object": "model",
                "owned_by": "aigate",
                "candidates": p.candidates,
                "strategy": p.strategy,
            }
            for p in policies
            if p.match_model != DEFAULT_POLICY and (not allowed or p.match_model in allowed)
        ],
    }


async def _maybe_shadow(
    ctx: _Ctx, primary_text: str, session: AsyncSession | None = None
) -> None:
    """Fire the shadow calls configured on this route, after the answer is ready.

    Deliberately after: the caller waits for the primary only, and a slow shadow model
    must never turn into a slow response.

    `session` is the *request's* session, passed by the non-streaming path. Borrowing it
    costs nothing (one SELECT on a row this request just read) and keeps the connection
    demand at one per in-flight request. Opening a second session here instead was
    measurably expensive: 8 concurrent requests then ask for 16 connections from a
    5+10 pool, overflow connections get recycled, and every fresh asyncpg connection on
    this host costs ~100-130 ms (see docs/PERF-PG.md). The streaming path still passes
    nothing and gets its own session, because it runs when the request session is gone.
    """
    async def _shadow_candidates(use) -> list[dict]:
        policy = (
            await use.execute(
                select(RoutePolicy).where(RoutePolicy.match_model == ctx.payload.model)
            )
        ).scalar_one_or_none()
        return list(getattr(policy, "shadow", None) or []) if policy else []

    if session is not None:
        candidates = await _shadow_candidates(session)
    else:
        async with get_session_factory()() as bg_session:
            candidates = await _shadow_candidates(bg_session)
    if not candidates:
        return
    shadow_traffic.schedule(
        get_session_factory(),
        request_id=ctx.request_id,
        project_id=ctx.principal.project.id,
        route=ctx.payload.model,
        messages=ctx.messages,
        temperature=ctx.payload.temperature,
        max_tokens=ctx.payload.max_tokens,
        # Shadow runs mirror the sampling parameters: comparing two models that sampled
        # differently answers the wrong question.
        top_p=ctx.payload.top_p,
        stop=ctx.payload.stop,
        enable_thinking=ctx.payload.enable_thinking,
        primary_text=primary_text,
        candidates=candidates,
        settings=ctx.settings,
    )


async def _provider_for(provider_name: str, session: Any, settings: Any):
    """Resolve a provider through the console-managed config, falling back to env.

    The console page 供应商 can change base_url, disable a vendor, or point it at another
    endpoint; without this indirection those edits would need a restart to matter.
    """
    cfg = await get_provider_config(session, settings, provider_name)
    if cfg is not None:
        if not cfg.enabled:
            raise ProviderError("provider_disabled", f"provider {provider_name} is disabled")
        if not cfg.configured:
            raise ProviderError(
                "provider_unconfigured",
                f"provider {provider_name} has no api key ({cfg.api_key_env or 'env not set'})",
            )
        if cfg.origin == "default":
            # Nothing was edited in the console: keep the process-wide instance so
            # circuit-breaker state and mock counters stay shared.
            return get_provider(cfg.name, settings)
        return get_configured_provider(cfg.name, cfg.base_url, cfg.api_key, settings.request_timeout_s)
    raise ProviderError("provider_unknown", f"unknown provider: {provider_name}")


@router.post("/chat/completions")
async def chat_completions(
    payload: ChatCompletionRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    started = time.perf_counter()
    request_id = getattr(request.state, "request_id", f"req_{uuid.uuid4().hex[:20]}")
    # Before anything is spent: a parameter this gateway cannot honour must not come back
    # as a 200 (see `_reject_unsupported_parameters`).
    _reject_unsupported_parameters(payload)
    timings: dict[str, int] = {}
    route_path: list[dict[str, Any]] = []
    # Auth is a dependency, so it runs *before* this function: the only honest measure is
    # "how long after the middleware started did the handler get here". This is the three
    # SELECTs of get_principal (api_key -> project -> team) plus routing/Pydantic.
    t_entry = getattr(request.state, "started", None)
    if t_entry is not None:
        timings["auth_ms"] = int((started - t_entry) * 1000)

    # A paused project keeps its keys and its history but stops spending money. This is
    # the "we are migrating / the demo is over" switch, and it has to be one click.
    if principal.project.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "gateway.project_paused",
                "message": f"project '{principal.project.slug}' is {principal.project.status}",
            },
        )

    _check_model_scope(payload.model, principal)
    timings["scope_ms"] = _ms(started)

    # --- 1. rate limit: key -> project -> team ---------------------------
    # Narrowest first: the message should name the smallest thing the caller can fix.
    t = time.perf_counter()
    limiter = quota.get_limiter(settings.rate_limiter, settings.redis_url)

    key_rpm = principal.api_key.rpm_limit or 0
    if key_rpm > 0:
        key_rl = await limiter.hit(f"key:{principal.api_key.id}", key_rpm, 60)
        if not key_rl.allowed:
            metrics.inc("aigate_rate_limited_total", project=principal.project.slug, scope="key")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "gateway.quota_exceeded",
                    "message": f"this API key is limited to {key_rpm}/min",
                    "scope": "key",
                    "retry_after_s": key_rl.retry_after_s,
                },
                headers={"Retry-After": str(key_rl.retry_after_s)},
            )

    team_rpm = principal.team.rpm_limit or 0
    if team_rpm > 0:
        team_rl = await limiter.hit(f"team:{principal.team.id}", team_rpm, 60)
        if not team_rl.allowed:
            metrics.inc("aigate_rate_limited_total", project=principal.project.slug, scope="team")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "gateway.quota_exceeded",
                    "message": f"team '{principal.team.slug}' is limited to {team_rpm}/min",
                    "scope": "team",
                    "retry_after_s": team_rl.retry_after_s,
                },
                headers={"Retry-After": str(team_rl.retry_after_s)},
            )

    rpm = principal.project.rpm_limit or settings.default_rpm
    rl = await limiter.hit(principal.project.id, rpm, 60)
    timings["quota_ms"] = _ms(t)
    if not rl.allowed:
        metrics.inc("aigate_rate_limited_total", project=principal.project.slug)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "gateway.quota_exceeded",
                "message": f"rate limit {rl.limit}/min exceeded",
                "retry_after_s": rl.retry_after_s,
            },
            headers={"Retry-After": str(rl.retry_after_s)},
        )

    # --- 2. budgets: project daily -> project monthly -> team monthly ------
    t = time.perf_counter()
    budget = await quota.check_budgets(
        session, principal.project, principal.team, tz_name=settings.timezone
    )
    timings["budget_ms"] = _ms(t)
    if not budget.allowed:
        # A daily cap that only warns is a cap that gets exceeded; refuse and say which
        # budget, because "budget exhausted" without a scope sends people to the wrong page.
        await quota.raise_alert_if_needed(
            session,
            scope_type=budget.scope,
            scope_id=budget.scope_id,
            scope_slug=budget.scope_slug,
            metric=budget.metric,
            spent=budget.spent,
            limit=budget.limit or Decimal("0"),
            period_key=budget.period_key,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "gateway.budget_exhausted",
                "message": (
                    f"{budget.scope} '{budget.scope_slug}' {budget.metric} exhausted: "
                    f"¥{budget.spent:.4f} of ¥{budget.limit:.4f}"
                ),
                "scope": budget.scope,
                "metric": budget.metric,
                "spent_cny": float(budget.spent),
                "limit_cny": float(budget.limit or 0),
            },
        )

    # Warn early: crossing 80% is the only moment where changing behaviour is cheap.
    for scope_type, scope_id, slug, limit_value, metric, since in (
        (
            "project",
            principal.project.id,
            principal.project.slug,
            principal.project.monthly_budget_cny,
            "budget_monthly",
            quota.month_start(tz_name=settings.timezone),
        ),
        (
            "team",
            principal.team.id,
            principal.team.slug,
            principal.team.monthly_budget_cny,
            "budget_monthly",
            quota.month_start(tz_name=settings.timezone),
        ),
    ):
        if limit_value is None:
            continue
        spent_now = (
            await quota.spend_since(session, since, project_id=scope_id)
            if scope_type == "project"
            else await quota.spend_since(session, since, team_id=scope_id)
        )
        await quota.raise_alert_if_needed(
            session,
            scope_type=scope_type,
            scope_id=scope_id,
            scope_slug=slug,
            metric=metric,
            spent=spent_now,
            limit=Decimal(str(limit_value)),
            # 账期键与花费窗口必须同一把尺子：`month_start` 按 AIGATE_TIMEZONE 切，
            # 所以 `local_date` 也必须拿同一个时区去格式化。漏传会退回 Asia/Shanghai，
            # 比上海靠东的时区（如 Asia/Tokyo）在跨月那一刻会算出上个月的键，
            # 让新月份的首条告警撞上上月的唯一索引。口径同 `core/quota.py:247`。
            period_key=timeutil.local_date(
                quota.month_start(tz_name=settings.timezone), settings.timezone
            )[:7],
        )

    # --- 3. PII masking (before anything leaves the gateway) -------------
    t = time.perf_counter()
    raw_messages = [m.model_dump(exclude_none=True) for m in payload.messages]
    messages, pii_hits = mask_messages(raw_messages)
    timings["guard_ms"] = _ms(t)
    if pii_hits:
        metrics.inc("aigate_pii_masked_total", spans=str(pii_hits))

    ctx = _Ctx(
        request_id=request_id,
        # A template change must invalidate the cache even though no code was deployed.
        prompt_version=(request.headers.get("x-prompt-version") or settings.prompt_version)[:40],
        principal=principal,
        settings=settings,
        payload=payload,
        messages=messages,
        raw_messages=raw_messages,
        candidates=[],
        timings=timings,
        route_path=route_path,
        pii_hits=pii_hits,
        started=started,
    )

    # --- 4. semantic cache: only single-shot, temperature=0, non-streaming ---
    cache = get_cache()
    if cache.eligible(payload):
        t = time.perf_counter()
        hit = await cache.lookup(
            session, principal.project.id, payload.model, payload.messages, ctx.prompt_version
        )
        timings["cache_ms"] = _ms(t)
        if hit is not None:
            metrics.inc("aigate_cache_served_total", layer=hit.layer)
            return await _serve_cache_hit(ctx, session, hit, response)

    t_route = time.perf_counter()
    ctx.candidates = await _resolve_candidates(session, payload.model, settings)
    timings["route_ms"] = _ms(t_route)

    if payload.stream:
        return _streaming_response(ctx, session)
    return await _complete(ctx, session, response)


# --------------------------------------------------------------------------
# context object (keeps the two paths from duplicating 12 arguments)
# --------------------------------------------------------------------------


class _Ctx:
    __slots__ = (
        "request_id",
        "prompt_version",
        "principal",
        "settings",
        "payload",
        "messages",
        "raw_messages",
        "candidates",
        "timings",
        "route_path",
        "pii_hits",
        "started",
    )

    def __init__(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


# --------------------------------------------------------------------------
# non-streaming
# --------------------------------------------------------------------------


async def _complete(ctx: _Ctx, session: AsyncSession, response: Response) -> dict[str, Any]:
    breaker = circuit.get_breaker()
    result = None
    chosen: dict[str, str] | None = None
    last_error: ProviderError | None = None
    degraded = False

    for idx, cand in enumerate(ctx.candidates):
        provider_name = cand.get("provider", ctx.settings.default_provider)
        model_name = cand.get("model", ctx.payload.model)
        allowed, reason = await breaker.allow(provider_name)
        if not allowed:
            ctx.route_path.append(
                {"attempt": idx + 1, "provider": provider_name, "skipped": reason, "ok": False}
            )
            continue

        attempt: dict[str, Any] = {
            "attempt": idx + 1,
            "provider": provider_name,
            "model": model_name,
            "ok": False,
        }
        t0 = time.perf_counter()
        try:
            t_cfg = time.perf_counter()
            provider = await _provider_for(provider_name, session, ctx.settings)
            ctx.timings["provider_cfg_ms"] = _ms(t_cfg)
            result = await provider.chat(
                model=model_name,
                messages=ctx.messages,
                temperature=ctx.payload.temperature,
                max_tokens=ctx.payload.max_tokens,
                top_p=ctx.payload.top_p,
                stop=ctx.payload.stop,
                user=ctx.payload.user,
                enable_thinking=ctx.payload.enable_thinking,
            )
            await breaker.record_success(provider_name)
            # 恢复也要记账：告警行靠它关闭（见 core/incidents.py 的语义）。
            await incidents.note_success(provider_name)
            attempt["ok"] = True
            attempt["latency_ms"] = _ms(t0)
            ctx.route_path.append(attempt)
            chosen = {"provider": provider_name, "model": model_name}
            degraded = idx > 0
            ctx.timings["upstream_ms"] = _ms(t0)
            break
        except ProviderError as exc:
            attempt["error"] = exc.code
            attempt["latency_ms"] = _ms(t0)
            ctx.route_path.append(attempt)
            last_error = exc
            if exc.retryable:
                await breaker.record_failure(provider_name)
                # 只对 retryable 计数：厂商 4xx 说明它活着（见 incidents 模块的 docstring）。
                await incidents.note_failure(
                    provider_name, exc.code, threshold=ctx.settings.upstream_alert_threshold
                )
            metrics.inc("aigate_upstream_errors_total", provider=provider_name, code=exc.code)
            if not exc.retryable:
                break
            continue

    total_ms = _ms(ctx.started)

    if result is None or chosen is None:
        code = last_error.code if last_error else "gateway.no_candidate"
        await _persist(
            ctx,
            provider=ctx.route_path[-1]["provider"] if ctx.route_path else ctx.settings.default_provider,
            model_used="",
            input_tokens=0,
            output_tokens=0,
            cost=None,
            latency_ms=total_ms,
            status="error",
            error_code=code,
            degraded=degraded,
            http_status=502,
        )
        metrics.inc("aigate_requests_total", status="error", code=code)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": code,
                "message": (last_error.message if last_error else "no provider available")[:300],
                "request_id": ctx.request_id,
            },
        )

    t_price = time.perf_counter()
    price = await _price_for(session, chosen["provider"], result.model) or await _price_for(
        session, chosen["provider"], chosen["model"]
    )
    ctx.timings["price_ms"] = _ms(t_price)
    cost = _estimate_cost(price, result.usage.input_tokens, result.usage.output_tokens)

    # `shadow_ms` used to be folded into `persist_ms` below; a shadow probe is a *read*
    # of the route policy on a second pooled connection, not persistence, and averaging
    # the two is how a 40 ms pool acquire hides inside "persist".
    t_shadow = time.perf_counter()
    await _maybe_shadow(ctx, result.content or "", session)
    ctx.timings["shadow_ms"] = _ms(t_shadow)

    t_persist = time.perf_counter()
    await _persist(
        ctx,
        provider=chosen["provider"],
        model_used=result.model,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        cost=cost,
        latency_ms=total_ms,
        status="fallback" if degraded else "ok",
        error_code=None,
        degraded=degraded,
        http_status=200,
    )
    # Persistence is deliberately excluded from `gateway_total` (that is the answer's
    # latency) but reported separately: hiding it made the response under-report real
    # request time by ~10x under load, which poisons any p95 investigation.
    ctx.timings["persist_ms"] = _ms(t_persist)

    # Cache only clean successes. A degraded answer came from the backup model and
    # caching it would silently pin lower-quality output for the whole TTL. A truncated
    # answer is refused too: the cached entry has no room for `finish_reason`, so a
    # `length` answer served from cache would be reported as a complete `stop`.
    finish_reason = _finish_reason(result.finish_reason, ctx.payload, result.usage.output_tokens)
    if not degraded and finish_reason == "stop" and cache_eligible_for_store(ctx.payload):
        await get_cache().store(
            session,
            ctx.principal.project.id,
            ctx.payload.model,
            ctx.payload.messages,
            provider=chosen["provider"],
            model_used=result.model,
            prompt_version=ctx.prompt_version,
            content=result.content,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
        )

    metrics.inc("aigate_requests_total", status="ok", provider=chosen["provider"])
    if degraded:
        metrics.inc("aigate_fallbacks_total", provider=chosen["provider"])
    response.headers["x-request-id"] = ctx.request_id

    return {
        "id": f"chatcmpl-{ctx.request_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": result.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": result.usage.input_tokens,
            "completion_tokens": result.usage.output_tokens,
            "total_tokens": result.usage.total_tokens,
        },
        "x_aigate": {
            "request_id": ctx.request_id,
            "team": ctx.principal.team.slug,
            "project": ctx.principal.project.slug,
            "provider": chosen["provider"],
            "model_requested": ctx.payload.model,
            "model_used": result.model,
            "degraded": degraded,
            "cache_hit": None,
            "pii_masked": ctx.pii_hits,
            # "upstream" when the vendor reported tokens; "missing" when it did not and
            # the numbers above are zeros. Never silently labelled as measured.
            "usage_source": "upstream" if result.usage.total_tokens > 0 else "missing",
            "cost_cny": float(cost) if cost is not None else 0.0,
            "priced": price is not None,
            "latency_ms": {"gateway_total": total_ms, **ctx.timings},
        },
    }


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------


def _sse(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _chunk(model: str, request_id: str, text: str, finish: str | None = None) -> str:
    return _sse(
        {
            "id": f"chatcmpl-{request_id}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text} if text else {}, "finish_reason": finish}],
        }
    )


def _usage_chunk(model: str, request_id: str, input_tokens: int, output_tokens: int) -> str:
    """OpenAI's `stream_options.include_usage` shape: `choices: []` plus `usage`.

    It goes out *after* the chunk carrying `finish_reason` and *before* `[DONE]`, which
    is the order the OpenAI SDK relies on to attach usage to the finished completion.
    This is also the only path that works: the `: aigate {...}` SSE comment is invisible
    to every SDK, so metadata in a comment is not usage as far as a client is concerned.
    """
    return _sse(
        {
            "id": f"chatcmpl-{request_id}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [],
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        }
    )


def _streaming_response(ctx: _Ctx, session: Any) -> StreamingResponse:
    return StreamingResponse(
        _event_stream(ctx, session),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx buffers SSE by default; this header is the difference between
            # "streaming works" and "streaming works on my machine".
            "X-Accel-Buffering": "no",
            "x-request-id": ctx.request_id,
        },
    )


async def _event_stream(ctx: _Ctx, session: Any):
    breaker = circuit.get_breaker()
    started_emitting = False
    chosen: dict[str, str] | None = None
    used_model = ""
    parts: list[str] = []
    ttft_ms: int | None = None
    last_error: ProviderError | None = None
    degraded = False
    upstream_finish: str | None = None
    upstream_usage: ProviderUsage | None = None

    for idx, cand in enumerate(ctx.candidates):
        provider_name = cand.get("provider", ctx.settings.default_provider)
        model_name = cand.get("model", ctx.payload.model)
        allowed, reason = await breaker.allow(provider_name)
        if not allowed:
            ctx.route_path.append(
                {"attempt": idx + 1, "provider": provider_name, "skipped": reason, "ok": False}
            )
            continue

        attempt: dict[str, Any] = {
            "attempt": idx + 1,
            "provider": provider_name,
            "model": model_name,
            "ok": False,
        }
        t0 = time.perf_counter()
        try:
            provider = await _provider_for(provider_name, session, ctx.settings)
            # A fresh attempt starts with no verdict: carrying another candidate's
            # `finish_reason` into this one would mislabel the answer that actually ships.
            upstream_finish = None
            upstream_usage = None
            async for ev in provider.stream(
                model=model_name,
                messages=ctx.messages,
                temperature=ctx.payload.temperature,
                max_tokens=ctx.payload.max_tokens,
                top_p=ctx.payload.top_p,
                stop=ctx.payload.stop,
                user=ctx.payload.user,
                enable_thinking=ctx.payload.enable_thinking,
            ):
                if ev.type == "delta":
                    if not started_emitting:
                        started_emitting = True
                        ttft_ms = _ms(ctx.started)
                        chosen = {"provider": provider_name, "model": model_name}
                        used_model = model_name
                        degraded = idx > 0
                    parts.append(ev.text)
                    yield _chunk(model_name, ctx.request_id, ev.text)
                elif ev.type == "done":
                    # Metadata, not an ending: the attempt bookkeeping stays right below
                    # so the success path exists once. A provider that never reports an
                    # ending simply omits this event and we derive one.
                    if ev.finish_reason:
                        upstream_finish = ev.finish_reason
                    if ev.usage is not None:
                        upstream_usage = ev.usage
                else:
                    raise ProviderError(
                        ev.code or "upstream_stream_error", ev.message, retryable=ev.retryable
                    )
            if started_emitting:
                await breaker.record_success(provider_name)
                await incidents.note_success(provider_name)
                attempt["ok"] = True
                attempt["latency_ms"] = _ms(t0)
                ctx.route_path.append(attempt)
                break
        except ProviderError as exc:
            attempt["error"] = exc.code
            attempt["latency_ms"] = _ms(t0)
            ctx.route_path.append(attempt)
            last_error = exc
            if exc.retryable:
                await breaker.record_failure(provider_name)
                await incidents.note_failure(
                    provider_name, exc.code, threshold=ctx.settings.upstream_alert_threshold
                )
            metrics.inc("aigate_upstream_errors_total", provider=provider_name, code=exc.code)

            if started_emitting:
                # Tokens already went out; we cannot rewind a stream. Say so explicitly.
                yield _sse(
                    {
                        "error": {
                            "code": "gateway.stream_interrupted",
                            "message": exc.message[:200],
                            "request_id": ctx.request_id,
                        }
                    }
                )
                yield "data: [DONE]\n\n"
                await _persist_stream(
                    ctx,
                    chosen,
                    used_model,
                    parts,
                    ttft_ms,
                    status="error",
                    error_code="gateway.stream_interrupted",
                    degraded=degraded,
                )
                return
            if not exc.retryable:
                break
            continue

    if not started_emitting:
        code = last_error.code if last_error else "gateway.no_candidate"
        await _persist_stream(ctx, None, "", [], None, status="error", error_code=code, degraded=False)
        yield _sse(
            {"error": {"code": code, "message": "no provider available", "request_id": ctx.request_id}}
        )
        yield "data: [DONE]\n\n"
        return

    # Token counts: vendor numbers when it reported them, character estimates otherwise —
    # and the stream comment says which, so nobody mistakes an estimate for a measurement.
    input_tokens = estimate_messages_tokens(ctx.messages)
    output_tokens = estimate_tokens("".join(parts))
    usage_source = "estimated"
    if upstream_usage is not None and upstream_usage.total_tokens > 0:
        reported_in = upstream_usage.input_tokens > 0
        reported_out = upstream_usage.output_tokens > 0
        if reported_in:
            input_tokens = upstream_usage.input_tokens
        if reported_out:
            output_tokens = upstream_usage.output_tokens
        usage_source = "upstream" if reported_in and reported_out else "mixed"
    finish_reason = _finish_reason(upstream_finish, ctx.payload, output_tokens)
    price = await _price_for_bg(chosen["provider"], used_model)
    cost = _estimate_cost(price, input_tokens, output_tokens)

    yield _chunk(used_model, ctx.request_id, "", finish=finish_reason)
    if _wants_usage(ctx.payload):
        # OpenAI order: finish chunk -> usage-only chunk -> [DONE].
        yield _usage_chunk(used_model, ctx.request_id, input_tokens, output_tokens)
    # SSE comments are ignored by every client, which is exactly why metadata for a
    # stream lives here instead of in the body: the body is protocol, this is ours.
    yield (
        ": aigate "
        + json.dumps(
            {
                "request_id": ctx.request_id,
                "provider": chosen["provider"],
                "model_used": used_model,
                "degraded": degraded,
                "ttft_ms": ttft_ms,
                "finish_reason": finish_reason,
                "usage_source": usage_source,
                "cost_cny": float(cost) if cost is not None else 0.0,
                "priced": price is not None,
                "pii_masked": ctx.pii_hits,
            },
            ensure_ascii=False,
        )
        + "\n\n"
    )
    yield "data: [DONE]\n\n"

    await _persist_stream(
        ctx,
        chosen,
        used_model,
        parts,
        ttft_ms,
        status="fallback" if degraded else "ok",
        error_code=None,
        degraded=degraded,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


async def _price_for_bg(provider: str, model: str) -> ModelPrice | None:
    async with get_session_factory()() as session:
        return await _price_for(session, provider, model)


async def _persist_stream(
    ctx: _Ctx,
    chosen: dict[str, str] | None,
    model_used: str,
    parts: list[str],
    ttft_ms: int | None,
    *,
    status: str,
    error_code: str | None,
    degraded: bool,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Persistence for streams runs after the response — new session, never the request one.

    The caller passes the counts it already resolved for the client (vendor-reported when
    the vendor sent a usage chunk, estimates otherwise). Re-estimating here made the
    accounting row disagree with the usage chunk in the same response: against DashScope
    the vendor said 34/82 while `usage_records` said 16/141 (+41 tokens, 35%). The
    interrupted-stream path still passes nothing — a half-finished stream has no vendor
    numbers, and an estimate labelled as such is the honest answer there.
    """
    text_out = "".join(parts)
    if input_tokens is None:
        input_tokens = estimate_messages_tokens(ctx.messages)
    if output_tokens is None:
        output_tokens = estimate_tokens(text_out) if text_out else 0
    price = await _price_for_bg(chosen["provider"], model_used) if chosen else None
    cost = _estimate_cost(price, input_tokens, output_tokens) if chosen else None

    await _persist(
        ctx,
        provider=chosen["provider"] if chosen else ctx.settings.default_provider,
        model_used=model_used,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost=cost,
        latency_ms=_ms(ctx.started),
        status=status,
        error_code=error_code,
        degraded=degraded,
        ttft_ms=ttft_ms,
    )
    metrics.inc("aigate_requests_total", status=status)
    # Shadow runs are only meaningful when there is an answer to compare against.
    if status != "error" and chosen is not None:
        await _maybe_shadow(ctx, text_out)
    if degraded:
        metrics.inc("aigate_fallbacks_total", provider=chosen["provider"] if chosen else "unknown")


# --------------------------------------------------------------------------
# cache hit
# --------------------------------------------------------------------------


def cache_eligible_for_store(payload) -> bool:
    return get_cache().eligible(payload)


async def _serve_cache_hit(ctx: _Ctx, session: AsyncSession, hit, response: Response) -> dict[str, Any]:
    """A cache hit still writes a usage row — with cost 0.

    Skipping the row would make the bill look like traffic dropped; recording it at
    zero cost is what lets a later report say "these 3,000 calls cost nothing".
    """
    total_ms = _ms(ctx.started)
    ctx.route_path.append({"cache": hit.layer, "similarity": hit.similarity, "ok": True})

    price = await _price_for(session, hit.provider, hit.model_used)
    saved = _estimate_cost(price, hit.input_tokens, hit.output_tokens) or Decimal("0")

    await _persist(
        ctx,
        provider=hit.provider,
        model_used=hit.model_used,
        input_tokens=hit.input_tokens,
        output_tokens=hit.output_tokens,
        cost=Decimal("0"),
        latency_ms=total_ms,
        status="cached",
        error_code=None,
        degraded=False,
        cache_hit=hit.layer,
    )
    metrics.inc("aigate_requests_total", status="cached")
    response.headers["x-request-id"] = ctx.request_id

    return {
        "id": f"chatcmpl-{ctx.request_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": hit.model_used,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": hit.content},
                # Truthful because only `stop` answers are stored (see `_complete`):
                # a truncated answer never enters the cache in the first place.
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": hit.input_tokens,
            "completion_tokens": hit.output_tokens,
            "total_tokens": hit.input_tokens + hit.output_tokens,
        },
        "x_aigate": {
            "request_id": ctx.request_id,
            "team": ctx.principal.team.slug,
            "project": ctx.principal.project.slug,
            "provider": hit.provider,
            "model_requested": ctx.payload.model,
            "model_used": hit.model_used,
            "degraded": False,
            "cache_hit": hit.layer,
            "cache_similarity": hit.similarity,
            "pii_masked": ctx.pii_hits,
            "cost_cny": 0.0,
            "saved_cny": float(saved),
            "priced": price is not None,
            "latency_ms": {"gateway_total": total_ms, **ctx.timings},
        },
    }


# --------------------------------------------------------------------------
# shared persistence
# --------------------------------------------------------------------------


async def _persist(
    ctx: _Ctx,
    *,
    provider: str,
    model_used: str,
    input_tokens: int,
    output_tokens: int,
    cost: Decimal | None,
    latency_ms: int,
    status: str,
    error_code: str | None,
    degraded: bool,
    http_status: int = 200,
    ttft_ms: int | None = None,
    cache_hit: str | None = None,
) -> None:
    """Hand the accounting to the batched writer.

    Nothing here touches the session: the request must not pay for a COMMIT. The writer
    owns the transaction, the retry, and the last_used_at throttle.
    """
    # Trace stores the *masked* prompt: the masked version is what actually left the
    # building, so it is the only version that can honestly be kept by default.
    masked_text = _content_to_text(ctx.messages)
    await meter.submit(
        UsageEvent(
            request_id=ctx.request_id,
            team_id=ctx.principal.team.id,
            project_id=ctx.principal.project.id,
            api_key_id=ctx.principal.api_key.id,
            provider=provider,
            model_requested=ctx.payload.model,
            model_used=model_used,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_cny=cost if cost is not None else Decimal("0"),
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            status=status,
            error_code=error_code,
            cache_hit=cache_hit,
            degraded=degraded,
            prompt_sha256=hashlib.sha256(masked_text.encode("utf-8")).hexdigest(),
            prompt_preview=mask_pii(masked_text)[:500],
            prompt_version=ctx.prompt_version,
            # Raw text only when explicitly enabled: the masked version is what left the
            # building, and it is the only one that is safe to keep by default.
            prompt_raw=(
                _content_to_text(ctx.payload.messages)[: ctx.settings.trace_prompt_max_chars]
                if ctx.settings.trace_prompt_mode == "raw"
                else None
            ),
            pii_masked=ctx.pii_hits,
            http_status=http_status,
            stage_timings=dict(ctx.timings),
            route_path=list(ctx.route_path),
        )
    )
