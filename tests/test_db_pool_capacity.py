"""连接池容量：可配、默认不变、启动校验、`/health.db_capacity`。

背景（接手时的问题）：`pool_size`/`max_overflow` 原来完全靠 SQLAlchemy 出厂值（5/10），
容量公式只躺在报告里，代码和部署文档都查不到。运行中改不了，改错也没人告诉你。

三个约束决定了这里的断言：
1. **默认值必须和改之前一模一样**（5/10），用户没拍板容量。所以第一条用例直接拿配置
   和真实 engine 的池对账，而不是信任注释。
2. 校验**只判定不改行为**：不满足公式打 WARNING、写进 `/health`，进程照常启动、照常
   服务。所以 over 的用例同时要求 200 和 warning 文本。
3. 公式：`workers × (pool_size + max_overflow) + 1 预留 ≤ max_conn × 0.8`。
"""

from __future__ import annotations

import logging
import types

import pytest

from aigate import db as db_mod
from aigate.config import Settings
from aigate.db import capacity_report, check_pool_capacity, get_engine, set_engine, warm_pool


def _stub_settings(**overrides) -> types.SimpleNamespace:
    """只带容量相关字段的 Settings 替身（`db.py` 里一律 getattr，所以够用）。"""
    base = {
        "db_pool_size": 5,
        "db_pool_overflow": 10,
        "db_pool_timeout": 1.0,
        "db_workers": 1,
        "db_max_connections": 100,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_settings_defaults_are_the_sqlalchemy_stock_values():
    """不配环境变量时 5/10 —— 与改动前 SQLAlchemy 自带值逐字相同（默认值不动的证据）。"""
    s = Settings(_env_file=None)
    assert (s.db_pool_size, s.db_pool_overflow) == (5, 10)
    assert (s.db_workers, s.db_max_connections) == (1, 100)


async def test_engine_pool_really_uses_the_defaults(client):
    """配置要落到 engine 上才算数：池自己报的 size/overflow 也必须是 5/10。"""
    pool = get_engine().pool
    assert pool.size() == 5
    # SQLAlchemy 2.0 没有公开 getter，上限在 `_max_overflow`（`overflow()` 是当前值）。
    assert pool._max_overflow == 10  # noqa: SLF001 - 上游没有公开 getter


async def test_pool_size_and_overflow_are_configurable(client, monkeypatch, tmp_path):
    """AIGATE_DB_POOL_SIZE / AIGATE_DB_POOL_OVERFLOW 改完真的改变池容量。"""
    monkeypatch.setattr(
        db_mod,
        "get_settings",
        lambda: _stub_settings(db_pool_size=11, db_pool_overflow=3),
    )
    set_engine(f"sqlite+aiosqlite:///{(tmp_path / 'cap.db').as_posix()}")
    pool = get_engine().pool
    assert pool.size() == 11
    assert pool._max_overflow == 3  # noqa: SLF001 - 上游没有公开 getter


async def test_memory_sqlite_still_uses_static_pool(client):
    """`:memory:` 走 StaticPool，不认 pool_size/pool_timeout —— 不能因为新参数而报错。"""
    set_engine("sqlite+aiosqlite:///:memory:")
    pool = get_engine().pool
    assert type(pool).__name__ == "StaticPool"
    # StaticPool 没有 checkedout/size：它只有一条共享连接，所以预热会直接报 skipped，
    # 而不是假装按 pool_size 建了 N 条。
    assert not hasattr(pool, "checkedout")
    report = await warm_pool()
    assert report["state"] == "skipped"
    assert report["warm"] == 0


async def test_health_reports_capacity_ok_by_default(client):
    """默认配置：1 × (5+10) + 1 = 16 ≤ 100 × 0.8 = 80，verdict=ok、没有 warning。"""
    cap = (await client.get("/health")).json()["db_capacity"]

    assert cap["verdict"] == "ok"
    assert cap["fits"] is True
    assert (cap["pool_size"], cap["max_overflow"]) == (5, 10)
    assert cap["per_worker_capacity"] == 15
    assert cap["peak_connections"] == 16
    assert cap["budget"] == 80.0
    assert cap["warning"] is None
    # enforced 只在 PG 上为真：SQLite 没有 max_connections 这种东西（限制在单写者锁上），
    # 那边的判定是"参考"。默认套件跑 SQLite，AIGATE_TEST_DB_URL 指 PG 时这里就是 True。
    assert cap["enforced"] is (cap["dialect"] == "postgresql")
    assert cap["formula"] == "workers x (pool_size + max_overflow) + reserved <= max_connections x 0.8"


async def test_over_capacity_is_warning_and_health_but_not_a_refusal(client, monkeypatch, caplog):
    """手工配一组不满足公式的值：WARNING + /health.db_capacity 说清算式，但服务照旧。"""
    monkeypatch.setattr(
        db_mod,
        "get_settings",
        lambda: _stub_settings(db_pool_size=60, db_pool_overflow=60, db_workers=2),
    )

    with caplog.at_level(logging.WARNING, logger="aigate.db"):
        report = check_pool_capacity()

    # 2 × (60 + 60) + 1 = 241 > 80
    assert report["verdict"] == "over"
    assert report["fits"] is False
    assert report["peak_connections"] == 241
    assert "241" in report["warning"] and "80" in report["warning"]
    assert any("over budget" in rec.getMessage() for rec in caplog.records), (
        "不满足公式必须留下 WARNING，否则值班看不到"
    )

    resp = await client.get("/health")
    cap = resp.json()["db_capacity"]
    assert resp.status_code == 200, "配置不满足公式也不能让 /health 5xx"
    assert cap["verdict"] == "over"
    assert cap["warning"] == report["warning"]
    assert cap["peak_connections"] == 241


async def test_capacity_report_without_a_ceiling_says_unknown(monkeypatch):
    """AIGATE_DB_MAX_CONNECTIONS=0 表示"没有上限可校"：说 unknown，不假装 ok。"""
    monkeypatch.setattr(db_mod, "get_settings", lambda: _stub_settings(db_max_connections=0))
    cap = capacity_report()
    assert cap["verdict"] == "unknown"
    assert cap["fits"] is None
    assert cap["warning"] is None


@pytest.mark.parametrize(
    ("pool_size", "overflow", "workers", "max_conn", "expected"),
    [
        (10, 20, 1, 100, "ok"),  # PG 单副本推荐值：31 ≤ 80
        (10, 10, 2, 100, "ok"),  # 2 workers：41 ≤ 80
        (10, 10, 4, 100, "over"),  # 4 workers：81 > 80 —— 刚好越线
        (10, 20, 6, 100, "over"),  # 6 workers：181 > 80
        (5, 10, 1, 20, "ok"),  # 小实例 max_connections=20：16 = 16，卡在预算线上，按 <= 算过
        (5, 10, 1, 19, "over"),  # 再小一条就过载：16 > 15.2
    ],
)
async def test_formula_table(client, monkeypatch, pool_size, overflow, workers, max_conn, expected):
    """公式本身的表格用例：边界值（等于预算/越线一格）都要按 <= 判。"""
    monkeypatch.setattr(
        db_mod,
        "get_settings",
        lambda: _stub_settings(
            db_pool_size=pool_size,
            db_pool_overflow=overflow,
            db_workers=workers,
            db_max_connections=max_conn,
        ),
    )
    assert capacity_report()["verdict"] == expected
