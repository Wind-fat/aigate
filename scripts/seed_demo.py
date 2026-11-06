"""Generate 7 days of demo traffic so the console has something to show.

This is **synthetic** data: it exists so a screenshot of the dashboard is not empty, not
to claim real business volume. Numbers are deterministic (fixed seed) so screenshots are
reproducible.

It writes a `request_traces` row for every `usage_records` row. Without that, clicking a
row in 调用明细 lands on "trace not found" — demo data you cannot drill into is worse than
no demo data, because it teaches the reviewer that the feature is broken.

Re-running replaces the previous demo rows (identified by the `req_demo_` prefix) instead
of appending: two runs of a seed script should not double the chart.

Usage:  uv run python scripts/seed_demo.py [--days 7] [--per-day 12]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import random
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import delete, select

from aigate.config import get_settings
from aigate.db import get_session_factory, init_db, set_engine
from aigate.models import (
    ApiKey,
    ModelPrice,
    Project,
    RequestTrace,
    Team,
    UsageRecord,
    utcnow,
)

MODELS = [
    ("mock", "mock-ok", "chat-default", 0.62),
    ("mock", "mock-ok", "chat-stream", 0.18),
    ("dashscope", "qwen-plus", "qwen-plus", 0.14),
    ("mock-alt", "mock-ok", "failover-demo", 0.06),
]

PROMPTS = [
    "帮我把这段客服对话总结成三条要点",
    "这个月的退款政策改了吗？",
    "用一句话解释什么是 LLM 网关",
    "把工单 #4821 的优先级改成 P1",
    "客户说发票金额不对，怎么回复",
    "整理一下本周的线上问题清单",
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

        await session.execute(delete(UsageRecord).where(UsageRecord.request_id.like("req_demo_%")))
        await session.execute(delete(RequestTrace).where(RequestTrace.request_id.like("req_demo_%")))

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
                request_id = f"req_demo_{day_offset:02d}_{written:04d}"
                cached = status_ == "cached"

                latency = rng.randint(8, 40) if cached else rng.randint(180, 2400)
                upstream = 0 if cached else max(60, latency - rng.randint(20, 60))

                stage_timings = {
                    "scope_ms": 0,
                    "quota_ms": rng.randint(0, 1),
                    "budget_ms": rng.randint(1, 3),
                    "guard_ms": 1 if rng.random() < 0.2 else 0,
                    "cache_ms": rng.randint(3, 9) if cached else rng.randint(2, 6),
                    "upstream_ms": upstream,
                    "persist_ms": rng.randint(1, 3),
                }

                if status_ == "error":
                    route_path = [
                        {
                            "attempt": 1,
                            "provider": provider,
                            "model": model,
                            "ok": False,
                            "error": "upstream_5xx",
                            "latency_ms": upstream,
                        }
                    ]
                    http_status = 502
                elif status_ == "fallback":
                    route_path = [
                        {
                            "attempt": 1,
                            "provider": "mock",
                            "model": "fail500-primary",
                            "ok": False,
                            "error": "upstream_5xx",
                            "latency_ms": max(40, upstream // 3),
                        },
                        {
                            "attempt": 2,
                            "provider": provider,
                            "model": model,
                            "ok": True,
                            "latency_ms": upstream,
                        },
                    ]
                    http_status = 200
                elif cached:
                    route_path = []  # nothing hit the upstream: an empty path is the fact
                    http_status = 200
                else:
                    route_path = [
                        {
                            "attempt": 1,
                            "provider": provider,
                            "model": model,
                            "ok": True,
                            "latency_ms": upstream,
                        }
                    ]
                    http_status = 200

                prompt = rng.choice(PROMPTS)

                session.add(
                    UsageRecord(
                        request_id=request_id,
                        team_id=team.id,
                        project_id=project.id,
                        api_key_id=key.id,
                        provider=provider,
                        model_requested=requested,
                        model_used=model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cost_cny=cost,
                        latency_ms=latency,
                        ttft_ms=None if cached else rng.randint(90, 700),
                        status=status_,
                        error_code=error,
                        cache_hit="L1" if cached and rng.random() < 0.6 else ("L2" if cached else None),
                        degraded=status_ == "fallback",
                        created_at=created,
                    )
                )
                session.add(
                    RequestTrace(
                        request_id=request_id,
                        project_id=project.id,
                        model_requested=requested,
                        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                        prompt_preview=prompt,
                        pii_masked=0,
                        stage_timings=stage_timings,
                        route_path=route_path,
                        http_status=http_status,
                        created_at=created,
                    )
                )
                written += 1

        await session.commit()

    print(f"demo traffic written: {written} usage rows + {written} traces over {days} days (synthetic)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--per-day", type=int, default=12)
    args = parser.parse_args()
    asyncio.run(main(args.days, args.per_day))
