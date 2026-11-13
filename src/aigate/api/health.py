"""Health, readiness and metrics endpoints (no auth — they are for the platform)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aigate import metrics
from aigate.config import Settings, get_settings
from aigate.db import capacity_report, get_session, pool_warm_report, schema_drift_report

router = APIRouter(tags=["ops"])


@router.get("/health")
async def health(settings: Settings = Depends(get_settings)) -> dict:
    drift = schema_drift_report()
    return {
        # status 只有两种：ok / degraded。缺列是 degraded —— 服务还在跑，但某些路径
        # 一旦被走到就会 500，而且报错指向 SQL 而不是真正的部署原因。
        "status": "degraded" if drift else "ok",
        "app": settings.app_name,
        "version": settings.version,
        "env": settings.env,
        # 连接池预热结果：warm = 实际建好的条数，ms = 建好它们花了多久。
        # 冷启动没预热过就是 state=cold / warm=null —— 不把"还没做"写成 0。
        "pool": pool_warm_report(),
        # 容量校验（workers × (pool_size + max_overflow) + 预留 ≤ max_conn × 0.8）。
        # 现场读配置，手工改完名额重启即生效；verdict=over 时 warning 里带算式。
        "db_capacity": capacity_report(),
        # 库结构体检：`create_all` 只建表、不补列。缺列会让运行期在相关路径上炸
        # `no such column`，而那个异常会盖住真正的根因（实测：告警写入炸掉、
        # 把上游 502 的原因藏了起来）。修法是 `alembic upgrade head`。
        "schema_drift": drift,
        "schema_hint": (
            "库结构落后于代码："
            + "；".join(f"{d['table']} 缺 {', '.join(d['missing'])}" for d in drift)
            + "。跑 `python -m alembic upgrade head` 后重启。"
        )
        if drift
        else "",
    }


@router.get("/ready")
async def ready(session: AsyncSession = Depends(get_session)) -> dict:
    await session.execute(text("SELECT 1"))
    return {"status": "ready", "database": "up"}


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics_endpoint() -> str:
    return metrics.render()
