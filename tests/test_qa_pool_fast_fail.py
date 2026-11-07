"""R5 回归：池满必须 1s 内快速失败成 503 + Retry-After，而不是挂 30s 再 500。

缺陷（修复前）：`db.py` 用 SQLAlchemy 默认 pool_timeout=30s，池压满时请求挂半分钟，
最后抛 `sqlalchemy.exc.TimeoutError` → 无人接管 → 500 + 一条 SQLAlchemy 栈。
调用方既不知道能不能重试，也不知道该等多久；运维也没有计数器。

修复后（db.py 走 AIGATE_DB_POOL_TIMEOUT，main.py 注册 handler）：
  - pool_timeout 由配置给（默认 1.0s）；
  - 撞顶 → 503 + `Retry-After` + `aigate_pool_timeout_total` 计数器，
    响应体 `error.code == "gateway.pool_timeout"`。

本文件两条用例都不降低并发/不缩短等待：
  test_pool_exhausted_fails_fast      —— 用 `pool.size() + pool.overflow()` 条真实连接把池占满，
                                        再发一次请求，钉住 503/耗时/头/计数器四件事。
  test_sixteen_concurrent_never_hangs —— N=16 并发（容量的 15 + 1），每个请求只许是
                                        200 或快速 503，任何一个挂到 5s 以上即失败。
"""

from __future__ import annotations

import asyncio
import time
import traceback

from sqlalchemy import text

from aigate import metrics
from aigate.db import get_engine, get_session_factory

PAYLOAD = {
    "model": "chat-default",
    "messages": [{"role": "user", "content": "pool probe"}],
    "temperature": 0.7,  # 不进语义缓存，每个请求都走 provider + _persist
}

POOL_TIMEOUT_COUNTER = "aigate_pool_timeout_total"
FAST_FAIL_BUDGET_S = 5.0


def _pool_timeouts() -> float:
    return metrics.snapshot().get(POOL_TIMEOUT_COUNTER, 0.0)


def _capacity() -> int:
    """池的总容量：pool_size + max_overflow（默认 5 + 10 = 15）。

    注意 `QueuePool.overflow()` 返回的是**当前**溢出连接数（不是上限），上限在
    `_max_overflow`（SQLAlchemy 2.0 没有公开 getter）。用 overflow() 会把容量算成
    5+0，占满 5 条就以为满了 —— 请求照样拿得到连接，用例就成了假绿。
    """
    pool = get_engine().pool
    max_overflow = getattr(pool, "_max_overflow", 10)
    return pool.size() + (max_overflow if max_overflow >= 0 else 0)


async def test_pool_exhausted_fails_fast(client, auth_headers):
    """池被占满后的一次请求：503 + Retry-After + 计数器 +1 + 耗时 < 5s。"""
    factory = get_session_factory()
    capacity = _capacity()
    holders = []
    try:
        # 占满：每条连接跑一次 SELECT 1 就停在事务里（不 commit 不放回池）。
        for _ in range(capacity):
            session = factory()
            await session.execute(text("SELECT 1"))
            holders.append(session)
        assert get_engine().pool.checkedout() >= capacity, "没把池占满，用例前提不成立"

        before = _pool_timeouts()
        started = time.perf_counter()
        resp = await client.post("/v1/chat/completions", json=PAYLOAD, headers=auth_headers)
        elapsed = time.perf_counter() - started
    finally:
        for session in holders:
            await session.close()

    # ① 响应码 503（不是 500，也不是 30s 后才返回）
    assert resp.status_code == 503, f"期望 503 快速失败，实际 {resp.status_code}: {resp.text[:300]}"
    # ② 耗时 < 5s（pool_timeout 默认 1s；这条同时排除"挂 30s"的旧行为）
    assert elapsed < FAST_FAIL_BUDGET_S, f"池满后等了 {elapsed:.2f}s 才失败，超过 {FAST_FAIL_BUDGET_S}s"
    # ③ 有 Retry-After，且和闸值同源
    retry_after = resp.headers.get("retry-after")
    assert retry_after is not None, f"缺 Retry-After 头：{dict(resp.headers)}"
    assert int(retry_after) >= 1
    # 契约细节：调用方能从响应体认出这是准入失败，而不是内部错误
    assert resp.json()["error"]["code"] == "gateway.pool_timeout", resp.text[:300]
    # ④ 可观测计数器 +1
    assert _pool_timeouts() == before + 1, f"计数器没 +1：{before} -> {_pool_timeouts()}"


async def test_sixteen_concurrent_never_hangs(client, auth_headers):
    """N=16（容量 15 + 1）并发：每个请求要么 200，要么快速 503，没有一个可以挂 5s 以上。"""
    n = 16  # 不降并发：这是缺陷被发现的现场
    before = _pool_timeouts()

    async def timed_post():
        started = time.perf_counter()
        try:
            resp = await client.post("/v1/chat/completions", json=PAYLOAD, headers=auth_headers)
            return resp, time.perf_counter() - started, None
        except Exception as exc:  # noqa: BLE001 - 未捕获异常本身就是失败
            return None, time.perf_counter() - started, exc

    results = await asyncio.gather(*(timed_post() for _ in range(n)))

    raised = [
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        for _, _, exc in results
        if exc is not None
    ]
    assert not raised, f"{len(raised)} 个请求抛出未处理异常（handler 没接住）：\n{raised[0][:800]}"

    responses = [(r, elapsed) for r, elapsed, _ in results]
    slowest = max(elapsed for _, elapsed in responses)
    assert slowest < FAST_FAIL_BUDGET_S, f"最慢的请求用了 {slowest:.2f}s：又挂回去了"

    statuses = [r.status_code for r, _ in responses]
    assert set(statuses) <= {200, 503}, f"出现了 200/503 之外的状态码：{sorted(statuses)}"

    rejected = [r for r, _ in responses if r.status_code == 503]
    for r in rejected:
        assert r.headers.get("retry-after") is not None, "503 必须带 Retry-After"
        assert r.json()["error"]["code"] == "gateway.pool_timeout", r.text[:300]

    # 计数器必须与真实的 503 数一致：不许漏计，也不许凭空多计。
    assert _pool_timeouts() - before == len(rejected), (
        f"503 有 {len(rejected)} 个，计数器只涨了 {_pool_timeouts() - before}"
    )