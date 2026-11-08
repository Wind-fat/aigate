"""运行状态：一个页面上把「现在到底怎么样」说完。

值班打开的第一个页面不该是日志，而是这里。六个信号，每一个都对应"出事时先看它"：

    上游       哪个供应商被熔断器判死了（以及多久之后会自动探测）
    缓存       命中率是不是掉了（掉了可能是阈值被改过，或 prompt 版本变了）
    记账       队列积压与 spool 残留（积压 = 落库在排队；spool 残留 = 上次崩溃的尾巴）
    预算       有没有未确认的告警
    数据库     一次 SELECT 1 的耗时（SQLite 锁 / PG 连接池问题最先在这里显形）
    版本/环境  正在跑的到底是哪个构建、是不是 prod

刻意不做的：图表。这一页要的是"扫一眼就知道"，不是趋势。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aigate import metrics, provider_config
from aigate.api.deps import Actor, get_actor, require_admin, require_capability
from aigate.config import Settings, get_settings
from aigate.core import circuit
from aigate.db import get_session
from aigate.metering_queue import get_writer
from aigate.models import Alert, Project, Team

router = APIRouter(prefix="/admin/v1", tags=["control-plane"], dependencies=[Depends(require_admin)])


@router.get("/ops-health")
async def ops_health(
    request: Request,
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    require_capability(actor, "providers:read")

    # --- 上游：熔断器现在怎么想 ---------------------------------------------
    breaker = circuit.get_breaker()
    breaker_error: str | None = None
    try:
        breaker_state = await breaker.snapshot()
    except Exception as exc:  # noqa: BLE001 - Redis 挂了也要能打开这一页
        # 关键：快照拿不到时**每个 provider 的熔断态是未知**，不是"闭合"。
        # 以前这里把异常塞进 state、却让 providers[].circuit 回落成 {"open": False}，
        # 于是前端渲染"熔断：全部正常" —— 一个查询失败的页面给出了最乐观的结论。
        breaker_state = {"__error__": str(exc)[:200]}
        breaker_error = str(exc)[:200]

    providers = []
    resolved = await provider_config.load(session, settings, force=True)
    for name, cfg in sorted(resolved.items()):
        providers.append(
            {
                "name": name,
                "enabled": cfg.enabled,
                "configured": cfg.configured,
                "key_source": cfg.key_source,
                # None = 不知道（快照失败）。前端必须显示"未知"，不能显示"闭合"。
                "circuit": (
                    None
                    if breaker_error is not None
                    else breaker_state.get(name, {"open": False, "consecutive_failures": 0})
                ),
            }
        )

    # --- 记账队列 ------------------------------------------------------------
    # writer 未配置 ≠ 队列积压 0。以前只回 {"note": ...}，前端 `Number(undefined ?? 0)`
    # 把"记账根本没开"显示成"0 / 已写 0 行 / spool 残留 0"——最该被看见的故障最健康。
    writer = get_writer()
    if writer is None:
        metering: dict[str, Any] = {"enabled": False, "note": "writer not configured"}
    else:
        metering = {"enabled": True, **writer.stats()}

    # --- 数据库 --------------------------------------------------------------
    t0 = time.perf_counter()
    try:
        await session.execute(text("SELECT 1"))
        db_ms: float | None = round((time.perf_counter() - t0) * 1000, 2)
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ms, db_ok = None, False

    # --- 预算告警 ------------------------------------------------------------
    # 未确认 **且** 未恢复：上游自己好了的告警不该继续占据「现在有事」这个词。
    open_alerts = (
        (
            await session.execute(
                select(Alert).where(Alert.acknowledged_at.is_(None), Alert.resolved_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    counts = {
        "teams": len((await session.execute(select(Team))).scalars().all()),
        "projects": len((await session.execute(select(Project))).scalars().all()),
    }

    # --- 计数器（本次进程启动以来） ------------------------------------------
    counters = metrics.snapshot()
    cache_hits = sum(v for k, v in counters.items() if k.startswith("aigate_cache_hits_total"))
    cache_served = sum(v for k, v in counters.items() if k.startswith("aigate_cache_served_total"))
    fallbacks = sum(v for k, v in counters.items() if k.startswith("aigate_fallbacks_total"))
    upstream_errors = sum(v for k, v in counters.items() if k.startswith("aigate_upstream_errors_total"))
    # 配置类失败（缺密钥 / 停用 / 路由指向不存在的 provider）单独一个数：它不是上游挂了，
    # 混进 upstream_errors 会让「上游错误率」在有人漏配密钥时虚高，值班追错方向。
    config_errors = sum(v for k, v in counters.items() if k.startswith("aigate_config_errors_total"))
    rate_limited = sum(v for k, v in counters.items() if k.startswith("aigate_rate_limited_total"))

    started = getattr(request.app.state, "started_at", None)
    return {
        "version": settings.version,
        "env": "prod" if settings.env == "prod" else "dev",
        "uptime_s": int(time.time() - started) if started else None,
        "db": {
            "ok": db_ok,
            "latency_ms": db_ms,
            # 只回后端类型，不回完整 URL：里面带密码
            "backend": settings.database_url.split("://")[0],
        },
        "breaker": {
            "backend": breaker.name,
            "threshold": breaker.threshold,
            "cooldown_s": breaker.cooldown_s,
            "state": breaker_state,
            # 显式三态信号：None = 快照成功；字符串 = 快照失败的原因。
            # 前端据此把整卡渲染成"未知"并给重试，而不是"全部正常"。
            "error": breaker_error,
        },
        "providers": providers,
        "metering": metering,
        "counters": {
            "cache_hits": cache_hits,
            "cache_served": cache_served,
            "fallbacks": fallbacks,
            "upstream_errors": upstream_errors,
            "config_errors": config_errors,
            "rate_limited": rate_limited,
        },
        "alerts": {
            "open": len(open_alerts),
            "items": [
                {
                    "level": a.level,
                    "scope": f"{a.scope_type} {a.scope_slug}",
                    "metric": a.metric,
                    "message": a.message,
                }
                for a in open_alerts[:5]
            ],
        },
        "counts": counts,
    }