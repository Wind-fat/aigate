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
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aigate import metering_queue as meter
from aigate import metrics
from aigate.api.deps import Principal, get_principal
from aigate.config import Settings, get_settings
from aigate.core import circuit, quota
from aigate.core.cache import get_cache
from aigate.db import get_session, get_session_factory
from aigate.guard import mask_messages
from aigate.metering import compute_cost, estimate_messages_tokens, estimate_tokens, mask_pii
from aigate.metering_queue import UsageEvent
from aigate.models import ModelPrice, RoutePolicy
from aigate.providers import ProviderError, get_provider
from aigate.schemas import ChatCompletionRequest

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
            candidates = list(default_policy.candidates or []) + list(default_policy.fallback or [])
            if candidates:
                return candidates
        return [{"provider": settings.default_provider, "model": virtual_model}]

    return list(policy.candidates or []) + list(policy.fallback or [])


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
    timings: dict[str, int] = {}
    route_path: list[dict[str, Any]] = []

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

    # --- 1. rate limit (per project) -------------------------------------
    t = time.perf_counter()
    limiter = quota.get_limiter(settings.rate_limiter, settings.redis_url)
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

    # --- 2. budget (hard limit) ------------------------------------------
    t = time.perf_counter()
    within, spent, limit = await quota.check_budget(session, principal.project)
    timings["budget_ms"] = _ms(t)
    if not within:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "gateway.budget_exhausted",
                "message": f"monthly budget exhausted: spent ¥{spent} of ¥{limit}",
            },
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
        hit = await cache.lookup(session, principal.project.id, payload.model, payload.messages)
        timings["cache_ms"] = _ms(t)
        if hit is not None:
            metrics.inc("aigate_cache_served_total", layer=hit.layer)
            return await _serve_cache_hit(ctx, session, hit, response)

    ctx.candidates = await _resolve_candidates(session, payload.model, settings)

    if payload.stream:
        return _streaming_response(ctx)
    return await _complete(ctx, session, response)


# --------------------------------------------------------------------------
# context object (keeps the two paths from duplicating 12 arguments)
# --------------------------------------------------------------------------


class _Ctx:
    __slots__ = (
        "request_id",
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
        allowed, reason = breaker.allow(provider_name)
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
            provider = get_provider(provider_name, ctx.settings)
            result = await provider.chat(
                model=model_name,
                messages=ctx.messages,
                temperature=ctx.payload.temperature,
                max_tokens=ctx.payload.max_tokens,
            )
            breaker.record_success(provider_name)
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
                breaker.record_failure(provider_name)
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

    price = await _price_for(session, chosen["provider"], result.model) or await _price_for(
        session, chosen["provider"], chosen["model"]
    )
    cost = _estimate_cost(price, result.usage.input_tokens, result.usage.output_tokens)

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
    # caching it would silently pin lower-quality output for the whole TTL.
    if not degraded and cache_eligible_for_store(ctx.payload):
        await get_cache().store(
            session,
            ctx.principal.project.id,
            ctx.payload.model,
            ctx.payload.messages,
            provider=chosen["provider"],
            model_used=result.model,
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
                "finish_reason": "stop",
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


def _streaming_response(ctx: _Ctx) -> StreamingResponse:
    return StreamingResponse(
        _event_stream(ctx),
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


async def _event_stream(ctx: _Ctx):
    breaker = circuit.get_breaker()
    started_emitting = False
    chosen: dict[str, str] | None = None
    used_model = ""
    parts: list[str] = []
    ttft_ms: int | None = None
    last_error: ProviderError | None = None
    degraded = False

    for idx, cand in enumerate(ctx.candidates):
        provider_name = cand.get("provider", ctx.settings.default_provider)
        model_name = cand.get("model", ctx.payload.model)
        allowed, reason = breaker.allow(provider_name)
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
            provider = get_provider(provider_name, ctx.settings)
            async for ev in provider.stream(
                model=model_name,
                messages=ctx.messages,
                temperature=ctx.payload.temperature,
                max_tokens=ctx.payload.max_tokens,
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
                else:
                    raise ProviderError(
                        ev.code or "upstream_stream_error", ev.message, retryable=ev.retryable
                    )
            if started_emitting:
                breaker.record_success(provider_name)
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
                breaker.record_failure(provider_name)
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

    output_tokens = estimate_tokens("".join(parts))
    input_tokens = estimate_messages_tokens(ctx.messages)
    price = await _price_for_bg(chosen["provider"], used_model)
    cost = _estimate_cost(price, input_tokens, output_tokens)

    yield _chunk(used_model, ctx.request_id, "", finish="stop")
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
) -> None:
    """Persistence for streams runs after the response — new session, never the request one."""
    text_out = "".join(parts)
    input_tokens = estimate_messages_tokens(ctx.messages)
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
            pii_masked=ctx.pii_hits,
            http_status=http_status,
            stage_timings=dict(ctx.timings),
            route_path=list(ctx.route_path),
        )
    )
