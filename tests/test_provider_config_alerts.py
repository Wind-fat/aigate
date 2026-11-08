"""配置类告警：缺密钥 / 供应商被停用 / 路由指向不存在的 provider，要能叫醒值班。

上一轮把「上游挂了」变成了告警行，但同一份分流里 `retryable=False` 的那条路什么也不写：
`provider_unconfigured`、`provider_disabled`、`provider_unknown` 只在 `/providers` 和 `/ops`
上可见，**不会半夜叫人**。而「主供应商的密钥滚失效了」正是最该在十分钟内知道的事。

这里钉住四件事，断言全部落在库里的行 / 端点返回 / 计数器上：

1. 缺密钥 → 一条 `metric="provider_config"`、`level="warn"` 的行，字段可读、message 说清该修什么；
   连打三次仍然只有一条（去重），且**不**产生 `upstream_failure`。
2. 配好密钥 → 下一次请求把它自动 resolve（`resolved_at` / `period_key="closed:…"` /
   `detail.duration_s`），恢复后默认列表不再显示。
3. 客户端传错（厂商 4xx / 不存在的模型名）→ **不产生任何告警**，`config_errors` 计数器也不动。
   告警风暴的入口就在这里：把「调用方自己写错了」当成事故。
4. 被手动停用、路由指向不存在的 provider 各有自己的 reason，级别仍是 warn 而不是 critical。
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select

from aigate.config import get_settings
from aigate.db import get_session_factory
from aigate.models import Alert, RoutePolicy

ADMIN = {"X-Admin-Token": get_settings().admin_token}
MISSING_ENV = "AIGATE_VLLM_API_KEY"


def _payload(model: str = "chat-default", text: str = "ping") -> dict:
    return {"model": model, "messages": [{"role": "user", "content": text}], "temperature": 0}


async def _alerts(metric: str | None = None) -> list[dict]:
    """把 provider 作用域的告警读成扁平 dict —— 控制台看到的就是这些字段。"""
    async with get_session_factory()() as session:
        stmt = select(Alert).where(Alert.scope_type == "provider")
        if metric is not None:
            stmt = stmt.where(Alert.metric == metric)
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": r.id,
                "level": r.level,
                "scope_type": r.scope_type,
                "scope_id": r.scope_id,
                "metric": r.metric,
                "period_key": r.period_key,
                "value": int(r.value),
                "limit_value": int(r.limit_value),
                "message": r.message,
                "detail": dict(r.detail or {}),
                "created_at": r.created_at,
                "acknowledged_at": r.acknowledged_at,
                "resolved_at": r.resolved_at,
            }
            for r in rows
        ]


def _show(label: str, row: dict) -> None:
    """把一行原样打出来（时间转字符串），报告里贴的就是这个。"""
    print(label, json.dumps({**row, "created_at": str(row["created_at"])}, default=str, ensure_ascii=False))


async def _route_to(client, provider: str, model: str = "qwen-plus", route: str = "chat-default"):
    resp = await client.put(
        f"/admin/v1/routes/{route}",
        headers=ADMIN,
        json={"candidates": [{"provider": provider, "model": model}]},
    )
    assert resp.status_code == 200, resp.text
    return resp


async def test_missing_api_key_opens_one_warn_config_alert(client, auth_headers, monkeypatch):
    """① 缺密钥 → 落一条 warn；连打三次只有一条；上游故障那条不许被顺手写出来。"""
    monkeypatch.delenv(MISSING_ENV, raising=False)
    await _route_to(client, "vllm")

    # 3 次 = 上游告警的默认阈值。配置类没有阈值：第一次就该有，三次之后也只允许一条。
    for _ in range(3):
        resp = await client.post("/v1/chat/completions", headers=auth_headers, json=_payload())
        assert resp.status_code == 502, resp.text
        assert resp.json()["error"]["code"] == "provider_unconfigured"

    rows = await _alerts()
    assert len(rows) == 1, rows
    a = rows[0]
    _show("config alert row:", a)

    assert a["scope_type"] == "provider" and a["scope_id"] == "vllm"
    assert a["metric"] == "provider_config"
    assert a["level"] == "warn", "配置类是 warn：critical 留给「上游在挂、流量正在丢」"
    assert a["period_key"] == "open" and a["resolved_at"] is None
    assert a["limit_value"] == 0, "配置类没有阈值"
    assert a["detail"]["provider"] == "vllm"
    assert a["detail"]["reason"] == "provider_unconfigured"
    assert a["detail"]["reasons"] == ["provider_unconfigured"]
    assert a["detail"]["error_code"] == "provider_unconfigured"
    assert a["detail"]["first_seen_at"], "首次出现时间必须落库（持续时长靠它算）"
    # 「该修什么」要具体到环境变量名和下一步动作，不是一句 unconfigured
    assert MISSING_ENV in a["detail"]["fix"], a["detail"]["fix"]
    assert MISSING_ENV in a["message"], a["message"]
    assert "config fix" in a["message"]

    # 同一段故障里继续失败：不刷第二条
    for _ in range(2):
        await client.post("/v1/chat/completions", headers=auth_headers, json=_payload())
    assert len(await _alerts()) == 1, "配置类也必须去重（同一 provider 同时只有一条）"
    assert await _alerts(metric="upstream_failure") == [], "缺密钥不是上游故障"

    # 控制台读的就是这个端点
    listed = (await client.get("/admin/v1/alerts", headers=ADMIN)).json()
    assert listed["count"] == 1, listed
    item = listed["items"][0]
    print("GET /admin/v1/alerts item:", json.dumps(item, ensure_ascii=False))
    assert item["level"] == "warn" and item["metric"] == "provider_config"
    assert item["resolved_at"] is None and item["acknowledged_at"] is None

    # /ops 的两个数是分开的：配置失败不许混进上游错误率
    health = (await client.get("/admin/v1/ops-health", headers=ADMIN)).json()
    assert health["counters"]["config_errors"] == 5, health["counters"]
    assert health["counters"]["upstream_errors"] == 0, health["counters"]


async def test_configuring_the_key_resolves_the_alert(client, auth_headers, monkeypatch):
    """② 补上密钥 → 下一次请求自动 resolve（不用人点、也不等这次调用成功）。"""
    monkeypatch.delenv(MISSING_ENV, raising=False)
    await _route_to(client, "vllm")

    resp = await client.post("/v1/chat/completions", headers=auth_headers, json=_payload())
    assert resp.json()["error"]["code"] == "provider_unconfigured"
    before = await _alerts(metric="provider_config")
    assert len(before) == 1 and before[0]["resolved_at"] is None

    # 配好配置：写密钥 + 把 base_url 指到一个必然拒绝连接的端口，让「配置解析成功」和
    # 「这次调用成功」明确分开 —— 关告警的条件是前者。
    put = await client.put(
        "/admin/v1/providers/vllm",
        headers=ADMIN,
        json={"base_url": "http://127.0.0.1:9/v1"},
    )
    assert put.status_code == 200, put.text
    secret = await client.put(
        "/admin/v1/providers/vllm/secret", headers=ADMIN, json={"api_key": "sk-test-abcdef123456"}
    )
    assert secret.status_code == 200, secret.text
    assert secret.json()["configured"] is True

    after_call = await client.post("/v1/chat/completions", headers=auth_headers, json=_payload())
    # 配置现在解析得动了：错误码不再说 unconfigured，而是上游连不上（这是另一件事）
    assert after_call.status_code == 502, after_call.text
    assert after_call.json()["error"]["code"] != "provider_unconfigured", after_call.text

    rows = await _alerts(metric="provider_config")
    assert len(rows) == 1, rows
    row = rows[0]
    _show("resolved config row:", row)
    assert row["resolved_at"] is not None, "配置修好必须自动 resolve"
    assert row["period_key"].startswith("closed:"), row["period_key"]
    assert row["detail"]["resolved_at"] and row["detail"]["duration_s"] is not None
    assert "config fixed after" in row["message"], row["message"]
    assert row["detail"]["occurrences"] == 1, "关掉时把这段故障的最终次数写死"
    assert row["acknowledged_at"] is None, "自动恢复不等于有人看过"

    # 默认视图（待处理）不再显示；带历史才看得到
    pending = (await client.get("/admin/v1/alerts", headers=ADMIN)).json()
    assert pending["count"] == 0, pending
    history = (await client.get("/admin/v1/alerts?include_acknowledged=true", headers=ADMIN)).json()
    assert any(i["id"] == row["id"] for i in history["items"]), history


async def test_disabled_provider_is_warn_and_reenabling_resolves(client, auth_headers):
    """被手动停用是「人去改配置」，不是上游挂了：warn + 一条可恢复的行。"""
    await client.put("/admin/v1/providers/mock", headers=ADMIN, json={"enabled": False})
    resp = await client.post("/v1/chat/completions", headers=auth_headers, json=_payload())
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "provider_disabled"

    rows = await _alerts(metric="provider_config")
    assert len(rows) == 1, rows
    a = rows[0]
    _show("disabled alert:", a)
    assert a["level"] == "warn"
    assert a["scope_id"] == "mock"
    assert a["detail"]["reason"] == "provider_disabled"
    assert '{"enabled": true}' in a["detail"]["fix"], a["detail"]["fix"]

    await client.put("/admin/v1/providers/mock", headers=ADMIN, json={"enabled": True})
    ok = await client.post("/v1/chat/completions", headers=auth_headers, json=_payload())
    assert ok.status_code == 200, ok.text
    assert ok.json()["x_aigate"]["provider"] == "mock"

    healed = await _alerts(metric="provider_config")
    assert len(healed) == 1 and healed[0]["resolved_at"] is not None, healed


async def test_route_pointing_at_an_unknown_provider_alerts_but_is_not_critical(client, auth_headers):
    """路由里写了不存在的 provider：数据面报 provider_unknown，告警写成 unknown_provider。

    这条只能靠直接写库造出来 —— 控制台保存路由时会先挡住（400 `gateway.unknown_provider`），
    但 runbook 里那条「改表实时生效」的 SQL 不会挡。
    """
    async with get_session_factory()() as session:
        policy = (
            await session.execute(select(RoutePolicy).where(RoutePolicy.match_model == "chat-default"))
        ).scalar_one()
        policy.candidates = [{"provider": "ghost-vendor", "model": "x"}]
        await session.commit()

    resp = await client.post("/v1/chat/completions", headers=auth_headers, json=_payload())
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "provider_unknown"

    rows = await _alerts(metric="provider_config")
    assert len(rows) == 1, rows
    a = rows[0]
    _show("unknown provider alert:", a)
    assert a["level"] == "warn"
    assert a["scope_id"] == "ghost-vendor"
    assert a["detail"]["reason"] == "unknown_provider"
    assert a["detail"]["error_code"] == "provider_unknown"
    assert "/admin/v1/routes" in a["detail"]["fix"], a["detail"]["fix"]


async def test_ack_then_manual_resolve_then_a_new_row(client, auth_headers):
    """确认、手工关、再坏一次 → 开新的一条。三件事都不能互相吃掉。"""
    await client.put("/admin/v1/providers/mock", headers=ADMIN, json={"enabled": False})
    await client.post("/v1/chat/completions", headers=auth_headers, json=_payload())
    rows = await _alerts(metric="provider_config")
    assert len(rows) == 1, rows
    first = rows[0]

    ack = await client.post(f"/admin/v1/alerts/{first['id']}/ack", headers=ADMIN)
    assert ack.status_code == 200, ack.text
    rows = await _alerts(metric="provider_config")
    assert rows[0]["acknowledged_at"] is not None

    # 手工关：开告警的副本先挂了 / 人判断这段配置故障到此为止
    resolved = await client.post(f"/admin/v1/alerts/{first['id']}/resolve", headers=ADMIN)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["period_key"].startswith("closed:"), resolved.text
    rows = await _alerts(metric="provider_config")
    assert rows[0]["resolved_at"] is not None

    # 还是坏的：必须开**新**一条，而不是复活刚才那条被关掉的行
    await client.post("/v1/chat/completions", headers=auth_headers, json=_payload())
    rows = await _alerts(metric="provider_config")
    assert len(rows) == 2, rows
    fresh = next(r for r in rows if r["period_key"] == "open")
    assert fresh["id"] != first["id"]
    assert fresh["acknowledged_at"] is None, "新事故要重新叫人，不能继承上一条的确认"

    # 修好之后，新的那条照样自动 resolve
    await client.put("/admin/v1/providers/mock", headers=ADMIN, json={"enabled": True})
    ok = await client.post("/v1/chat/completions", headers=auth_headers, json=_payload())
    assert ok.status_code == 200, ok.text
    rows = await _alerts(metric="provider_config")
    assert all(r["resolved_at"] is not None for r in rows), rows


async def test_client_side_bad_request_pages_nobody(client, auth_headers):
    """③ 厂商 4xx（调用方传错模型名）→ 一条告警都不许有，配置计数器也不许动。

    这是告警风暴的入口：把「调用方自己写错了」当成事故，一次压测就能刷满整页。
    """
    for _ in range(3):
        resp = await client.post(
            "/v1/chat/completions", headers=auth_headers, json=_payload("bad-request")
        )
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "upstream_4xx"

    # 客户端瞎写的模型名：路由里没有它，落到默认策略上，mock 照答 —— 不是配置问题。
    bogus = await client.post("/v1/chat/completions", headers=auth_headers, json=_payload("no-such-model"))
    assert bogus.status_code in (200, 502), bogus.text

    assert await _alerts() == [], "客户端传错不许产生任何 provider 告警"
    health = (await client.get("/admin/v1/ops-health", headers=ADMIN)).json()
    assert health["counters"]["config_errors"] == 0, health["counters"]


async def test_streaming_path_alerts_on_missing_key_too(client, auth_headers, monkeypatch, caplog):
    """流式与非流式是两条代码路径：告警不能只挂在其中一条上。"""
    monkeypatch.delenv(MISSING_ENV, raising=False)
    await _route_to(client, "vllm")

    with caplog.at_level(logging.WARNING, logger="aigate.upstream"):
        resp = await client.post(
            "/v1/chat/completions", headers=auth_headers, json={**_payload(), "stream": True}
        )
        assert resp.status_code == 200, resp.text
        assert "provider_unconfigured" in resp.text, resp.text[:400]

    rows = await _alerts(metric="provider_config")
    assert len(rows) == 1 and rows[0]["level"] == "warn", rows

    logged = [r for r in caplog.records if r.name == "aigate.upstream"]
    assert logged, "必须有 WARNING 日志"
    last = logged[-1]
    assert getattr(last, "provider", None) == "vllm"
    assert getattr(last, "reason", None) == "provider_unconfigured"
    assert getattr(last, "alerting", None) is True