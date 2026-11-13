"""库结构落后于代码时，必须**当场喊出来**，而不是等某条冷门路径炸。

真实事故（2026-11-13）：拿一个月前的 `aigate.db` 起服务，`alerts` 表没有
`resolved_at`。`create_all` 只建表、从不补列，所以启动"成功"；然后上游一次抖动
触发告警写入 → `sqlite3.OperationalError: no such column: alerts.resolved_at`
→ 这个异常盖住了真正的上游 502 根因。值班看到的是一条和自己无关的 SQL 报错。

这组用例钉住三件事：
1. 缺列能被 `schema_drift()` 发现（表在、列不在，正是最容易漏的形状）；
2. 启动后 `/health` 变 `degraded` 并给出可执行的修法，而不是继续报 `ok`；
3. 结构正常时 `schema_drift()` 为空、`/health` 仍是 `ok`（不许把正常情况也标黄）。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from aigate.db import dispose_engine, get_engine, init_db, schema_drift, set_engine
from aigate.main import create_app


async def _drop_column(table: str, column: str) -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))


@pytest.fixture
async def fresh_db(tmp_path):
    db = tmp_path / "drift.db"
    set_engine(f"sqlite+aiosqlite:///{db.as_posix()}")
    await init_db()
    yield
    await dispose_engine()


async def test_dropped_column_is_reported(fresh_db) -> None:
    # 干净库：没有任何漂移
    assert await schema_drift() == []

    await _drop_column("alerts", "resolved_at")

    drift = await schema_drift()
    assert drift == [{"table": "alerts", "missing": ["resolved_at"]}]


async def test_health_turns_degraded_and_tells_you_the_command(fresh_db) -> None:
    await _drop_column("alerts", "resolved_at")
    # init_db 会把最近一次体检结果记进模块状态；/health 读的就是它
    await init_db()

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        body = (await client.get("/health")).json()

    assert body["status"] == "degraded", body
    assert body["schema_drift"] == [{"table": "alerts", "missing": ["resolved_at"]}]
    assert "alembic upgrade head" in body["schema_hint"]
    assert "alerts" in body["schema_hint"] and "resolved_at" in body["schema_hint"]


async def test_clean_db_stays_ok(fresh_db) -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        body = (await client.get("/health")).json()

    assert body["status"] == "ok", body
    assert body["schema_drift"] == []
    assert body["schema_hint"] == ""