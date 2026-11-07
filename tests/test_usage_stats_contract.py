"""指标口径的契约：同一批 usage 行，概览与模型对比必须是同一个成功率。

历史问题：两处各自写了一份白名单和分母，`/overview` 用 2 位小数、`/model-compare`
用 1 位，`/model-compare` 还在没有调用时返回 `0.0`。值班同时打开两页会看到两个数，
而且"未知状态"在其中一处会被算成成功。这里把两条端点钉在同一份 aigate.usage_stats 上。
"""

from __future__ import annotations

from decimal import Decimal

from aigate.config import get_settings
from aigate.db import get_session_factory, pool_warm_report, warm_pool
from aigate.models import UsageRecord
from aigate.usage_stats import OK_STATUSES, is_success, success_rate_of

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