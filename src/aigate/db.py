"""Database engine/session management.

Engine is created lazily so tests can point it at a temp SQLite file
without re-importing modules.  Same code path serves PostgreSQL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from aigate.config import get_settings
from aigate.models import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def set_engine(url: str, *, echo: bool = False) -> None:
    global _engine, _session_factory
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


async def init_db() -> None:
    """Create tables if absent. Alembic replaces this once schema stabilises."""
    engine = get_engine()
    async with engine.begin() as conn:
        if get_settings().is_sqlite:
            # WAL is a property of the *database file*, so setting it once here is enough.
            # It lets readers proceed while a writer holds the lock — the difference
            # between "p95 explodes at 20 concurrent users" and "it degrades politely".
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            await conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
        await conn.run_sync(Base.metadata.create_all)


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
