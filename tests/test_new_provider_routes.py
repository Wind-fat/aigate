"""控制台新增的 provider 必须能建路由、能真的接到流量。

真调时暴露的缺口：`admin_ops._validate_candidates()` 只认静态的 `known_providers()`。
在控制台「新增供应商」之后，`POST /routes` 直接 400 `gateway.unknown_provider` —— 数据面
其实早就能解析库里的 provider（`provider_config.get` + `get_configured_provider`），
死在写路由那一步，新增的供应商永远接不到一点流量。

这里走的是控制台上那三步：**登记供应商 → 录密钥 → 建路由**，然后打一次真实请求，
断言选中的确实是这一家（并且用的是库里那份 base_url / 密钥，不是任何内置默认值）。
"""

from __future__ import annotations

import json

from sqlalchemy import select

from aigate.api import v1_chat
from aigate.config import get_settings
from aigate.db import get_session_factory
from aigate.models import UsageRecord
from aigate.providers.mock import MockProvider

ADMIN = {"X-Admin-Token": get_settings().admin_token}
NEW_PROVIDER = "acme-vllm"  # 不在 providers.known_providers() 里，只能来自 provider_configs
BASE_URL = "http://127.0.0.1:9100/v1"
SECRET = "sk-acme-local-0001"


async def _add_provider(client, *, enabled: bool = True) -> None:
    created = await client.post(
        f"/admin/v1/providers?name={NEW_PROVIDER}&label=Acme&base_url={BASE_URL}&api_key_env=ACME_VLLM_KEY",
        headers=ADMIN,
    )
    assert created.status_code == 201, created.text
    stored = await client.put(
        f"/admin/v1/providers/{NEW_PROVIDER}/secret", headers=ADMIN, json={"api_key": SECRET}
    )
    assert stored.status_code == 200, stored.text
    assert stored.json()["configured"] is True
    if not enabled:
        off = await client.put(
            f"/admin/v1/providers/{NEW_PROVIDER}", headers=ADMIN, json={"enabled": False}
        )
        assert off.status_code == 200, off.text
        assert off.json()["item"]["enabled"] is False


async def test_console_added_provider_can_get_a_route_and_serve_traffic(client, auth_headers, monkeypatch):
    await _add_provider(client)

    # ① 建路由：修复前这里就是 400 gateway.unknown_provider
    route = await client.post(
        "/admin/v1/routes?match_model=acme-chat&candidates=acme-vllm/qwen-local&name=acme",
        headers=ADMIN,
    )
    print("POST /admin/v1/routes ->", route.status_code, route.text)
    assert route.status_code == 201, route.text
    body = route.json()
    assert body["item"]["match_model"] == "acme-chat"
    assert body["item"]["candidates"] == [{"provider": NEW_PROVIDER, "model": "qwen-local"}]
    # 没定价仍然是 warning（不是拒绝）：新供应商第一天通常还没录单价
    assert any("未定价" in w for w in body["warnings"]), body["warnings"]

    listed = (await client.get("/admin/v1/routes", headers=ADMIN)).json()
    assert any(r["match_model"] == "acme-chat" for r in listed["items"])

    # ② 调用：真实走 _provider_for -> provider_config（库里那份配置）
    captured: dict[str, str] = {}
    fake = MockProvider()

    def _fake_configured(name: str, base_url: str, api_key: str, timeout_s: float = 60.0):
        captured.update(name=name, base_url=base_url, api_key=api_key)
        return fake

    monkeypatch.setattr(v1_chat, "get_configured_provider", _fake_configured)

    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "acme-chat",
            "messages": [{"role": "user", "content": "你是新来的供应商吗"}],
            "temperature": 0,
        },
    )
    meta_printed = json.dumps(resp.json().get("x_aigate", {}), ensure_ascii=False)
    print("POST /v1/chat/completions ->", resp.status_code, meta_printed)
    assert resp.status_code == 200, resp.text
    meta = resp.json()["x_aigate"]
    assert meta["provider"] == NEW_PROVIDER
    assert meta["model_requested"] == "acme-chat"
    assert meta["model_used"] == "qwen-local"
    assert meta["degraded"] is False

    # 选路真的用的是库里那份连接信息，而不是任何内置默认值
    assert captured == {"name": NEW_PROVIDER, "base_url": BASE_URL, "api_key": SECRET}, captured
    assert fake.calls == 1, "新 provider 必须被真的调用一次"

    # 账单也记在这一家名下
    async with get_session_factory()() as session:
        rows = (await session.execute(select(UsageRecord))).scalars().all()
    assert len(rows) == 1
    assert rows[0].provider == NEW_PROVIDER
    assert rows[0].model_used == "qwen-local"
    assert rows[0].status == "ok"


async def test_disabled_provider_is_still_rejected(client):
    """关了开关的供应商不算「已注册」：路由写进去只会在运行时变成 502。"""
    await _add_provider(client, enabled=False)

    resp = await client.post(
        "/admin/v1/routes?match_model=acme-off&candidates=acme-vllm/qwen-local",
        headers=ADMIN,
    )
    print("disabled provider route ->", resp.status_code, resp.text)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "gateway.unknown_provider"


async def test_typo_is_still_a_400(client):
    """放宽到「库里的 provider」不等于什么都收：真正的笔误必须在写的时候炸。"""
    resp = await client.post(
        "/admin/v1/routes?match_model=typo-chat&candidates=acme-vlm/qwen-local",
        headers=ADMIN,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["unknown"] == ["acme-vlm/qwen-local"]