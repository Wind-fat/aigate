"""迁移必须能独立建出一个可用的库。

为什么单独测：`create_all` 会在启动时补齐缺失的表，所以「代码能跑」**不能**证明
「迁移是对的」。这个项目已经踩过一次——`console_users` 靠 create_all 建出来，
迁移里没有，真机 `alembic upgrade head` 之后登录直接 500；第二次是给
`console_users` 加列，表存在但列不存在。

所以这里从一个空文件开始，只跑 alembic：
  upgrade head -> 期望的表与列都在
  downgrade base -> 只剩 alembic_version
  upgrade head -> 幂等
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _alembic(db: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "AIGATE_DATABASE_URL": f"sqlite+aiosqlite:///{db.as_posix()}",
        # keep the run hermetic: no rate limiter backend needed for DDL
        "AIGATE_RATE_LIMITER": "memory",
    }
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _tables(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    finally:
        conn.close()


def _columns(db: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {r[1] for r in conn.execute(f"pragma table_info({table})")}
    finally:
        conn.close()


EXPECTED = {
    "teams",
    "projects",
    "api_keys",
    "model_prices",
    "route_policies",
    "usage_records",
    "request_traces",
    "semantic_cache_entries",
    "console_users",
    "audit_logs",
    "provider_configs",
}


def test_upgrade_head_builds_the_whole_schema(tmp_path):
    db = tmp_path / "mig.db"
    result = _alembic(db, "upgrade", "head")
    assert result.returncode == 0, result.stderr[-2000:]

    tables = _tables(db)
    assert EXPECTED <= tables, f"missing: {EXPECTED - tables}"

    # the two columns that were added after create_all already knew about the tables
    assert "project_scope" in _columns(db, "console_users")
    assert "password_hash" in _columns(db, "console_users")
    assert "api_key_env" in _columns(db, "provider_configs")
    # and it must not accidentally store vendor secrets
    assert "api_key" not in _columns(db, "provider_configs")


def test_downgrade_base_leaves_only_the_version_table(tmp_path):
    db = tmp_path / "mig.db"
    assert _alembic(db, "upgrade", "head").returncode == 0
    result = _alembic(db, "downgrade", "base")
    assert result.returncode == 0, result.stderr[-2000:]
    assert _tables(db) == {"alembic_version"}


def test_upgrade_is_repeatable(tmp_path):
    db = tmp_path / "mig.db"
    assert _alembic(db, "upgrade", "head").returncode == 0
    again = _alembic(db, "upgrade", "head")
    assert again.returncode == 0, again.stderr[-2000:]