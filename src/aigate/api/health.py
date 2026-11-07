"""Health, readiness and metrics endpoints (no auth — they are for the platform)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aigate import metrics
from aigate.config import Settings, get_settings
from aigate.db import get_session, pool_warm_report

router = APIRouter(tags=["ops"])


@router.get("/health")
async def health(settings: Settings = Depends(get_settings)) -> dict:
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.version,
        "env": settings.env,
        # 连接池预热结果：warm = 实际建好的条数，ms = 建好它们花了多久。
        # 冷启动没预热过就是 state=cold / warm=null —— 不把"还没做"写成 0。
        "pool": pool_warm_report(),
    }


@router.get("/ready")
async def ready(session: AsyncSession = Depends(get_session)) -> dict:
    await session.execute(text("SELECT 1"))
    return {"status": "ready", "database": "up"}


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics_endpoint() -> str:
    return metrics.render()
