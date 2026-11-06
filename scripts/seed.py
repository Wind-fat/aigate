"""Seed demo data — idempotent, prints the API key once.

Usage:  uv run python scripts/seed.py
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import select

from aigate.config import get_settings
from aigate.db import get_session_factory, init_db, set_engine
from aigate.models import ApiKey, ModelPrice, Project, RoutePolicy, Team
from aigate.security import generate_api_key

DEMO_KEY_NAME = "demo-key"

PRICES = [
    # provider, model, input/1k, output/1k, context
    ("mock", "mock-ok", "0.001", "0.002", 8192),
    ("mock-alt", "mock-ok", "0.001", "0.002", 8192),
    ("dashscope", "qwen-plus", "0.0008", "0.002", 131072),
    ("dashscope", "qwen-flash", "0.0003", "0.0006", 131072),
    ("deepseek", "deepseek-chat", "0.001", "0.002", 65536),
    ("deepseek", "deepseek-reasoner", "0.004", "0.016", 65536),
    # OpenAI：单价按公开价换算成「每 1k token 人民币」（1 USD ≈ 7.2 CNY），
    # 只作为初始值——真实结算以控制台「模型路由」里的单价为准。
    ("openai", "gpt-4o-mini", "0.0011", "0.0043", 128000),
    ("openai", "gpt-4o", "0.0180", "0.0720", 128000),
    ("openai", "gpt-4.1-mini", "0.0029", "0.0115", 1000000),
    ("openai", "o4-mini", "0.0079", "0.0317", 200000),
]

POLICIES = [
    {
        "name": "default echo",
        "match_model": "chat-default",
        "candidates": [{"provider": "mock", "model": "mock-ok"}],
        "fallback": [],
        "strategy": "static",
    },
    {
        # Streaming demo target.
        "name": "stream echo",
        "match_model": "chat-stream",
        "candidates": [{"provider": "mock", "model": "mock-ok"}],
        "fallback": [],
        "strategy": "static",
    },
    {
        "name": "qwen with fallback",
        "match_model": "qwen-plus",
        "candidates": [{"provider": "dashscope", "model": "qwen-plus"}],
        "fallback": [{"provider": "mock-alt", "model": "mock-ok"}],
        "strategy": "static",
    },
    {
        # 便宜档：主用 qwen-flash，抖了退 deepseek-chat。两份都在供应商页可管理。
        "name": "cheap tier",
        "match_model": "chat-cheap",
        "candidates": [{"provider": "dashscope", "model": "qwen-flash"}],
        "fallback": [{"provider": "deepseek", "model": "deepseek-chat"}],
        "strategy": "cost_first",
    },
    {
        "name": "deepseek direct",
        "match_model": "chat-deepseek",
        "candidates": [{"provider": "deepseek", "model": "deepseek-chat"}],
        "fallback": [{"provider": "mock-alt", "model": "mock-ok"}],
        "strategy": "static",
    },
    {
        # Deliberately broken primary: proves fallback + circuit breaker with no real vendor.
        "name": "failover demo",
        "match_model": "failover-demo",
        "candidates": [{"provider": "mock", "model": "fail500-primary"}],
        "fallback": [{"provider": "mock-alt", "model": "mock-ok"}],
        "strategy": "static",
    },
    {
        # Fails *after* the first token: the unrecoverable case, demoed on purpose.
        "name": "mid-stream interruption demo",
        "match_model": "stream-interrupt",
        "candidates": [{"provider": "mock", "model": "streamfail-x"}],
        "fallback": [],
        "strategy": "static",
    },
]


async def main() -> None:
    settings = get_settings()
    set_engine(settings.database_url)
    await init_db()

    async with get_session_factory()() as session:
        team = (await session.execute(select(Team).where(Team.slug == "acme"))).scalar_one_or_none()
        if team is None:
            team = Team(name="Acme Corp", slug="acme")
            session.add(team)
            await session.flush()

        project = (
            await session.execute(
                select(Project).where(Project.team_id == team.id, Project.slug == "support-bot")
            )
        ).scalar_one_or_none()
        if project is None:
            project = Project(
                team_id=team.id,
                name="Support Bot",
                slug="support-bot",
                monthly_budget_cny=Decimal("200.00"),
                rpm_limit=60,
            )
            session.add(project)
            await session.flush()

        for provider, model, inp, outp, ctx in PRICES:
            exists = (
                await session.execute(
                    select(ModelPrice).where(ModelPrice.provider == provider, ModelPrice.model == model)
                )
            ).scalar_one_or_none()
            if exists is None:
                session.add(
                    ModelPrice(
                        provider=provider,
                        model=model,
                        input_price_per_1k=Decimal(inp),
                        output_price_per_1k=Decimal(outp),
                        context_window=ctx,
                    )
                )

        for p in POLICIES:
            exists = (
                await session.execute(select(RoutePolicy).where(RoutePolicy.match_model == p["match_model"]))
            ).scalar_one_or_none()
            if exists is None:
                session.add(RoutePolicy(**p))

        existing_key = (
            await session.execute(
                select(ApiKey).where(ApiKey.project_id == project.id, ApiKey.name == DEMO_KEY_NAME)
            )
        ).scalar_one_or_none()

        raw_key = None
        if existing_key is None:
            raw_key, prefix, digest = generate_api_key()
            session.add(
                ApiKey(
                    project_id=project.id,
                    name=DEMO_KEY_NAME,
                    prefix=prefix,
                    key_hash=digest,
                    allowed_models=[],  # empty = all enabled policies
                )
            )

        await session.commit()

    print("=" * 72)
    print("seed done")
    print(f"  team     : {team.slug}   project: {project.slug}")
    print(f"  budget   : ¥{project.monthly_budget_cny}/month, {project.rpm_limit} rpm")
    if raw_key:
        print(f"  API KEY  : {raw_key}")
        print("             ^ shown once, only the sha256 digest is stored")
    else:
        print("  API KEY  : already exists (delete aigate.db to rotate)")
    print("  models   : chat-default / chat-stream / qwen-plus / failover-demo / stream-interrupt")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
