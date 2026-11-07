"""SQLite PRAGMA 必须逐连接生效，而不是只在 init_db() 那条连接上生效。

缺陷（修复前 `db.py:set_engine`）：`init_db()` 里执行了一次
`PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL`，注释写的是「WAL 是文件属性，
设一次就够」。前半句对，后半句不全：

- `journal_mode` 确实是文件属性，写进 db 文件头，之后每条连接都继承；
- `synchronous` 是**连接属性**，每条新连接都回到编译期默认值 FULL(2)。

于是池里第一条连接是 NORMAL，压测时新建的 14 条全是 FULL —— WAL 模式下每次 commit
都要 fsync，而 SQLite 只有一个写者，锁竞争正好发生在 commit 上。本用例开 3 条并发连接
把池撑大，钉住「每条物理连接都是 NORMAL」。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from aigate.db import get_engine


async def test_every_pooled_connection_runs_synchronous_normal(client):
    engine = get_engine()
    if engine.dialect.name != "sqlite":
        pytest.skip("PRAGMA 是 SQLite 的事；本用例在 AIGATE_TEST_DB_URL 指向 PG 时无意义")
    async with engine.connect() as c1, engine.connect() as c2, engine.connect() as c3:
        values = []
        for conn in (c1, c2, c3):
            values.append((await conn.execute(text("PRAGMA synchronous"))).scalar())
        wal = (await c1.execute(text("PRAGMA journal_mode"))).scalar()

    assert wal is not None and str(wal).lower() == "wal", f"journal_mode={wal!r}，期望 wal"
    assert values == [1, 1, 1], (
        f"池化连接的 synchronous={values}（1=NORMAL, 2=FULL）："
        "只在 init_db 的连接上设 PRAGMA，压测时新建的连接会退回 FULL，写入每次 commit 都 fsync"
    )