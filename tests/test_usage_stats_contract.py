"""指标口径的契约：同一批行，概览 / 模型对比 / 影子汇总必须是同一个成功率入口。

历史问题：几处各自写了一份白名单和分母，`/overview` 用 2 位小数、`/model-compare`
用 1 位、`/shadow/summary` 又是自己算的一行 `ok / runs`；`/model-compare` 还在没有
调用时返回 `0.0`。值班同时打开两页会看到两个数，而且"未知状态"在其中一处会被算成
成功。这里把三条端点钉在同一份 aigate.usage_stats 上，并把"影子行为什么要 strict=False"
写成可执行的断言：差异只能是分母与字段，不能是白名单。
"""

from __future__ import annotations

import asyncio
import re
import time
import types
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncEngine

from aigate import db as db_mod
from aigate import metrics
from aigate.config import get_settings
from aigate.db import get_session_factory, pool_warm_report, warm_pool
from aigate.models import ShadowRun, UsageRecord
from aigate.usage_stats import (
    OK_STATUSES,
    count_success_rows,
    is_success,
    row_is_success,
    success_rate_of,
    success_rate_of_rows,
)

ADMIN = {"X-Admin-Token": get_settings().admin_token}

SRC = "team-src"
PROJ = "proj-src"
KEY = "key-src"

# 5 行：3 条白名单内成功 + 1 条 error + 1 条表外状态（"mystery"）。
# 期望成功率 3/5 = 60.0；未知状态若被算成功就会变成 80.0，这个用例就是为了抓它。
STATUSES = ["ok", "fallback", "cached", "error", "mystery"]


def _row(status: str, i: int) -> UsageRecord:
    return UsageRecord(
        request_id=f"req_success_rate_{i}",
        team_id=SRC,
        project_id=PROJ,
        api_key_id=KEY,
        provider="mock",
        model_requested="chat-default",
        model_used="mock-ok",
        input_tokens=10,
        output_tokens=5,
        cost_cny=Decimal("0.0001"),
        latency_ms=100 + i,
        status=status,
    )


async def test_whitelist_never_treats_unknown_status_as_success():
    """纯函数层：未知状态算未成功，空集合是 None 而不是 0.0。"""
    assert success_rate_of(STATUSES) == 60.0
    assert success_rate_of(["mystery"]) == 0.0, "全是未知 → 0 成功（不是 100）"
    assert success_rate_of([]) is None
    assert is_success("ok") and not is_success("error") and not is_success("mystery")
    assert "mystery" not in OK_STATUSES


async def test_overview_and_model_compare_agree_on_the_same_rows(client):
    async with get_session_factory()() as session:
        session.add_all([_row(status, i) for i, status in enumerate(STATUSES)])
        await session.commit()

    overview = (await client.get("/admin/v1/overview?days=7", headers=ADMIN)).json()
    compare = (await client.get("/admin/v1/model-compare?days=7", headers=ADMIN)).json()

    window = overview["window"]
    item = next(i for i in compare["items"] if i["model"] == "mock-ok")

    assert window["requests"] == item["calls"] == len(STATUSES)
    assert window["success_rate"] == 60.0
    assert item["success_rate"] == window["success_rate"], (
        "同一批行在两个端点上必须是同一个数；精度/白名单不同源就会在这里裂开"
    )
    assert window["errors"] == 2, "error + 表外状态都算未成功"


async def test_model_compare_with_no_traffic_is_empty_not_zero(client):
    """没有 usage 行时对比表是空表；任何一个 0.0 都会把"没有数据"说成"0% 成功"。"""
    compare = (await client.get("/admin/v1/model-compare?days=7", headers=ADMIN)).json()
    assert compare["items"] == []


async def test_health_reports_pool_warm_count_and_cost(client):
    """预热结果必须能从 /health 读到真实条数与耗时；没预热过就说 cold。"""
    cold = (await client.get("/health")).json()
    assert cold["pool"]["state"] == "cold"
    assert cold["pool"]["warm"] is None, "没预热过不能报 0 条"

    report = await warm_pool()
    assert report["state"] == "warm"
    assert report["warm"] == report["target"] >= 1
    assert report["ms"] >= 0

    body = (await client.get("/health")).json()
    assert body["pool"]["warm"] == report["warm"]
    assert body["pool"]["state"] == "warm"
    assert body["pool"]["error"] is None
    assert pool_warm_report()["warm"] == report["warm"]

@dataclass
class _MixedRow:
    """一行同时带两种口径的字段，用来证明"差异可解释"。

    真实数据里两种行不会同时有 status 和 ok；这个测试刻意把它们放在一起，
    好让两个入口在同一批行上跑出两个数，并要求差值只能来自"看哪个字段"。
    """

    status: str | None
    ok: bool | None


async def test_two_entries_on_the_same_rows_differ_only_by_field_and_denominator():
    """同一批行：调用行口径 50%，影子口径 75%，差的那一条能指着行说清原因。"""
    rows = [
        _MixedRow("ok", True),
        _MixedRow("cached", True),
        _MixedRow("error", False),
        # 网关侧：表外状态 → 未成功（白名单同源）；影子侧：ok=True → 成功。
        _MixedRow("mystery", True),
    ]
    assert success_rate_of_rows(rows) == 50.0
    assert success_rate_of_rows(rows, strict=False) == 75.0
    assert count_success_rows(rows) == (2, 4)
    assert count_success_rows(rows, strict=False) == (3, 4)
    # 精度与"没有数据"的语义完全一致：都是同一个 success_rate。
    assert success_rate_of_rows([]) is None
    assert success_rate_of_rows([], strict=False) is None
    # 两个字段都没有 → 未成功。未知不能被染成绿色。
    assert row_is_success(object(), strict=False) is False


async def test_shadow_summary_uses_the_shared_entry_and_stays_on_its_own_denominator(client):
    """端点层：影子汇总是"影子跑了几次"做分母，成功率来自 usage_stats(strict=False)。"""
    rows = [
        ShadowRun(
            request_id=f"req_shadow_rate_{i}",
            project_id=PROJ,
            provider="mock-alt",
            model="shadow-candidate",
            ok=ok,
            latency_ms=100 + i,
            cost_cny=Decimal("0.0002"),
            output_chars=10,
            output_preview="hi",
            similarity=0.9,
        )
        for i, ok in enumerate([True, True, False])
    ]
    async with get_session_factory()() as session:
        session.add_all(rows)
        await session.commit()

    body = (await client.get("/admin/v1/shadow/summary?days=7", headers=ADMIN)).json()
    item = next(i for i in body["items"] if i["model"] == "shadow-candidate")

    assert item["runs"] == 3, "分母是影子跑了几次，不是网关调用行数"
    assert item["success_rate"] == success_rate_of_rows(rows, strict=False) == 66.67
    # 拿调用行口径套同一批影子行会得到 0.0（它们没有 status）——这正是不能混用的理由。
    assert success_rate_of_rows(rows) == 0.0


async def test_warm_pool_gives_up_after_two_pool_timeouts(client, monkeypatch):
    """DB 迟滞时预热必须有硬上限：到点收工、state=timeout、启动不被拖住。"""
    real_connect = AsyncEngine.connect

    async def slow_connect(self):  # noqa: ANN001 - 只为这一次调用替换
        await asyncio.sleep(5)
        return await real_connect(self)

    monkeypatch.setattr(AsyncEngine, "connect", slow_connect)
    monkeypatch.setattr(
        db_mod, "get_settings", lambda: types.SimpleNamespace(db_pool_timeout=0.05)
    )
    started = time.perf_counter()
    try:
        report = await warm_pool()
    finally:
        monkeypatch.setattr(AsyncEngine, "connect", real_connect)
    elapsed = time.perf_counter() - started

    assert report["state"] == "timeout"
    assert report["warm"] == 0, "一条都没建好就不能报成 warm"
    assert report["target"] >= 1
    assert elapsed < 1.0, f"预热把启动拖了 {elapsed:.2f}s；上限应是 2 × pool_timeout"
    assert "2 x AIGATE_DB_POOL_TIMEOUT=0.05s" in report["error"]

    body = (await client.get("/health")).json()
    assert body["pool"]["state"] == "timeout"
    assert body["pool"]["error"] == report["error"]

    snap = metrics.snapshot()
    assert "aigate_pool_warm_ms" in snap
    assert "aigate_pool_warm_connections" in snap
    assert snap["aigate_pool_warm_connections"] == 0
    assert "aigate_pool_warm_timeout_total" in snap


async def test_pool_warm_gauges_are_actually_in_the_metrics_text(client):
    """`snapshot()` 是内部视图，不是运维拿到的那个东西 —— 这里断言 HTTP 响应体。

    历史问题：`aigate_pool_warm_*` 的测试只查 `metrics.snapshot()`，所以「`/metrics`
    里看得见」这件事从来没有被验证过：render() 的拼装、标签格式、endpoint 返回码
    任何一处坏了，测试都是绿的。预热一次，然后从真实 `/metrics` 文本里 grep 两个 gauge，
    并且要求值与这次预热的报告一致（不是「存在某个数字」）。
    """
    report = await warm_pool()
    assert report["state"] in ("warm", "partial"), f"预热没跑起来：{report}"

    resp = await client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text

    # 按 Prometheus 文本格式逐行解析：`name value`，值不带引号。
    found = dict(re.findall(r"^(aigate_pool_warm_(?:ms|connections)) (\S+)$", text, re.M))
    assert "aigate_pool_warm_ms" in found, f"/metrics 里没有预热耗时 gauge：\n{text}"
    assert "aigate_pool_warm_connections" in found, f"/metrics 里没有预热条数 gauge：\n{text}"
    assert float(found["aigate_pool_warm_ms"]) == float(report["ms"])
    assert int(float(found["aigate_pool_warm_connections"])) == report["warm"]
    # 这两行必须能被 grep 到（运维手册里的命令就是这个形状）
    assert f"aigate_pool_warm_connections {report['warm']:g}" in text
