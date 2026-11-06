"""质量-成本曲线：用真实调用数据回答「换更便宜的模型，值不值」。

这不是一份 benchmark。它只做两件事：

1. 把 `usage_records` 按模型聚合成一张表：调用量、成功率、p95、每次成本、每千次成本。
2. 把 `shadow_runs`（影子流量）按模型聚合：跑了多少次、成功率、与主回答的平均相似度。

**为什么不是一条漂亮的曲线**：只有每个模型都跑过同一批 prompt，才能画「准确率 vs 成本」。
做过影子流量或评测的模型才有质量轴，其余只有成本轴——表里 `sim` 为 `—` 就是没测过，
不猜、不用行业平均值填充。

    uv run python scripts/quality_cost.py --days 30
    uv run python scripts/quality_cost.py --days 30 --out docs/quality-cost.md
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from aigate.config import get_settings
from aigate.db import get_session_factory, set_engine
from aigate.models import RoutePolicy, ShadowRun, UsageRecord, utcnow

ROOT = Path(__file__).resolve().parent.parent

HEADER = "| provider | model | 调用 | 成功率 | p95 | 每次成本 | 每千次成本 | 均 tokens | sim | 影子 |"
DIVIDER = "|---|---|---|---|---|---|---|---|---|---|"


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = 0.95 * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return int(round(ordered[low] + (ordered[high] - ordered[low]) * (pos - low)))


async def build(days: int) -> str:
    settings = get_settings()
    set_engine(settings.database_url)
    since = utcnow() - timedelta(days=days)

    async with get_session_factory()() as session:
        rows = (
            (await session.execute(select(UsageRecord).where(UsageRecord.created_at >= since)))
            .scalars()
            .all()
        )
        shadows = (
            (await session.execute(select(ShadowRun).where(ShadowRun.created_at >= since)))
            .scalars()
            .all()
        )
        routes = (await session.execute(select(RoutePolicy))).scalars().all()

    by_model: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"n": 0, "ok": 0, "cost": Decimal("0"), "lat": [], "tok": 0}
    )
    for r in rows:
        b = by_model[(r.provider, r.model_used)]
        b["n"] += 1
        b["ok"] += 1 if r.status in ("ok", "fallback", "cached") else 0
        b["cost"] += r.cost_cny or Decimal("0")
        b["lat"].append(r.latency_ms)
        b["tok"] += (r.input_tokens or 0) + (r.output_tokens or 0)

    shadow_by_model: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"n": 0, "ok": 0, "sim": []}
    )
    for s in shadows:
        b = shadow_by_model[(s.provider, s.model)]
        b["n"] += 1
        b["ok"] += 1 if s.ok else 0
        if s.similarity is not None:
            b["sim"].append(s.similarity)

    # 配了影子但还没跑出数据的候选也要能看出来：这是「没测过」，不是「没配置」
    configured: dict[str, int] = defaultdict(int)
    for policy in routes:
        for cand in policy.shadow or []:
            configured[f"{cand.get('provider')}/{cand.get('model')}"] += 1

    lines: list[str] = []
    lines.append(f"# 质量-成本对照（近 {days} 天真实调用）")
    lines.append("")
    lines.append("> 由 `scripts/quality_cost.py` 生成。数字来自 `usage_records` 与 `shadow_runs`。")
    lines.append("> `sim` 是影子流量与主回答的字符级相似度；`—` 表示这个模型还没有对照数据。")
    lines.append("")
    lines.append(HEADER)
    lines.append(DIVIDER)

    def cost_per_call(bucket: dict) -> float:
        return float(bucket["cost"]) / bucket["n"] if bucket["n"] else 0.0

    for (provider, model), b in sorted(by_model.items(), key=lambda kv: cost_per_call(kv[1])):
        shadow = shadow_by_model.get((provider, model))
        sim_text = f"{statistics.mean(shadow['sim']):.2f}" if shadow and shadow["sim"] else "—"
        shadow_text = str(shadow["n"]) if shadow else "0"
        row = (
            "| {p} | {m} | {n} | {ok:.1f}% | {p95} ms | ¥{c:.6f} | ¥{c1k:.2f} | {tok} | "
            "{sim} | {sh} |"
        )
        lines.append(
            row.format(
                p=provider,
                m=model,
                n=b["n"],
                ok=b["ok"] / b["n"] * 100 if b["n"] else 0,
                p95=_p95(b["lat"]),
                c=cost_per_call(b),
                c1k=cost_per_call(b) * 1000,
                tok=round(b["tok"] / b["n"]) if b["n"] else 0,
                sim=sim_text,
                sh=shadow_text,
            )
        )

    # 只在配置了影子、却一次都没跑成的模型上提示——这条比"多一个空行"有用
    missing: list[str] = []
    for pair in configured:
        provider_name, _, model_name = pair.partition("/")
        if not shadow_by_model.get((provider_name, model_name)):
            missing.append(pair)
    if missing:
        lines.append("")
        lines.append(f"配置了影子但还没有数据：{', '.join(sorted(missing))}")

    lines.append("")
    lines.append("## 怎么读")
    lines.append("")
    lines.append("- **每千次成本** 是唯一能横向比的数字；单价不同、token 长度不同，比单价没意义。")
    lines.append("- **成功率** 只统计落库的调用（限流与预算拒绝不落库，见 runbook 场景 0）。")
    lines.append("- **sim 低** 不代表新模型差，只代表两个模型说法差得多——要看原文，见调用链页。")
    lines.append("- 想给某个模型补质量轴：在「模型路由」里把它加进影子链，跑几天再看这张表。")
    return "\n".join(lines) + "\n"


async def main(days: int, out: str | None) -> None:
    report = await build(days)
    if out:
        path = ROOT / out
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
        print(f"written: {out}")
    print(report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    asyncio.run(main(args.days, args.out))