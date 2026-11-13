"""Database engine/session management.

Engine is created lazily so tests can point it at a temp SQLite file
without re-importing modules.  Same code path serves PostgreSQL.
"""

from __future__ import annotations

import asyncio
import logging
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

from aigate import metrics
from aigate.config import get_settings
from aigate.models import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# 最近一次预热的结果。给 /health 读，不参与任何业务判断。
_pool_warm: dict[str, Any] | None = None
# 最近一次 init_db 的库结构体检结果（缺列清单）。空 = 库与模型一致。
_SCHEMA_DRIFT: list[dict[str, Any]] = []

# 容量公式里的两个常数：0.8 是给 DB 留的头寸（PG 的 autovacuum / 运维 psql 也要连），
# 1 条是给备份、迁移、巡检的预留——它不是 per-worker 的，全局只有一条。
CAPACITY_HEADROOM = 0.8
OPS_RESERVED_CONNECTIONS = 1

log = logging.getLogger("aigate.db")


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
        settings = get_settings()
        kwargs["pool_timeout"] = settings.db_pool_timeout
        # 容量也走配置（AIGATE_DB_POOL_SIZE / AIGATE_DB_POOL_OVERFLOW）。默认 5/10 就是
        # SQLAlchemy 的出厂值，所以不配等于没改。StaticPool（`:memory:`）不认这两个
        # 参数，所以和 pool_timeout 一样只在非内存库上设置。
        kwargs["pool_size"] = getattr(settings, "db_pool_size", 5)
        kwargs["max_overflow"] = getattr(settings, "db_pool_overflow", 10)
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


def _dialect_name() -> str:
    """当前引擎的方言。有引擎就以引擎为准：`set_engine()` 允许显式传 URL（测试、迁移），
    此时 settings.database_url 还停在默认值，看它会把 PG 判成 sqlite。"""
    if _engine is not None:
        return _engine.dialect.name
    url = str(getattr(get_settings(), "database_url", "") or "")
    if not url:
        return "unknown"
    scheme = url.split("://", 1)[0].split("+", 1)[0]
    return {"sqlite": "sqlite", "postgresql": "postgresql"}.get(scheme, scheme or "other")


def capacity_report() -> dict[str, Any]:
    """连接池容量校验：`workers × (pool_size + max_overflow) + 预留 ≤ max_conn × 0.8`。

    为什么需要它：`pool_size`/`max_overflow` 现在可以从环境变量配，而**配错是静默的**——
    把 pool_size 调到 60、workers 开 4 个，PG 的 max_connections 立刻被打满，第一批
    受害者是 autovacuum 和运维自己的 psql，症状是"数据库时好时坏"。这里在启动时把
    账算一遍：**只判定，不改行为**，不满足只打 WARNING 并把这个 dict 放进
    `/health` 的 `db_capacity`，让配置错误在第一次看 /health 时就暴露。

    公式里 +1 的是全局运维预留（备份、迁移、巡检各要一条连接），不是 per-worker。
    SQLite 没有 `max_connections` 这个概念（限制在单写者锁上），所以判定仍然给，
    但 `enforced=false`，含义是"这组数在相对意义上也不健康"，不是 PG 那种硬上限。
    """
    settings = get_settings()
    pool_size = int(getattr(settings, "db_pool_size", 5))
    overflow = int(getattr(settings, "db_pool_overflow", 10))
    workers = max(1, int(getattr(settings, "db_workers", 1)))
    max_conn = int(getattr(settings, "db_max_connections", 100))

    per_worker = pool_size + max(0, overflow)
    peak = workers * per_worker + OPS_RESERVED_CONNECTIONS
    budget = round(max_conn * CAPACITY_HEADROOM, 2)
    dialect = _dialect_name()

    if max_conn <= 0:
        verdict, fits, warning = "unknown", None, None
    elif peak <= budget:
        verdict, fits, warning = "ok", True, None
    else:
        verdict, fits = "over", False
        warning = (
            f"{workers} worker(s) x ({pool_size} + {overflow}) + {OPS_RESERVED_CONNECTIONS} "
            f"reserved = {peak} > max_connections {max_conn} x {CAPACITY_HEADROOM:g} = {budget:g}; "
            f"池满时请求会 503（gateway.pool_timeout），而不是排队"
        )

    return {
        "verdict": verdict,
        "fits": fits,
        "dialect": dialect,
        # enforced=false 表示这个上限不是数据库强制的（SQLite 只在写锁上串行），
        # 数字仍然有意义：它说的是"这组配置相对数据库体量已经过载"。
        "enforced": dialect == "postgresql",
        "formula": "workers x (pool_size + max_overflow) + reserved <= max_connections x 0.8",
        "pool_size": pool_size,
        "max_overflow": overflow,
        "per_worker_capacity": per_worker,
        "workers": workers,
        "reserved": OPS_RESERVED_CONNECTIONS,
        "peak_connections": peak,
        "max_connections": max_conn,
        "budget": budget,
        "warning": warning,
    }


def check_pool_capacity() -> dict[str, Any]:
    """启动时调一次：不满足公式就 WARNING。**永不抛异常**——配置不合理是运维问题，
    不该让进程起不来（和预热失败同一个取向：可用性优先，证据写进 /health）。"""
    report = capacity_report()
    if report["verdict"] == "over":
        log.warning(
            "db pool capacity over budget (%s): %s -- 提高 AIGATE_DB_MAX_CONNECTIONS 或"
            " 下调 AIGATE_DB_POOL_SIZE / AIGATE_DB_POOL_OVERFLOW / AIGATE_DB_WORKERS",
            report["dialect"],
            report["warning"],
        )
    elif report["verdict"] == "ok":
        log.info(
            "db pool capacity ok: peak %s <= budget %s (pool_size=%s overflow=%s workers=%s max_conn=%s)",
            report["peak_connections"],
            report["budget"],
            report["pool_size"],
            report["max_overflow"],
            report["workers"],
            report["max_connections"],
        )
    return report


def warm_timeout_s() -> float:
    """预热的硬上限：`2 × AIGATE_DB_POOL_TIMEOUT`（默认 1s → 2s）。

    为什么要有上限：`pool_timeout` 已经定义了"取一条连接最多等多久"，预热只是把这个
    动作并发做 pool_size 次，两倍闸值足够；没有它，DB 在 TCP 层被丢包（防火墙 DROP、
    主机半死）时 asyncio 的 connect 会一直挂着，一个"优化"就变成了启动可用性风险。
    """
    return max(0.2, 2 * get_settings().db_pool_timeout)


async def warm_pool(target: int | None = None) -> dict[str, Any]:
    """启动时把池里 `pool_size` 条连接真正建出来，并记录条数与耗时。

    为什么：冷启动时池是空的，首批并发请求各自付一次握手。本机 PG 实测一条连接的
    P50 是 ~119 ms（握手 92–115 ms），首屏那 3–5 个并发请求就各自吃一档，而且吃在
    用户第一次点开页面的那 200 ms 里。预热把这段成本挪到 lifespan，谁也不用等它。

    失败**不阻塞启动**：池没热起来意味着"更慢"，不是"不能服务"；具体原因进
    `/health` 的 `pool.error`，让值班看得到，而不是启动日志里一闪而过。
    同样地，**超时也不阻塞启动**：到 `warm_timeout_s()` 就收工，`state="timeout"`，
    已建好的连接照常归还，剩下的交给首批请求各自付握手。
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

    timeout_s = warm_timeout_s()
    # 并发建连：串行 5 × 119 ms 会把启动时间拖到 0.6 s，并发只需要一档。
    # 全部拿到之后才归还——先还再取会复用同一条连接，池里永远只有 1 条。
    # 这里不用 gather + wait_for：wait_for 超时后 gather 里的连接已经建好但还没被
    # 记到账上，close 就漏了。自己拿 done/pending 才能把"建好的"与"还在等的"分开。
    async def _open() -> Any:
        # `engine.connect()` 返回的是可等待的 AsyncConnection，不是 coroutine：
        # 直接丢给 create_task 会 TypeError，所以包一层。
        return await engine.connect()

    tasks = [asyncio.create_task(_open()) for _ in range(n)]
    done, pending = await asyncio.wait(tasks, timeout=timeout_s)
    timed_out = bool(pending)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    ok: list[Any] = []
    failed: list[BaseException] = []
    for task in done:
        exc = task.exception()
        if exc is not None:
            failed.append(exc)
        else:
            ok.append(task.result())
    for conn in ok:
        await conn.close()

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    if timed_out:
        state = "timeout"
        error: str | None = (
            f"warm-up exceeded {timeout_s:g}s "
            f"(2 x AIGATE_DB_POOL_TIMEOUT={get_settings().db_pool_timeout:g}s)"
        )
    elif not failed:
        state = "warm"
        error = None
    elif ok:
        state = "partial"
        error = str(failed[0])[:300]
    else:
        state = "failed"
        error = str(failed[0])[:300]

    # 指标口径：耗时与条数是"当前值"（gauge），累加没有意义；
    # 超时次数是计数（counter），值班要的是"这次重启有没有被拖过"。
    metrics.gauge("aigate_pool_warm_ms", elapsed_ms)
    metrics.gauge("aigate_pool_warm_connections", len(ok))
    if timed_out:
        metrics.inc("aigate_pool_warm_timeout_total")

    _pool_warm = {
        "state": state,
        "warm": len(ok),
        "target": n,
        "ms": elapsed_ms,
        "error": error,
        "at": datetime.now(UTC).isoformat(),
    }
    return dict(_pool_warm)


async def schema_drift() -> list[dict[str, Any]]:
    """库里缺了模型定义里的列 —— `create_all` 只建表，**从不补列**。

    真实事故：拿一个月前的 `aigate.db` 启动，`alerts` 表没有 `resolved_at`，
    `create_all` 一声不响地"成功"，然后在**上游失败的告警路径**里炸
    `no such column: alerts.resolved_at` —— 502 已经发出去了，真正的上游错误
    被这条异常盖住，值班只能看到一个和自己无关的 SQL 报错。

    所以启动后主动体检：缺列就 ERROR 日志 + `/health` 报 degraded，并给出可执行命令。
    不 raise：正在服务的实例不该因为一次部署顺序问题直接躺下；但也不能装作没事。
    """
    engine = get_engine()
    drift: list[dict[str, Any]] = []

    def _check(sync_conn: Any) -> list[dict[str, Any]]:
        from sqlalchemy import inspect as sa_inspect

        found: list[dict[str, Any]] = []
        inspector = sa_inspect(sync_conn)
        existing = set(inspector.get_table_names())
        for name, table in Base.metadata.tables.items():
            if name not in existing:
                continue  # create_all 刚建过；真缺表是另一个问题，不在这里喊
            have = {c["name"] for c in inspector.get_columns(name)}
            missing = [c.name for c in table.columns if c.name not in have]
            if missing:
                found.append({"table": name, "missing": missing})
        return found

    async with engine.connect() as conn:
        drift = await conn.run_sync(_check)
    return drift


async def init_db() -> None:
    """Create tables if absent, then **check** that它们不缺列。Alembic 负责补列。"""
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

    drift = await schema_drift()
    _SCHEMA_DRIFT[:] = drift
    if drift:
        detail = "；".join(f"{d['table']} 缺 {', '.join(d['missing'])}" for d in drift)
        log.error(
            "库结构落后于代码：%s。`create_all` 不会补列，运行期会在用到那些列的路径上炸 "
            "`no such column`（例如告警写入盖掉真正的上游错误）。修法："
            "`.venv/Scripts/python.exe -m alembic upgrade head`（或容器里跑同一命令）后重启。",
            detail,
        )


def schema_drift_report() -> list[dict[str, Any]]:
    """最近一次 init_db 体检结果；空列表 = 库与模型一致。"""
    return [dict(d) for d in _SCHEMA_DRIFT]


async def dispose_engine() -> None:
    global _engine, _session_factory, _pool_warm
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
    _pool_warm = None
