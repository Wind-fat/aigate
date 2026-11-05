"""Point the gateway at a real provider.

The demo ships with `mock` so everything runs without a key. This script flips a route
to a real model and keeps `mock-alt` as the fallback, so the degradation path still works
if the vendor misbehaves.

Usage:
  uv run python scripts/use_provider.py dashscope qwen-plus --key-env AIGATE_DASHSCOPE_API_KEY
  uv run python scripts/use_provider.py deepseek deepseek-chat --verify
  uv run python scripts/use_provider.py --list

The API key is never passed as an argument (it would end up in your shell history):
put it in `.env` and this script only checks that it is non-empty.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal

import httpx
from sqlalchemy import select

from aigate.config import get_settings
from aigate.db import get_session_factory, init_db, set_engine
from aigate.models import ModelPrice, RoutePolicy

# provider -> (base_url setting, key setting, known cheap default model, price per 1k)
PROVIDERS = {
    # `mock` is the offline provider: switching back to it must always work, which is
    # what makes "did I break something or is the vendor down?" answerable in one command.
    "mock": (None, None, "mock-ok", ("0.001", "0.002")),
    "dashscope": ("dashscope_base_url", "dashscope_api_key", "qwen-plus", ("0.0008", "0.002")),
    "deepseek": ("deepseek_base_url", "deepseek_api_key", "deepseek-chat", ("0.001", "0.002")),
    "vllm": ("vllm_base_url", "vllm_api_key", "qwen3", ("0", "0")),
}


async def _apply(provider: str, model: str, route: str) -> None:
    settings = get_settings()
    set_engine(settings.database_url)
    await init_db()

    _, key_attr, _, (in_price, out_price) = PROVIDERS[provider]
    key_value = getattr(settings, key_attr) if key_attr else "offline"

    async with get_session_factory()() as session:
        price = (
            await session.execute(
                select(ModelPrice).where(
                    ModelPrice.provider == provider, ModelPrice.model == model
                )
            )
        ).scalar_one_or_none()
        if price is None:
            session.add(
                ModelPrice(
                    provider=provider,
                    model=model,
                    input_price_per_1k=Decimal(in_price),
                    output_price_per_1k=Decimal(out_price),
                )
            )
            print(f"  price         added {provider}/{model} @ {in_price}/{out_price} per 1k")

        policy = (
            await session.execute(select(RoutePolicy).where(RoutePolicy.match_model == route))
        ).scalar_one_or_none()
        if policy is None:
            policy = RoutePolicy(name=f"{provider} route", match_model=route)
            session.add(policy)

        policy.candidates = [{"provider": provider, "model": model}]
        policy.fallback = [{"provider": "mock-alt", "model": "mock-ok"}]
        policy.strategy = "static"
        policy.enabled = True

        await session.commit()

    print(f"  route         {route} -> {provider}/{model}  (fallback: mock-alt/mock-ok)")
    if key_attr and not key_value:
        print(f"  !! {key_attr} is empty — set it in .env or every call will fail with 401/连接错误")
        print("     (until then the route falls back to mock-alt, so calls still return 200)")
        sys.exit(3)


async def _verify(route: str, provider: str) -> None:
    settings = get_settings()
    base = "http://127.0.0.1:8080"
    print(f"  verify        POST {base}/v1/chat/completions (model={route})")

    # reuse the first key in the database by minting a fresh one through the admin API
    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        projects = await client.get(
            f"{base}/admin/v1/projects", headers={"X-Admin-Token": settings.admin_token}
        )
        if projects.status_code != 200:
            print("  !! gateway not reachable on :8080 — start it with ./scripts/up.sh")
            sys.exit(4)
        project_id = projects.json()["items"][0]["id"]
        key = await client.post(
            f"{base}/admin/v1/keys?project_id={project_id}&name=verify",
            headers={"X-Admin-Token": settings.admin_token},
        )
        raw = key.json()["api_key"]

        resp = await client.post(
            f"{base}/v1/chat/completions",
            headers={"Authorization": f"Bearer {raw}"},
            json={"model": route, "messages": [{"role": "user", "content": "reply with OK"}]},
        )
        if resp.status_code != 200:
            print(f"  !! call failed: {resp.status_code} {resp.text[:200]}")
            sys.exit(5)
        meta = resp.json()["x_aigate"]
        print(
            f"  ok            provider={meta['provider']} model={meta['model_used']} "
            f"degraded={meta['degraded']} cost=¥{meta['cost_cny']}"
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description="switch a route to a real provider")
    parser.add_argument("provider", nargs="?", choices=sorted(PROVIDERS), help="vendor name")
    parser.add_argument("model", nargs="?", help="model id, e.g. qwen-plus")
    parser.add_argument("--route", default="chat-default", help="virtual model to repoint")
    parser.add_argument("--verify", action="store_true", help="send one real request through the gateway")
    parser.add_argument("--list", action="store_true", help="show known providers")
    args = parser.parse_args()

    if args.list or not args.provider:
        print("known providers:")
        for name, (_, key_attr, default_model, _) in PROVIDERS.items():
            env = key_attr.upper() if key_attr else "— (offline)"
            print(f"  {name:<10} default model: {default_model:<14} key env: {env}")
        return

    model = args.model or PROVIDERS[args.provider][2]
    print(f"configuring ({args.provider}, {model})")
    await _apply(args.provider, model, args.route)

    if args.verify:
        await _verify(args.route, args.provider)
    else:
        print("  next          ./scripts/up.sh  然后 curl 一条 /v1/chat/completions 验证")


if __name__ == "__main__":
    asyncio.run(main())