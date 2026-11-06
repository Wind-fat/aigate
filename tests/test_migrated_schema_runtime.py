"""跑在「迁移建出来的库」上，而不是 create_all 建出来的库上。

为什么需要这个测试：`create_all` 只补缺失的表，不改已有的表。所以「模型里有字段」不等于
「迁移里有字段」，而这个差异只在**真机升级**时爆炸：

- 第一次：`console_users.project_scope` 只存在于模型里 → 真机升级后登录 500。
- 第二次：给 `RequestTrace` 加 `prompt_raw` 时，替换文本同时命中了 `SemanticCacheEntry`
  的同名字段 → 缓存查询引用一个不存在的列 → 所有带缓存查找的请求 500。

两个 bug 单测都没抓到，因为测试库是 create_all 建的（模型和库永远一致）。这个文件补上那个缺口：
先用 alembic 建库，再跑一个真实请求。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _migrated_db(tmp_path: Path) -> Path:
    db = tmp_path / "migrated.db"
    env = {
        **os.environ,
        "AIGATE_DATABASE_URL": f"sqlite+aiosqlite:///{db.as_posix()}",
        "AIGATE_RATE_LIMITER": "memory",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return db


@pytest.mark.asyncio
async def test_a_request_works_against_a_migrated_database(tmp_path, monkeypatch):
    """最关键的一条：迁移建出来的库，能真的跑通一次带缓存查找的调用。"""
    db = _migrated_db(tmp_path)

    # 让应用与 alembic 指向同一个库；这里的 monkeypatch 只影响本进程的 settings 缓存
    from aigate.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(
        settings, "database_url", f"sqlite+aiosqlite:///{db.as_posix()}", raising=False
    )

    from aigate.db import dispose_engine, get_session_factory, set_engine
    from aigate.models import ApiKey, ModelPrice, Project, RoutePolicy, Team
    from aigate.security import generate_api_key

    set_engine(settings.database_url)

    from aigate.metering_queue import configure_writer

    configure_writer(get_session_factory())  # inline writer: tests assert right after the call
    try:
        raw, prefix, digest = generate_api_key()
        async with get_session_factory()() as session:
            team = Team(name="Mig Team", slug="mig-team")
            session.add(team)
            await session.flush()
            project = Project(team_id=team.id, name="Mig", slug="mig", rpm_limit=60)
            session.add(project)
            await session.flush()
            session.add(
                ApiKey(project_id=project.id, name="k", prefix=prefix, key_hash=digest)
            )
            session.add(
                ModelPrice(
                    provider="mock",
                    model="mock-ok",
                    input_price_per_1k=0.001,
                    output_price_per_1k=0.002,
                )
            )
            session.add(
                RoutePolicy(
                    name="default",
                    match_model="chat-default",
                    candidates=[{"provider": "mock", "model": "mock-ok"}],
                    fallback=[],
                )
            )
            await session.commit()

        from httpx import ASGITransport, AsyncClient

        from aigate.main import create_app

        app = create_app()
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=True), base_url="http://t"
        ) as client:
            first = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {raw}"},
                json={
                    "model": "chat-default",
                    "messages": [{"role": "user", "content": "迁移库上的第一次调用"}],
                    "temperature": 0,
                },
            )
            assert first.status_code == 200, first.text
            assert first.json()["x_aigate"]["provider"] == "mock"

            # 第二次走缓存查找：这一段之前因为缓存表多出一个模型字段而 500
            second = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {raw}"},
                json={
                    "model": "chat-default",
                    "messages": [{"role": "user", "content": "迁移库上的第一次调用"}],
                    "temperature": 0,
                },
            )
            assert second.status_code == 200, second.text
    finally:
        await dispose_engine()

    # 顺带确认：缓存表里没有多出来的 prompt_raw（那是 trace 表的字段）
    conn = sqlite3.connect(db)
    try:
        columns = {r[1] for r in conn.execute("pragma table_info(semantic_cache_entries)")}
    finally:
        conn.close()
    assert "prompt_raw" not in columns
    assert "prompt_version" in columns