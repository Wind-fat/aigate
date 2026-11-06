"""规格核对后的补口：路由策略、三级配额、日预算、告警、prompt 版本、原文开关、影子流量。

这些测试对应「P0/P1 清单」里曾经只是标签、没有真实行为的部分。每个用例都断言**行为**，
不断言实现：例如 cost_first 断言「第一次调用打到了更便宜的那家」，而不是「排序函数返回了什么」。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from aigate import shadow as shadow_mod
from aigate.config import get_settings
from aigate.db import get_session_factory
from aigate.models import Alert, ApiKey, Project, RoutePolicy, ShadowRun, Team, UsageRecord

ADMIN = {"X-Admin-Token": get_settings().admin_token}


# ------------------------------------------------------------------ 路由策略


async def _add_price(provider: str, model: str, inp: str, outp: str) -> None:
    from aigate.models import ModelPrice
    from aigate.security import hash_api_key  # noqa: F401 - keeps import graph warm

    async with get_session_factory()() as session:
        session.add(
            ModelPrice(
                provider=provider,
                model=model,
                input_price_per_1k=Decimal(inp),
                output_price_per_1k=Decimal(outp),
            )
        )
        await session.commit()


async def _set_policy(match_model: str, **fields) -> None:
    async with get_session_factory()() as session:
        policy = (
            await session.execute(select(RoutePolicy).where(RoutePolicy.match_model == match_model))
        ).scalar_one()
        for key, value in fields.items():
            setattr(policy, key, value)
        await session.commit()


async def test_cost_first_actually_picks_the_cheaper_candidate(client, auth_headers, chat_payload):
    """`strategy` 以前只是个标签：候选顺序永远按配置来。现在它决定真正的顺序。"""
    await _add_price("mock-alt", "mock-cheap", "0.0001", "0.0002")
    await _set_policy(
        "chat-default",
        strategy="cost_first",
        candidates=[
            {"provider": "mock", "model": "mock-ok"},  # 0.003 / 1k 合计
            {"provider": "mock-alt", "model": "mock-cheap"},  # 0.0003 / 1k 合计
        ],
    )

    resp = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["x_aigate"]["provider"] == "mock-alt"


async def test_static_keeps_the_configured_order(client, auth_headers, chat_payload):
    await _add_price("mock-alt", "mock-cheap", "0.0001", "0.0002")
    await _set_policy(
        "chat-default",
        strategy="static",
        candidates=[
            {"provider": "mock", "model": "mock-ok"},
            {"provider": "mock-alt", "model": "mock-cheap"},
        ],
    )
    resp = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert resp.json()["x_aigate"]["provider"] == "mock"


async def test_latency_first_uses_recent_history(client, auth_headers, chat_payload):
    """没有历史延迟的候选排在有历史的后面——“未知”不等于“快”。"""
    async with get_session_factory()() as session:
        project = (await session.execute(select(Project))).scalars().first()
        key = (await session.execute(select(ApiKey))).scalars().first()
        for provider, ms in (("mock", 900), ("mock-alt", 40)):
            session.add(
                UsageRecord(
                    request_id=f"req_hist_{provider}",
                    team_id=project.team_id,
                    project_id=project.id,
                    api_key_id=key.id,
                    provider=provider,
                    model_requested="chat-default",
                    model_used="mock-ok",
                    cost_cny=Decimal("0"),
                    latency_ms=ms,
                    status="ok",
                    created_at=datetime.now(UTC) - timedelta(hours=1),
                )
            )
        await session.commit()

    await _set_policy(
        "chat-default",
        strategy="latency_first",
        candidates=[
            {"provider": "mock", "model": "mock-ok"},
            {"provider": "mock-alt", "model": "mock-ok"},
        ],
    )
    resp = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert resp.json()["x_aigate"]["provider"] == "mock-alt"


# --------------------------------------------------------------- 三级配额


async def test_key_level_rpm_is_enforced(client, auth_headers, chat_payload):
    async with get_session_factory()() as session:
        from conftest import TEST_KEY_PLAIN

        from aigate.security import hash_api_key

        key = (
            await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_api_key(TEST_KEY_PLAIN)))
        ).scalar_one()
        key.rpm_limit = 1
        await session.commit()

    first = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert first.status_code == 200

    second = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert second.status_code == 429
    body = second.json()["error"]
    assert body["scope"] == "key"
    assert "API key" in body["message"]


async def test_team_rpm_is_enforced(client, auth_headers, chat_payload):
    async with get_session_factory()() as session:
        team = (await session.execute(select(Team))).scalars().first()
        team.rpm_limit = 1
        await session.commit()

    assert (
        await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    ).status_code == 200
    blocked = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["scope"] == "team"


async def test_daily_budget_stops_the_all_nighter(client, auth_headers, chat_payload):
    """日预算的价值是“凌晨跑飞的循环”在天亮前就被挡住。"""
    async with get_session_factory()() as session:
        project = (await session.execute(select(Project))).scalars().first()
        project.daily_budget_cny = Decimal("0.000001")
        await session.commit()

    # The cap is checked against *already spent* money, so the first call is the one
    # that accrues the spend and the second is the one that gets refused.
    first = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert first.status_code == 200

    resp = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert resp.status_code == 429
    body = resp.json()["error"]
    assert body["metric"] == "budget_daily"
    assert body["scope"] == "project"

    alerts = (await client.get("/admin/v1/alerts", headers=ADMIN)).json()["items"]
    assert any(a["metric"] == "budget_daily" for a in alerts)


async def test_team_monthly_budget_is_enforced_and_reports_scope(client, auth_headers, chat_payload):
    async with get_session_factory()() as session:
        team = (await session.execute(select(Team))).scalars().first()
        team.monthly_budget_cny = Decimal("0.000001")
        await session.commit()

    assert (
        await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    ).status_code == 200
    resp = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert resp.status_code == 429
    assert resp.json()["error"]["scope"] == "team"
    assert resp.json()["error"]["metric"] == "budget_monthly"


# --------------------------------------------------------------- 80% 告警


async def test_crossing_eighty_percent_raises_exactly_one_alert(client, auth_headers, chat_payload):
    async with get_session_factory()() as session:
        project = (await session.execute(select(Project))).scalars().first()
        # one mock call costs ~2.4e-05; set the cap so that lands over 80%
        project.monthly_budget_cny = Decimal("0.000028")
        await session.commit()

    for _ in range(3):
        await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)

    async with get_session_factory()() as session:
        alerts = (await session.execute(select(Alert))).scalars().all()

    budget_alerts = [a for a in alerts if a.metric == "budget_monthly"]
    assert len(budget_alerts) == 1, "重复告警会把真正的信号淹掉（唯一索引去重）"
    assert budget_alerts[0].level in ("warn", "critical")


async def test_warning_escalates_to_critical_and_reopens(client, auth_headers, chat_payload):
    """先 80% 报警、再 100%：同一条告警要升级并重新变成未确认。

    只靠唯一索引去重会把 critical 悄悄吞掉——值班确认过 80% 之后，超支就没有任何提示。
    注意时序：告警基于**已落库**的金额，所以判定永远比产生它的那次调用晚一步（预算检查在
    写库之前）。这是有意的——预算是"已经花了多少"的闸门，不是"这一笔将花多少"的预测。
    """
    from aigate.models import Project

    await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    spent = (await client.get("/admin/v1/projects/summary", headers=ADMIN)).json()["items"][0][
        "month_spend_cny"
    ]
    assert spent > 0

    # 让已花金额占 90%：下一次请求仍被放行，但检查时会写出 warn 告警
    async with get_session_factory()() as session:
        project = (await session.execute(select(Project))).scalars().first()
        project.monthly_budget_cny = Decimal(str(spent)) / Decimal("0.9")
        await session.commit()

    allowed = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert allowed.status_code == 200

    async with get_session_factory()() as session:
        alert = (
            await session.execute(select(Alert).where(Alert.metric == "budget_monthly"))
        ).scalar_one()
        assert alert.level == "warn"

    # 值班点过「知道了」
    ack = await client.post(f"/admin/v1/alerts/{alert.id}/ack", headers=ADMIN)
    assert ack.status_code == 200

    # 预算下调到已花金额之下：下一次请求既被拒绝，也必须把告警升级回未确认
    async with get_session_factory()() as session:
        project = (await session.execute(select(Project))).scalars().first()
        project.monthly_budget_cny = Decimal(str(spent)) / Decimal("2")
        await session.commit()

    blocked = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert blocked.status_code == 429

    async with get_session_factory()() as session:
        alert = (
            await session.execute(select(Alert).where(Alert.metric == "budget_monthly"))
        ).scalar_one()
        assert alert.level == "critical"
        assert alert.acknowledged_at is None


async def test_alert_can_be_acknowledged(client, auth_headers, chat_payload):
    async with get_session_factory()() as session:
        project = (await session.execute(select(Project))).scalars().first()
        project.monthly_budget_cny = Decimal("0.000028")
        await session.commit()
    # two calls: the first accrues the spend that trips the 80% warning
    for _ in range(2):
        await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)

    items = (await client.get("/admin/v1/alerts", headers=ADMIN)).json()["items"]
    assert items
    ack = await client.post(f"/admin/v1/alerts/{items[0]['id']}/ack", headers=ADMIN)
    assert ack.status_code == 200
    after = (await client.get("/admin/v1/alerts", headers=ADMIN)).json()["items"]
    assert all(a["id"] != items[0]["id"] for a in after)


# --------------------------------------------------------------- prompt 版本


async def test_prompt_version_is_part_of_the_cache_key(client, auth_headers, chat_payload):
    """模板一改，缓存必须整体失效——否则用户拿到的是上一个版本的答案。"""
    first = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert first.status_code == 200

    same = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert same.json()["x_aigate"]["cache_hit"] in ("L1", "L2")

    bumped = await client.post(
        "/v1/chat/completions",
        json=chat_payload,
        headers={**auth_headers, "X-Prompt-Version": "v2"},
    )
    assert bumped.json()["x_aigate"]["cache_hit"] is None

    async with get_session_factory()() as session:
        versions = {
            v
            for v in (
                await session.execute(select(UsageRecord.prompt_version))
            ).scalars()
            if v
        }
    assert {"v1", "v2"} <= versions


# --------------------------------------------------------------- 原文开关


async def test_trace_stores_masked_prompt_by_default(client, auth_headers, chat_payload):
    request_id = (
        await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    ).json()["x_aigate"]["request_id"]

    trace = (await client.get(f"/admin/v1/traces/{request_id}", headers=ADMIN)).json()
    assert trace["prompt_raw"] is None, "默认不保存用户原文"
    assert trace["prompt_preview"]


async def test_raw_prompt_mode_is_opt_in(client, auth_headers, chat_payload):
    settings = get_settings()
    original = settings.trace_prompt_mode
    settings.trace_prompt_mode = "raw"
    try:
        request_id = (
            await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
        ).json()["x_aigate"]["request_id"]
    finally:
        settings.trace_prompt_mode = original

    trace = (await client.get(f"/admin/v1/traces/{request_id}", headers=ADMIN)).json()
    assert trace["prompt_raw"]


# --------------------------------------------------------------- 影子流量


async def test_shadow_runs_are_recorded_but_not_billed(client, auth_headers, chat_payload):
    await _set_policy("chat-default", shadow=[{"provider": "mock-alt", "model": "mock-ok"}])

    resp = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert resp.status_code == 200

    # the shadow call is a background task: wait for it instead of sleeping a fixed time
    if shadow_mod._PENDING:
        await asyncio.gather(*list(shadow_mod._PENDING), return_exceptions=True)

    async with get_session_factory()() as session:
        shadows = (await session.execute(select(ShadowRun))).scalars().all()
        usage = (await session.execute(select(UsageRecord))).scalars().all()

    assert len(shadows) == 1
    assert shadows[0].provider == "mock-alt"
    assert shadows[0].ok is True
    assert shadows[0].similarity is not None
    # 影子花费不进账单：否则每份成本报表都会系统性偏高
    assert len([u for u in usage if u.provider == "mock-alt"]) == 0


async def test_shadow_also_runs_for_streaming_requests(client, auth_headers, chat_payload):
    """流式请求也要有影子对照。

    流式的答案是在生成器里一点点吐出来的，响应函数早就返回了——影子必须在流结束、
    记账落库之后才发，否则「流式调用一条影子数据都没有」这种缺陷会安静地存在很久。
    """
    await client.put(
        "/admin/v1/routes/chat-default",
        json={"shadow": [{"provider": "mock-alt", "model": "mock-ok"}]},
        headers=ADMIN,
    )

    payload = dict(chat_payload, stream=True)
    async with client.stream(
        "POST", "/v1/chat/completions", json=payload, headers=auth_headers
    ) as resp:
        assert resp.status_code == 200
        async for _ in resp.aiter_lines():
            pass

    # 影子是 create_task 发出去的，给它一点时间落库
    for _ in range(40):
        await asyncio.sleep(0.05)
        async with get_session_factory()() as session:
            n = (
                await session.execute(
                    select(func.count()).select_from(ShadowRun).where(
                        ShadowRun.provider == "mock-alt"
                    )
                )
            ).scalar_one()
        if n:
            break
    assert n >= 1, "streaming request produced no shadow run"


async def test_shadow_failure_does_not_affect_the_response(client, auth_headers, chat_payload):
    await _set_policy("chat-default", shadow=[{"provider": "mock-alt", "model": "badrequest-model"}])

    resp = await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["x_aigate"]["degraded"] is False

    if shadow_mod._PENDING:
        await asyncio.gather(*list(shadow_mod._PENDING), return_exceptions=True)

    async with get_session_factory()() as session:
        shadows = (await session.execute(select(ShadowRun))).scalars().all()
    assert shadows and shadows[0].ok is False


# --------------------------------------------------------------- 团队维度


async def test_usage_can_be_filtered_and_grouped_by_team(client, auth_headers, chat_payload):
    await client.post("/v1/chat/completions", json=chat_payload, headers=auth_headers)

    async with get_session_factory()() as session:
        team = (await session.execute(select(Team))).scalars().first()

    body = (await client.get(f"/admin/v1/usage?team_id={team.id}", headers=ADMIN)).json()
    assert body["total"] >= 1

    overview = (await client.get("/admin/v1/overview?days=1", headers=ADMIN)).json()
    assert any(row["team"] == team.slug for row in overview["by_team"])


async def test_team_budget_is_editable(client):
    async with get_session_factory()() as session:
        team = (await session.execute(select(Team))).scalars().first()

    resp = await client.put(
        f"/admin/v1/teams/{team.id}",
        headers=ADMIN,
        json={"rpm_limit": 1200, "monthly_budget_cny": 500},
    )
    assert resp.status_code == 200
    assert resp.json()["rpm_limit"] == 1200

    listing = (await client.get("/admin/v1/teams", headers=ADMIN)).json()["items"]
    row = next(t for t in listing if t["id"] == team.id)
    assert row["rpm_limit"] == 1200
    assert row["monthly_budget_cny"] == 500