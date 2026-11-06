"""模型与单价：接一个新供应商之后必须能登记它的模型。

没有这张表，路由里就加不了候选——网关必须在发请求之前知道单价，否则「成本优先」
和预算都无从判断。之前它只能靠种子脚本，等于「接 OpenAI」在界面上走不通。
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select

from aigate.config import get_settings
from aigate.db import get_session_factory
from aigate.models import ModelPrice

ADMIN = {"X-Admin-Token": get_settings().admin_token}


async def test_register_a_model_with_price(client: AsyncClient):
    resp = await client.post(
        "/admin/v1/models",
        json={
            "provider": "openai",
            "model": "gpt-4o-mini",
            "input_price_per_1k": 0.0011,
            "output_price_per_1k": 0.0043,
            "context_window": 128000,
        },
        headers=ADMIN,
    )
    assert resp.status_code == 201, resp.text

    listed = await client.get("/admin/v1/models", headers=ADMIN)
    prices = listed.json()["prices"]
    row = next(p for p in prices if p["provider"] == "openai" and p["model"] == "gpt-4o-mini")
    assert row["input_price_per_1k"] == 0.0011


async def test_duplicate_model_is_refused_with_a_hint(client: AsyncClient):
    body = {"provider": "openai", "model": "dup-model", "input_price_per_1k": 1, "output_price_per_1k": 2}
    assert (await client.post("/admin/v1/models", json=body, headers=ADMIN)).status_code == 201
    again = await client.post("/admin/v1/models", json=body, headers=ADMIN)
    assert again.status_code == 409
    assert "改单价" in again.json()["error"]["message"]


async def test_price_change_does_not_rewrite_history(client: AsyncClient):
    """历史账单按当时的单价落库，改价只影响之后的调用。"""
    await client.post(
        "/admin/v1/models",
        json={"provider": "openai", "model": "pricey", "input_price_per_1k": 1, "output_price_per_1k": 1},
        headers=ADMIN,
    )
    await client.put(
        "/admin/v1/models/openai/pricey",
        json={"provider": "openai", "model": "pricey", "input_price_per_1k": 9, "output_price_per_1k": 9},
        headers=ADMIN,
    )
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(ModelPrice).where(ModelPrice.provider == "openai", ModelPrice.model == "pricey")
            )
        ).scalar_one()
        assert str(row.input_price_per_1k) == "9.000000"


async def test_openai_is_a_known_provider(client: AsyncClient):
    listed = await client.get("/admin/v1/providers", headers=ADMIN)
    names = [p["name"] for p in listed.json()["items"]]
    assert "openai" in names


async def test_viewer_cannot_register_models(client: AsyncClient):
    login = await client.post(
        "/console/v1/login", json={"email": "viewer@example.com", "password": "read-only-password"}
    )
    cookies = {"Cookie": f"aigate_session={login.cookies.get('aigate_session')}"}
    resp = await client.post(
        "/admin/v1/models",
        json={"provider": "openai", "model": "sneaky", "input_price_per_1k": 1, "output_price_per_1k": 1},
        headers=cookies,
    )
    assert resp.status_code == 403