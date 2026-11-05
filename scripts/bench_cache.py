"""Cost A/B for the semantic cache — the number that goes on the résumé.

Same question bank, three passes against a live gateway:
  pass 1  cold     -> every call is a miss (this is the "no cache" baseline)
  pass 2  identical-> L1 hits
  pass 3  paraphrased -> L2 hits

The script prints a markdown table you can paste into the README. It measures the
gateway as a client would; it does not read internal counters, so the numbers cannot
drift from what a user actually experiences.

Usage:  uv run python scripts/bench_cache.py
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

PORT = 8098
BASE = f"http://127.0.0.1:{PORT}"
# Dedicated database: the benchmark must never contend with a dev server's db file.
BENCH_DB = "bench.db"
BENCH_ENV = {**os.environ, "AIGATE_DATABASE_URL": f"sqlite+aiosqlite:///./{BENCH_DB}"}

QUESTIONS = [
    "如何申请年假",
    "报销发票需要哪些材料",
    "试用期是多久",
    "公司有哪些培训课程",
    "如何重置办公系统密码",
    "出差住宿标准是多少",
    "远程办公需要谁审批",
    "绩效评估多久一次",
    "如何申请调岗",
    "社保基数怎么确定",
    "公积金提取流程",
    "采购审批需要几级",
    "合同盖章要走什么流程",
    "如何申请公司邮箱",
    "设备损坏怎么报修",
    "员工推荐奖励怎么算",
    "加班费如何计算",
    "如何查看工资条",
    "入职需要准备哪些材料",
    "离职手续有哪些步骤",
]

VARIANTS = ["{}？", "请问{}", "{} 谢谢"]


@dataclass
class Pass:
    name: str
    costs: list[float] = field(default_factory=list)
    latencies: list[float] = field(default_factory=list)
    hits: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return round(sum(self.costs), 6)

    @property
    def avg_ms(self) -> float:
        return round(sum(self.latencies) / len(self.latencies), 1) if self.latencies else 0.0

    @property
    def hit_rate(self) -> float:
        hits = sum(1 for h in self.hits if h)
        return round(hits / len(self.hits) * 100, 1) if self.hits else 0.0


async def _ask(client: httpx.AsyncClient, key: str, text: str, out: Pass) -> None:
    started = time.perf_counter()
    resp = await client.post(
        f"{BASE}/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": "chat-default",
            "messages": [{"role": "user", "content": text}],
            "temperature": 0,
        },
    )
    out.latencies.append((time.perf_counter() - started) * 1000)
    resp.raise_for_status()
    meta = resp.json()["x_aigate"]
    out.costs.append(float(meta["cost_cny"]))
    out.hits.append(meta.get("cache_hit") or "")


async def main() -> None:
    print("== reset + seed ==")
    if os.path.exists(BENCH_DB):
        os.remove(BENCH_DB)

    seed = subprocess.run(
        [sys.executable, "scripts/seed.py"],
        capture_output=True,
        text=True,
        check=True,
        env=BENCH_ENV,
    )
    match = re.search(r"API KEY\s*:\s*(\S+)", seed.stdout)
    if not match:
        print(seed.stdout, seed.stderr)
        raise SystemExit("could not read demo key from seed output")
    key = match.group(1)

    print(f"== starting gateway on :{PORT} ==")
    log_path = "bench_server.log"
    server_log = open(log_path, "w", encoding="utf-8")
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "aigate.main:app",
            "--port",
            str(PORT),
            "--log-level",
            "info",
        ],
        stdout=server_log,
        stderr=subprocess.STDOUT,
        env=BENCH_ENV,
    )
    try:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            for _ in range(80):
                try:
                    if (await client.get(f"{BASE}/health")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.3)
            else:
                server_log.flush()
                print(Path(log_path).read_text(encoding="utf-8")[-2000:])
                raise SystemExit("gateway did not become ready")

            cold, same, para = Pass("cold (no cache)"), Pass("identical (L1)"), Pass("paraphrased (L2)")

            print(f"== pass 1: {len(QUESTIONS)} cold calls ==")
            for q in QUESTIONS:
                await _ask(client, key, q, cold)

            print("== pass 2: same questions ==")
            for q in QUESTIONS:
                await _ask(client, key, q, same)

            print("== pass 3: paraphrased ==")
            for q, v in zip(
                QUESTIONS, [VARIANTS[i % len(VARIANTS)] for i in range(len(QUESTIONS))], strict=False
            ):
                await _ask(client, key, v.format(q), para)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            server.kill()

    calls = len(QUESTIONS) * 3
    no_cache_cost = round(cold.total * 3, 6)
    with_cache_cost = round(cold.total + same.total + para.total, 6)
    saving = (1 - with_cache_cost / no_cache_cost) * 100 if no_cache_cost else 0.0

    print()
    print("| pass | calls | hit rate | total cost (CNY) | avg latency |")
    print("|---|---|---|---|---|")
    for p in (cold, same, para):
        print(f"| {p.name} | {len(p.costs)} | {p.hit_rate}% | {p.total:.6f} | {p.avg_ms:.1f} ms |")
    print()
    print(f"calls                : {calls}")
    print(f"cost if no cache     : {no_cache_cost:.6f} CNY  (every call a miss)")
    print(f"cost with cache      : {with_cache_cost:.6f} CNY")
    print(f"cost reduction       : {saving:.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
