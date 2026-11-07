"""Database engine/session management.

Engine is created lazily so tests can point it at a temp SQLite file
without re-importing modules.  Same code path serves PostgreSQL.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from aigate.config import get_settings
from aigate.models import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# 最近一次预热的结果。给 /health 读，不参与任何业务判断。
_pool_warm: dict[str, Any] | None = None


def set_engine(url: str, *, echo: bool = False) -> None:
    global _engine, _session_factory, _pool_warm
    _pool_warm = None
    kwargs: dict = {"echo": echo, "future": True}
    if url.startswith("sqlite"):
        # timeout -> sqlite3 busytimeout: without it a single-writer collision under
        # concurrency surfaces as `database is locked` and an unhandled 500.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    if ":memory:" not in url:
        # 池等待上限走配置（AIGATE_DB_POOL_TIMEOUT，默认 1s）：SQLAlchemy 默认 30s，
        # 池满时请求会挂半分钟再变成一个 500。1s 内失败，503 + Retry-After 交给调用方
        # 决策（见 main.py），这才是网关该有的准入表现。
        # 内存库用 StaticPool，不认 pool_timeout，所以只对文件库/网络库设置。
        kwargs["pool_timeout"] = get_settings().db_pool_timeout
    _engine = create_async_engine(url, **kwargs)
    if url.startswith("sqlite"):
        # `journal_mode` is a property of the file, but `synchronous` is a property of the
        # *connection* and resets to FULL(2) on every new one. Setting it once in init_db()
        # therefore only fixed the first pooled connection; every connection opened later
        # under load ran FULL, i.e. an fsync per commit on the single SQLite writer — which
        # is exactly where the write lock is contended. PRAGMAs must be applied per connect.
        @event.listens_for(_engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


def get_engine() -> AsyncEngine:
    if _engine is None:
        set_engine(get_settings().database_url)
    assert _engine is not None
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session


def _pool_target(engine: AsyncEngine) -> int:
    size = getattr(engine.pool, "size", None)
    if callable(size):
        try:
            return max(1, int(size()))
        except Exception:  # noqa: BLE001 - 取不到就按 SQLAlchemy 默认值
            return 5
    return 5


def pool_warm_report() -> dict[str, Any]:
    """`/health` 里的 `pool` 字段。从没预热过就是 `state: cold`，不假装热。"""
    if _pool_warm is None:
        return {"state": "cold", "warm": None, "target": None, "ms": None, "error": None, "at": None}
    return dict(_pool_warm)


async def warm_pool(target: int | None = None) -> dict[str, Any]:
    """启动时把池里 `pool_size` 条连接真正建出来，并记录条数与耗时。

    为什么：冷启动时池是空的，首批并发请求各自付一次握手。本机 PG 实测一条连接的
    P50 是 ~119 ms（握手 92–115 ms），首屏那 3–5 个并发请求就各自吃一档，而且吃在
    用户第一次点开页面的那 200 ms 里。预热把这段成本挪到 lifespan，谁也不用等它。

    失败**不阻塞启动**：池没热起来意味着"更慢"，不是"不能服务"；具体原因进
    `/health` 的 `pool.error`，让值班看得到，而不是启动日志里一闪而过。
    """
    global _pool_warm
    started = time.perf_counter()
    engine = get_engine()
    n = target or _pool_target(engine)

    # StaticPool（`:memory:` 的 sqlite）只有一条共享连接，预热没有意义；
    # 报 0 而不是虚报 n 条。
    if isinstance(engine.pool, StaticPool):
        _pool_warm = {
            "state": "skipped",
            "warm": 0,
            "target": 0,
            "ms": 0.0,
            "error": None,
            "at": datetime.now(UTC).isoformat(),
        }
        return dict(_pool_warm)

    # 并发建连：串行 5 × 119 ms 会把启动时间拖到 0.6 s，并发只需要一档。
    # 全部拿到之后才归还——先还再取会复用同一条连接，池里永远只有 1 条。
    results = await asyncio.gather(
        *(engine.connect() for _ in range(n)),
        return_exceptions=True,
    )
    ok = [r for r in results if not isinstance(r, BaseException)]
    failed = [r for r in results if isinstance(r, BaseException)]
    for conn in ok:
        await conn.close()

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    if not failed:
        state = "warm"
    elif ok:
        state = "partial"
    else:
        state = "failed"
    _pool_warm = {
        "state": state,
        "warm": len(ok),
        "target": n,
        "ms": elapsed_ms,
        "error": str(failed[0])[:300] if failed else None,
        "at": datetime.now(UTC).isoformat(),
    }
    return dict(_pool_warm)


async def init_db() -> None:
    """Create tables if absent. Alembic replaces this once schema stabilises."""
    engine = get_engine()
    async with engine.begin() as conn:
        # 判方言要看**引擎**，不能看 settings：`set_engine()` 允许显式传 URL（测试、迁移、
        # 只读副本），此时 settings.database_url 还停在默认值，用 `is_sqlite` 会把
        # `PRAGMA journal_mode=WAL` 发给 PostgreSQL，启动即 syntax error。
        if engine.dialect.name == "sqlite":
            # WAL is a property of the *database file*, so setting it once here is enough.
            # It lets readers proceed while a writer holds the lock — the difference
            # between "p95 explodes at 20 concurrent users" and "it degrades politely".
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            await conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
        await conn.run_sync(Base.metadata.create_all)


async def dispose_engine() -> None:
    global _engine, _session_factory, _pool_warm
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
    _pool_warm = None
