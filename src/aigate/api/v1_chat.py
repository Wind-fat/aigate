"""Data plane: POST /v1/chat/completions — the one endpoint that carries the product.

Request path (Stage 1):
  auth (dependency) -> model scope -> route resolve -> provider call
  -> cost calc -> usage record + trace -> OpenAI-shaped response + x_aigate
"""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aigate import metrics
from aigate.api.deps import Principal, get_principal
from aigate.config import Settings, get_settings
from aigate.db import get_session
from aigate.metering import compute_cost, mask_pii
from aigate.models import ModelPrice, RequestTrace, RoutePolicy, UsageRecord
from aigate.providers import ProviderError, get_provider
from aigate.schemas import ChatCompletionRequest

router = APIRouter(prefix="/v1", tags=["data-plane"])

DEFAULT_POLICY = "__default__"


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _content_to_text(messages: list[dict]) -> str:
    """Works on both pydantic models (via .content) and dicts (via ["content"])."""
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
    stmt = select(RoutePolicy).where(
        RoutePolicy.match_model == virtual_model, RoutePolicy.enabled.is_(True)
    )
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


@router.get("/models")
async def list_models(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Only models this key may actually call — the list is a permission surface."""
    allowed = principal.allowed_models
    stmt = select(RoutePolicy).where(RoutePolicy.enabled.is_(True))
    policies = (await session.execute(stmt)).scalars().all()
    data = []
    for p in policies:
        if p.match_model == DEFAULT_POLICY:
            continue
        if allowed and p.match_model not in allowed:
            continue
        data.append(
            {
                "id": p.match_model,
                "object": "model",
                "owned_by": "aigate",
                "candidates": p.candidates,
                "strategy": p.strategy,
            }
        )
    return {"object": "list", "data": data}


@router.post("/chat/completions")
async def chat_completions(
    payload: ChatCompletionRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if payload.stream:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={"code": "gateway.stream_not_implemented", "message": "streaming lands in Stage 2"},
        )

    started = time.perf_counter()
    request_id = getattr(request.state, "request_id", f"req_{uuid.uuid4().hex[:20]}")
    timings: dict[str, int] = {}
    route_path: list[dict[str, Any]] = []

    _check_model_scope(payload.model, principal)
    timings["scope_ms"] = _ms(started)

    messages = [m.model_dump(exclude_none=True) for m in payload.messages]
    candidates = await _resolve_candidates(session, payload.model, settings)

    result = None
    chosen: dict[str, str] | None = None
    last_error: ProviderError | None = None
    degraded = False

    for idx, cand in enumerate(candidates):
        provider_name = cand.get("provider", settings.default_provider)
        model_name = cand.get("model", payload.model)
        attempt: dict[str, Any] = {
            "attempt": idx + 1,
            "provider": provider_name,
            "model": model_name,
            "ok": False,
        }
        t0 = time.perf_counter()
        try:
            provider = get_provider(provider_name, settings)
            result = await provider.chat(
                model=model_name,
                messages=messages,
                temperature=payload.temperature,
                max_tokens=payload.max_tokens,
            )
            attempt["ok"] = True
            attempt["latency_ms"] = _ms(t0)
            route_path.append(attempt)
            chosen = {"provider": provider_name, "model": model_name}
            degraded = idx > 0
            timings["upstream_ms"] = _ms(t0)
            break
        except ProviderError as exc:
            attempt["error"] = exc.code
            attempt["latency_ms"] = _ms(t0)
            route_path.append(attempt)
            last_error = exc
            metrics.inc("aigate_upstream_errors_total", provider=provider_name, code=exc.code)
            if not exc.retryable:
                break
            continue

    total_ms = _ms(started)

    if result is None or chosen is None:
        code = last_error.code if last_error else "gateway.no_candidate"
        await _persist(
            session,
            request_id=request_id,
            principal=principal,
            provider=route_path[-1]["provider"] if route_path else settings.default_provider,
            model_requested=payload.model,
            model_used="",
            input_tokens=0,
            output_tokens=0,
            cost=None,
            latency_ms=total_ms,
            status="error",
            error_code=code,
            degraded=degraded,
            messages=messages,
            timings=timings,
            route_path=route_path,
            http_status=502,
        )
        metrics.inc("aigate_requests_total", status="error", code=code)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": code,
                "message": (last_error.message if last_error else "no provider available")[:300],
                "request_id": request_id,
            },
        )

    price = await _price_for(session, chosen["provider"], result.model) or await _price_for(
        session, chosen["provider"], chosen["model"]
    )
    cost = (
        compute_cost(
            result.usage.input_tokens,
            result.usage.output_tokens,
            price.input_price_per_1k,
            price.output_price_per_1k,
        )
        if price
        else None
    )

    await _persist(
        session,
        request_id=request_id,
        principal=principal,
        provider=chosen["provider"],
        model_requested=payload.model,
        model_used=result.model,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        cost=cost,
        latency_ms=total_ms,
        status="fallback" if degraded else "ok",
        error_code=None,
        degraded=degraded,
        messages=messages,
        timings=timings,
        route_path=route_path,
        http_status=200,
    )

    metrics.inc("aigate_requests_total", status="ok", provider=chosen["provider"])
    if degraded:
        metrics.inc("aigate_fallbacks_total", provider=chosen["provider"])

    response.headers["x-request-id"] = request_id

    return {
        "id": f"chatcmpl-{request_id}",
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
            "request_id": request_id,
            "team": principal.team.slug,
            "project": principal.project.slug,
            "provider": chosen["provider"],
            "model_requested": payload.model,
            "model_used": result.model,
            "degraded": degraded,
            "cache_hit": None,
            "cost_cny": float(cost) if cost is not None else 0.0,
            "priced": price is not None,
            "latency_ms": {"gateway_total": total_ms, **timings},
        },
    }


async def _persist(
    session: AsyncSession,
    *,
    request_id: str,
    principal: Principal,
    provider: str,
    model_requested: str,
    model_used: str,
    input_tokens: int,
    output_tokens: int,
    cost: Any,
    latency_ms: int,
    status: str,
    error_code: str | None,
    degraded: bool,
    messages: list[dict],
    timings: dict[str, int],
    route_path: list[dict],
    http_status: int,
) -> None:
    from decimal import Decimal

    session.add(
        UsageRecord(
            request_id=request_id,
            team_id=principal.team.id,
            project_id=principal.project.id,
            api_key_id=principal.api_key.id,
            provider=provider,
            model_requested=model_requested,
            model_used=model_used,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_cny=cost if cost is not None else Decimal("0"),
            latency_ms=latency_ms,
            status=status,
            error_code=error_code,
            degraded=degraded,
        )
    )

    prompt_text = _content_to_text(messages)
    session.add(
        RequestTrace(
            request_id=request_id,
            project_id=principal.project.id,
            model_requested=model_requested,
            prompt_sha256=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
            prompt_preview=mask_pii(prompt_text)[:500],
            stage_timings=timings,
            route_path=route_path,
            http_status=http_status,
        )
    )

    from aigate.models import utcnow

    principal.api_key.last_used_at = utcnow()
    await session.commit()