"""Generate 7 days of demo traffic so the console has something to show.

This is **synthetic** data: it exists to make screenshots and the README honest about
what a week of usage looks like, not to claim real business volume. Every row it writes
carries `usage_record.error_code=None` and a realistic mix of statuses; the numbers are
deterministic (fixed seed) so screenshots stay reproducible.

Usage:  uv run python scripts/seed_demo.py [--days 7] [--per-day 12]
"""

from __future__ import annotations

import argparse
import asyncio
import random
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from aigate.config import get_settings
from aigate.db import get_session_factory, init_db, set_engine
from aigate.models import ApiKey, ModelPrice, Project, Team, UsageRecord, utcnow

MODELS = [
    ("mock", "mock-ok", "chat-default", 0.62),
    ("mock", "mock-ok", "chat-stream", 0.18),
    ("dashscope", "qwen-plus", "qwen-plus", 0.14),
    ("mock-alt", "mock-ok", "failover-demo", 0.06),
]


async def main(days: int, per_day: int) -> None:
    settings = get_settings()
    set_engine(settings.database_url)
    await init_db()

    rng = random.Random(20261105)  # deterministic: screenshots must be reproducible
    now = utcnow()

    async with get_session_factory()() as session:
        team = (await session.execute(select(Team).where(Team.slug == "acme"))).scalar_one_or_none()
        project = (
            await session.execute(select(Project).where(Project.slug == "support-bot"))
        ).scalar_one_or_none()
        key = (
            (
                await session.execute(select(ApiKey).where(ApiKey.project_id == project.id).limit(1))
            ).scalar_one_or_none()
            if project
            else None
        )

        if team is None or project is None or key is None:
            raise SystemExit("run scripts/seed.py first")

        prices = {
            (p.provider, p.model): p for p in (await session.execute(select(ModelPrice))).scalars().all()
        }

        written = 0
        for day_offset in range(days - 1, -1, -1):
            # Weekday shape: weekends are quiet, which is what makes a chart look real.
            weekday = (now - timedelta(days=day_offset)).weekday()
            count = max(2, int(per_day * (0.35 if weekday >= 5 else 1.0) * rng.uniform(0.7, 1.3)))

            for _ in range(count):
                provider, model, requested, _weight = rng.choices(
                    MODELS, weights=[m[3] for m in MODELS], k=1
                )[0]
                price = prices.get((provider, model))
                input_tokens = rng.randint(18, 320)
                output_tokens = rng.randint(20, 480)
                cost = (
                    (Decimal(input_tokens) / 1000) * price.input_price_per_1k
                    + (Decimal(output_tokens) / 1000) * price.output_price_per_1k
                    if price
                    else Decimal("0")
                ).quantize(Decimal("0.000001"))

                roll = rng.random()
                if roll < 0.04:
                    status_, error, cost = "error", "upstream_5xx", Decimal("0")
                elif roll < 0.30:
                    status_, error, cost = "cached", None, Decimal("0")
                elif requested == "failover-demo":
                    status_, error = "fallback", None
                else:
                    status_, error = "ok", None

                created = now - timedelta(
                    days=day_offset,
                    hours=rng.randint(0, 9),
                    minutes=rng.randint(0, 59),
                )

                session.add(
                    UsageRecord(
                        request_id=f"req_demo_{day_offset:02d}_{written:04d}",
                        team_id=team.id,
                        project_id=project.id,
                        api_key_id=key.id,
                        provider=provider,
                        model_requested=requested,
                        model_used=model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cost_cny=cost,
                        latency_ms=rng.randint(180, 2400) if status_ != "cached" else rng.randint(8, 40),
                        ttft_ms=rng.randint(90, 700) if status_ != "cached" else None,
                        status=status_,
                        error_code=error,
                        cache_hit="L1"
                        if status_ == "cached" and rng.random() < 0.6
                        else ("L2" if status_ == "cached" else None),
                        degraded=status_ == "fallback",
                        created_at=created,
                    )
                )
                written += 1

        await session.commit()

    print(f"demo traffic written: {written} usage rows over {days} days (synthetic)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--per-day", type=int, default=12)
    args = parser.parse_args()
    asyncio.run(main(args.days, args.per_day))
