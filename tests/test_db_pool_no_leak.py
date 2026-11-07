"""回归：连接被取消（超时/客户端断开）之后，池的 `checkedout()` 必须回到 0。

接手时留下的空档：`warm_pool()` 的超时路径会 `task.cancel()` 掉还在握手的连接任务，
但只有"实测过一次"（sqlite + 手动 sleep 版 connect），没有断言。取消发生在
`engine.connect()` 中间时，SQLAlchemy 里的那半条连接如果没被回收，症状是**运行几周后
池子莫名其妙永远满**——每次重启都正常，查不出来。这里把它钉住：

1. `test_warm_timeout_cancels_and_leaves_pool_empty` —— 预热超时（connect 被拖慢），
   取消之后 `checkedout()==0`，且池子还能正常取/还连接（没有被取消搞成死池）。
2. `test_cancelled_connect_in_flight_returns_slot` —— 真的在握手过程中取消任务。PG 上
   握手 ~100 ms，所以 20 ms 后取消一定打在"连接已从池里领出、还没交回"的窗口里；
   SQLite 的 connect 是 1 ms 级，取消窗口几乎抓不到，**这就是两边的差异**：
   SQLite 上这条用例只能证明"没有留下占位"，不能证明"中途取消被回收"，
   中途取消的证明由 PG 分支（本机便携 PG）负责。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from aigate import db as db_mod
from aigate.db import get_engine, warm_pool


class _Stub:
    """warm_pool 只读 db_pool_timeout；其余字段不起作用。"""

    db_pool_timeout = 0.05


async def test_warm_timeout_cancels_and_leaves_pool_empty(client, monkeypatch):
    """预热超时后：state=timeout、池里 0 条被占、池子仍然可用。"""
    engine = get_engine()
    pool = engine.pool
    assert pool.checkedout() == 0, "用例前提：开始时池是空的"

    real_connect = AsyncEngine.connect

    async def slow_connect(self):  # noqa: ANN001 - 只替换这一次
        await asyncio.sleep(5)  # 握手永不完成 → 必然走到 warm_pool 的取消分支
        return await real_connect(self)

    monkeypatch.setattr(AsyncEngine, "connect", slow_connect)
    # warm_timeout_s() = max(0.2, 2 × 0.05) = 0.2s，远小于 sleep(5)
    monkeypatch.setattr(db_mod, "get_settings", lambda: _Stub())

    report = await warm_pool()

    assert report["state"] == "timeout", report
    assert report["warm"] == 0
    # 关键断言：取消之后没有任何一条连接被留在 checked out 状态。
    assert pool.checkedout() == 0, f"预热超时取消后池里还占着 {pool.checkedout()} 条：连接泄漏"

    # 池子没有被取消搞坏：真实连接照样能取到并归还（若上面漏了一条，这里会多占 1 条）。
    monkeypatch.setattr(AsyncEngine, "connect", real_connect)
    conn = await engine.connect()
    assert pool.checkedout() == 1
    await conn.close()
    assert pool.checkedout() == 0


async def test_cancelled_connect_in_flight_returns_slot(client):
    """握手中途取消：PG 上真的打在 in-flight 窗口，取消后占位归零、池子可复用。"""
    engine = get_engine()
    pool = engine.pool
    n = min(3, pool.size())
    assert pool.checkedout() == 0

    async def _open():
        return await engine.connect()

    tasks = [asyncio.create_task(_open()) for _ in range(n)]
    done, pending = await asyncio.wait(tasks, timeout=0.02)

    for task in pending:
        task.cancel()
    if pending:
        results = await asyncio.gather(*pending, return_exceptions=True)
        assert all(isinstance(r, asyncio.CancelledError) for r in results), results

    if engine.dialect.name == "postgresql":
        # PG 握手 ~100 ms，20 ms 时必然还在握手中；pending 为空说明这条用例没测到取消路径。
        assert pending, "PG 上 20ms 内连接就建好了：这条用例没打在取消窗口里，断言无效"

    # 已建好的那几条照常归还；被取消的那几条必须由 SQLAlchemy 回收。
    for task in done:
        if task.exception() is None:
            await task.result().close()
    await asyncio.sleep(0.1)  # 让取消的收尾跑完（asyncpg 的 connection_lost 回调）

    assert pool.checkedout() == 0, (
        f"中途取消后有 {pool.checkedout()} 条连接没回到池里（dialect={engine.dialect.name}）"
    )

    # 池子还能用：容量没被吃掉。
    conn = await engine.connect()
    assert pool.checkedout() == 1
    await conn.close()
    assert pool.checkedout() == 0


@pytest.mark.parametrize("_run", [1])
async def test_slot_returns_after_holder_is_closed(client, _run):
    """对照组：正常归还路径必须是 0，否则上面两条"回到 0"就没有参照物。"""
    engine = get_engine()
    pool = engine.pool
    conn = await engine.connect()
    assert pool.checkedout() == 1
    await conn.close()
    assert pool.checkedout() == 0
