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
from aigate.api import health, v1_chat
from aigate.config import get_settings
from aigate.db import dispose_engine, init_db

log = logging.getLogger("aigate")


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.auto_create_schema:
        await init_db()
        log.info("database ready (%s)", settings.database_url.split("://")[0])
    log.info("%s v%s started in %s mode", settings.app_name, settings.version, settings.env)
    yield
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
            if "request_id" in detail:
                error["request_id"] = detail["request_id"]
        else:
            error = {"code": "gateway.error", "message": str(detail)}
        return JSONResponse(
            status_code=exc.status_code, content={"error": error}, headers=dict(exc.headers or {})
        )

    app.include_router(health.router)
    app.include_router(v1_chat.router)
    return app


app = create_app()
