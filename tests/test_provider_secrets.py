"""供应商密钥：能录入、能加密、能在上游真的用上、且任何读接口都看不到明文。

这一组测试针对的是一个真实的阻塞问题：控制台上「未配置密钥 / 测试失败 0ms」——
因为密钥只认环境变量，运维没有任何办法把刚申请到的 key 填进去。
"""

from __future__ import annotations

import json

from sqlalchemy import select

from aigate import provider_config
from aigate.config import get_settings
from aigate.db import get_session_factory
from aigate.models import ProviderConfig
from aigate.secrets import hint, looks_like_key

ADMIN = {"X-Admin-Token": get_settings().admin_token}
KEY = "sk-test-abcdefghijklmnop-9f8e"


async def test_secret_roundtrip_is_encrypted_at_rest(client):
    resp = await client.put("/admin/v1/providers/deepseek/secret", json={"api_key": KEY}, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["key_source"] == "db"
    assert body["key_hint"] == "…9f8e"
    assert body["configured"] is True

    async with get_session_factory()() as session:
        row = (
            await session.execute(select(ProviderConfig).where(ProviderConfig.name == "deepseek"))
        ).scalar_one()
        # 落盘的是密文：既不是明文，也不是"混淆一下"的明文
        assert KEY not in row.api_key_cipher
        assert row.api_key_cipher.startswith("gAAAA")
        assert row.api_key_hint == "…9f8e"
        assert row.api_key_updated_by == "token:admin"


async def test_read_endpoints_never_return_the_plaintext(client):
    await client.put("/admin/v1/providers/deepseek/secret", json={"api_key": KEY}, headers=ADMIN)
    listed = await client.get("/admin/v1/providers", headers=ADMIN)
    assert listed.status_code == 200
    assert KEY not in json.dumps(listed.json())  # 整个响应体里都不能出现
    row = next(p for p in listed.json()["items"] if p["name"] == "deepseek")
    assert row["key_source"] == "db"
    assert row["key_hint"] == "…9f8e"


async def test_the_saved_key_is_what_the_provider_will_use(client):
    """写进去不算数，能解析成上游要用的那把才算。"""
    await client.put("/admin/v1/providers/deepseek/secret", json={"api_key": KEY}, headers=ADMIN)
    settings = get_settings()
    async with get_session_factory()() as session:
        resolved = await provider_config.get(session, settings, "deepseek")
    assert resolved is not None
    assert resolved.api_key == KEY
    assert resolved.key_source == "db"


async def test_env_var_is_the_fallback_when_no_secret_saved(client, monkeypatch):
    monkeypatch.setenv("AIGATE_DEEPSEEK_API_KEY", "sk-from-env-123456")
    settings = get_settings()
    await client.post("/admin/v1/providers/deepseek/test", headers=ADMIN)  # 触发种子
    async with get_session_factory()() as session:
        resolved = await provider_config.get(session, settings, "deepseek")
    assert resolved is not None
    assert resolved.key_source == "env"
    assert resolved.api_key == "sk-from-env-123456"


async def test_clearing_the_secret_falls_back_to_env(client, monkeypatch):
    monkeypatch.setenv("AIGATE_DEEPSEEK_API_KEY", "sk-from-env-123456")
    await client.put("/admin/v1/providers/deepseek/secret", json={"api_key": KEY}, headers=ADMIN)
    cleared = await client.delete("/admin/v1/providers/deepseek/secret", headers=ADMIN)
    assert cleared.status_code == 200
    assert cleared.json()["key_source"] == "env"

    settings = get_settings()
    async with get_session_factory()() as session:
        resolved = await provider_config.get(session, settings, "deepseek")
    assert resolved is not None and resolved.api_key == "sk-from-env-123456"


async def test_obviously_broken_keys_are_refused(client):
    for bad in ["  ", "has space inside-key", "short"]:
        resp = await client.put(
            "/admin/v1/providers/deepseek/secret", json={"api_key": bad}, headers=ADMIN
        )
        assert resp.status_code == 422, bad


async def test_viewer_cannot_set_a_secret(client):
    login = await client.post(
        "/console/v1/login", json={"email": "viewer@example.com", "password": "read-only-password"}
    )
    cookies = {"Cookie": f"aigate_session={login.cookies.get('aigate_session')}"}
    resp = await client.put(
        "/admin/v1/providers/deepseek/secret", json={"api_key": KEY}, headers=cookies
    )
    assert resp.status_code == 403


async def test_wrong_master_key_reports_instead_of_crashing(client, monkeypatch):
    """换过主密钥之后：控制台要能说「解不开，请重新录入」，而不是 500。"""
    await client.put("/admin/v1/providers/deepseek/secret", json={"api_key": KEY}, headers=ADMIN)
    async with get_session_factory()() as session:
        row = (
            await session.execute(select(ProviderConfig).where(ProviderConfig.name == "deepseek"))
        ).scalar_one()
        cipher = row.api_key_cipher

    settings = get_settings()
    monkeypatch.setattr(settings, "secret_key", "a-different-master-key", raising=False)
    plain, err = provider_config.decrypt_secret(settings, cipher)
    assert plain == ""
    assert "re-enter" in err, err


def test_hint_and_validation_rules():
    assert hint("sk-1234567890") == "…7890"
    assert hint("") == ""
    # 自建 vLLM 常常没有前缀，别在这里做厂商正则
    assert looks_like_key("not-needed-but-long-enough")
    assert not looks_like_key("sk-with space")
    assert not looks_like_key("short")