"""供应商（Providers）：谁在真正提供服务，以及那个服务当前能不能用。

这一层存在的理由很朴素：网关里「provider」原本只是 model_prices 表里的一个字符串，
运营想知道「通义千问那家配好没有、base_url 对不对、还有额度吗」，只能去翻 .env。
所以这里把**除密钥以外的一切**变成可管理的对象，并且提供一次真打来证明它可用。

不把密钥写进数据库是有意的：SQLite 文件同时放着账单和厂商 key，等于每次备份都是一次
凭据泄露；轮换 key 也不该是数据库写操作。控制台只记录「密钥在哪个环境变量里」。
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aigate import provider_config
from aigate.api.deps import Actor, get_actor, require_admin, require_capability
from aigate.config import Settings, get_settings
from aigate.db import get_session
from aigate.models import ModelPrice, ProviderConfig, RoutePolicy, utcnow

router = APIRouter(prefix="/admin/v1", tags=["control-plane"], dependencies=[Depends(require_admin)])


class ProviderPatch(BaseModel):
    label: str | None = Field(default=None, max_length=80)
    base_url: str | None = Field(default=None, max_length=255)
    api_key_env: str | None = Field(default=None, max_length=80)
    enabled: bool | None = None


def _models_for(prices: list[ModelPrice], name: str) -> list[dict[str, Any]]:
    return [
        {
            "model": p.model,
            "input_price_per_1k": float(p.input_price_per_1k or 0),
            "output_price_per_1k": float(p.output_price_per_1k or 0),
            "context_window": p.context_window,
            "enabled": bool(p.enabled),
        }
        for p in prices
        if p.provider == name
    ]


async def _routes_using(session: AsyncSession, name: str) -> list[str]:
    """Which virtual models point at this vendor — deleting one blindly breaks them."""
    policies = (await session.execute(select(RoutePolicy))).scalars().all()
    hits = []
    for policy in policies:
        for candidate in list(policy.candidates or []) + list(policy.fallback or []):
            if candidate.get("provider") == name:
                hits.append(policy.match_model)
                break
    return sorted(hits)


@router.get("/providers")
async def list_providers(
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    require_capability(actor, "providers:read")
    await provider_config.ensure_seeds(session)
    rows = (await session.execute(select(ProviderConfig).order_by(ProviderConfig.name))).scalars().all()
    prices = (await session.execute(select(ModelPrice))).scalars().all()
    await provider_config.load(session, settings, force=True)

    items = []
    for row in rows:
        resolved = await provider_config.get(session, settings, row.name)
        item = provider_config.dump(resolved or row, _models_for(prices, row.name))
        item["used_by_routes"] = await _routes_using(session, row.name)
        item["is_mock"] = row.name == "mock"
        items.append(item)
    return {"items": items, "count": len(items)}


@router.put("/providers/{name}")
async def update_provider(
    name: str,
    patch: ProviderPatch,
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    require_capability(actor, "providers:write")
    row = (
        await session.execute(select(ProviderConfig).where(ProviderConfig.name == name))
    ).scalar_one_or_none()
    if row is None:
        from aigate.providers import known_providers

        if name not in known_providers():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "gateway.provider_not_found", "message": name},
            )
        # Known to the code but not in the table yet (fresh DB, custom bootstrap): create
        # it instead of making the operator run a seed script before editing anything.
        row = ProviderConfig(name=name, label=name, api_key_env=f"AIGATE_{name.upper()}_API_KEY")
        session.add(row)

    fields = patch.model_fields_set
    if "label" in fields and patch.label is not None:
        row.label = patch.label
    if "base_url" in fields and patch.base_url is not None:
        if patch.base_url and not patch.base_url.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "gateway.bad_base_url", "message": "base_url must start with http(s)://"},
            )
        row.base_url = patch.base_url
    if "api_key_env" in fields and patch.api_key_env is not None:
        row.api_key_env = patch.api_key_env
    if "enabled" in fields and patch.enabled is not None:
        row.enabled = patch.enabled
    row.updated_at = utcnow()

    await session.commit()
    provider_config.invalidate()  # 下一次请求就用新配置，不需要重启

    resolved = await provider_config.get(session, settings, name)
    return {"item": provider_config.dump(resolved or row)}


@router.post("/providers", status_code=status.HTTP_201_CREATED)
async def create_provider(
    name: str = Query(..., pattern=r"^[a-z][a-z0-9_-]{1,39}$"),
    label: str = Query("", max_length=80),
    base_url: str = Query("", max_length=255),
    api_key_env: str = Query("", max_length=80),
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Register another vendor (自建 vLLM、某个区域的 Azure 端点……）。

    只登记连接信息；模型与单价仍走 model_prices，路由仍然按 provider 名匹配。
    """
    require_capability(actor, "providers:write")
    exists = (
        await session.execute(select(ProviderConfig).where(ProviderConfig.name == name))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "gateway.provider_exists", "message": name},
        )
    if base_url and not base_url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "gateway.bad_base_url", "message": "base_url must start with http(s)://"},
        )

    row = ProviderConfig(
        name=name,
        label=label or name,
        base_url=base_url,
        api_key_env=api_key_env or f"AIGATE_{name.upper()}_API_KEY",
    )
    session.add(row)
    await session.commit()
    provider_config.invalidate()
    resolved = await provider_config.get(session, settings, name)
    return {"item": provider_config.dump(resolved or row)}


@router.post("/providers/{name}/test")
async def test_provider(
    name: str,
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """真打一次 `GET /models`：证明「配好了」不是一句自述。

    用 /models 而不是 chat：不花钱、不消耗额度，但同样能验证 base_url 与密钥。返回的是
    原始状态码（401 就是 key 不对，404 多半是 base_url 少了 /v1），运营看得懂。
    """
    require_capability(actor, "providers:write")
    await provider_config.ensure_seeds(session)
    cfg = await provider_config.get(session, settings, name)
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "gateway.provider_not_found", "message": name},
        )

    if name == "mock":
        return {"ok": True, "status": 200, "latency_ms": 0, "detail": "offline mock, no network"}

    if not cfg.configured:
        return {
            "ok": False,
            "status": None,
            "latency_ms": 0,
            "detail": f"未配置密钥：环境变量 {cfg.api_key_env or '(未设置)'} 为空",
        }

    url = cfg.base_url.rstrip("/") + "/models"
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {cfg.api_key}"})
        elapsed = int((time.perf_counter() - started) * 1000)
        detail = {
            200: "可用",
            401: "密钥无效（401）",
            403: "无权限（403）",
            404: "路径不对，检查 base_url 是否包含 /v1",
        }.get(resp.status_code, resp.text[:160])
        return {
            "ok": resp.status_code == 200,
            "status": resp.status_code,
            "latency_ms": elapsed,
            "detail": detail,
        }
    except httpx.TimeoutException:
        return {"ok": False, "status": None, "latency_ms": 10_000, "detail": "超时（10s）"}
    except Exception as exc:  # noqa: BLE001 - the message is the point
        return {"ok": False, "status": None, "latency_ms": 0, "detail": f"连接失败：{exc}"[:200]}
