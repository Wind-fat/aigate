"""FastAPI application factory.

Middleware order matters and is deliberate:
  request_id -> timing/logging -> CORS -> routes
Auth is a route dependency (not middleware) so OpenAPI documents it per-endpoint.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from aigate import metrics
from aigate.api import (
    admin,
    admin_health,
    admin_ops,
    admin_prompts,
    admin_providers,
    console_auth,
    health,
    v1_chat,
)
from aigate.audit import resolve_action, resolve_actor, should_audit, write_audit
from aigate.config import assert_production_ready, get_settings
from aigate.core.cache import build_cache, get_cache, reset_cache
from aigate.db import dispose_engine, get_session_factory, init_db
from aigate.metering_queue import configure_writer

log = logging.getLogger("aigate")


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    import time as _time

    app.state.started_at = _time.time()
    settings = get_settings()
    configure_logging(settings.log_level)
    assert_production_ready(settings)
    if settings.auto_create_schema:
        await init_db()
        log.info("database ready (%s)", settings.database_url.split("://")[0])
        # Register the built-in vendors once. Idempotent: an edited row is never overwritten.
        from aigate.provider_config import ensure_seeds

        async with get_session_factory()() as session:
            added = await ensure_seeds(session)
        if added:
            log.info("provider configs seeded: %s", added)
    writer = configure_writer(
        get_session_factory(),
        batch_size=settings.metering_batch_size,
        flush_interval_s=settings.metering_flush_interval_ms / 1000,
        max_queue=settings.metering_queue_max,
        spool_path=settings.metering_spool_path or None,
    )
    await writer.start()

    # 熔断器与限流器用同一个后端开关：只要是多副本部署，两者都必须共享状态，
    # 否则"供应商挂了"这件事每个副本会各自发现一次（有效阈值 = N × threshold）。
    from aigate.core import circuit

    circuit.configure_breaker(
        threshold=settings.circuit_threshold,
        cooldown_s=settings.circuit_cooldown_s,
        backend=settings.circuit_backend or settings.rate_limiter,
        redis_url=settings.redis_url,
    )
    log.info("circuit breaker backend: %s", circuit.get_breaker().name)

    get_cache(
        build_cache(
            enabled=settings.semantic_cache_enabled,
            threshold=settings.cache_threshold,
            ttl_s=settings.cache_ttl_s,
            max_scan=settings.cache_max_scan,
        )
    )
    log.info(
        "%s v%s started in %s mode (cache=%s, threshold=%.2f)",
        settings.app_name,
        settings.version,
        settings.env,
        settings.semantic_cache_enabled,
        get_cache().threshold,
    )
    yield
    # drain the billing queue before the engine goes away
    await writer.stop()
    reset_cache()
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="AIGate",
        version=settings.version,
        description="Multi-tenant LLM gateway: routing, quota, metering, tracing, eval gate.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-request-id"],
    )

    @app.middleware("http")
    async def audit_writes(request: Request, call_next):
        response = await call_next(request)
        if should_audit(request):
            resolved = getattr(request.state, "actor", None)
            if resolved is not None:
                actor, role = resolved.name, resolved.role
            else:
                # no authenticated dependency ran (401/422): record it as anonymous
                actor, role = resolve_actor(request, settings)
            await write_audit(
                get_session_factory(),
                actor=actor,
                role=role,
                action=resolve_action(request.method, request.url.path),
                request=request,
                status_code=response.status_code,
            )
        return response

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:20]}"
        request.state.request_id = rid
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - we re-raise, just want the log line
            log.exception("unhandled error rid=%s path=%s", rid, request.url.path)
            raise
        duration_ms = int((time.perf_counter() - started) * 1000)
        response.headers["x-request-id"] = rid
        log.info(
            "%s %s -> %s %dms rid=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            rid,
        )
        metrics.inc("aigate_http_requests_total", method=request.method, status=str(response.status_code))
        metrics.observe_ms("aigate_http_duration", duration_ms, path=request.url.path)
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            error = {"code": detail["code"], "message": detail.get("message", "")}
            # Extras are part of the contract: a validation error that says *which*
            # candidate was wrong lets the console highlight the field instead of
            # printing "bad request" and leaving the operator to guess.
            for key, value in detail.items():
                if key not in ("code", "message"):
                    error[key] = value
        else:
            error = {"code": "gateway.error", "message": str(detail)}
        return JSONResponse(
            status_code=exc.status_code, content={"error": error}, headers=dict(exc.headers or {})
        )

    app.include_router(health.router)
    app.include_router(v1_chat.router)
    app.include_router(admin.router)
    app.include_router(admin_ops.router)
    app.include_router(admin_providers.router)
    app.include_router(admin_prompts.router)
    app.include_router(admin_health.router)
    app.include_router(console_auth.router)
    return app


app = create_app()
