"""What one PostgreSQL connection actually costs, measured from the client side.

The gateway's PG-vs-SQLite delta was reported as "~90 ms per request" but never located.
This probe separates the four candidates the review asked about, without the gateway in the
way (so a number here cannot be blamed on the app's own scheduling):

  (a) connection acquisition  -> `asyncpg.connect()` and a saturated SQLAlchemy pool
  (b) round-trip count        -> warm `SELECT 1` p50, i.e. the price of one round trip
  (c) asyncpg/plan overhead   -> first execution of each statement on a *fresh* connection
                                 vs the same statements on a warm one (PostgreSQL plans at
                                 BIND time, so this is the "SELECT was 0.03 ms but bind was
                                 5 ms" effect visible in `log_min_duration_statement=0`)
  (d) client side             -> not measurable here on purpose; the client work is in
                                 scripts/perf_stages.py, where `client_ms - gateway_total`
                                 is the residue the server never sees.

Usage:
    .venv/Scripts/python.exe scripts/perf_pg_probe.py \
        --dsn postgresql://postgres:@127.0.0.1:50242/aigate_perf --rounds 5
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import asyncpg
from sqlalchemy.ext.asyncio import create_async_engine

# App-shaped statements: the ones the chat path runs, with real parameter types. They are
# hand-written SQL on purpose — importing the ORM models would let SQLAlchemy's own caching
# answer the question instead of PostgreSQL.
STMTS: list[tuple[str, str]] = [
    ("api_key", "SELECT id, project_id, status, expires_at FROM api_keys "
                "WHERE key_hash=$1 AND status='active'"),
    ("project", "SELECT id, team_id, status FROM projects WHERE id=$1"),
    ("team", "SELECT id, slug, rpm_limit FROM teams WHERE id=$1"),
    ("route", "SELECT id, strategy, candidates, fallback, shadow FROM route_policies WHERE match_model=$1"),
    ("price1", "SELECT id, input_price_per_1k FROM model_prices WHERE provider=$1 AND model=$2 AND enabled"),
    ("price2", "SELECT id, input_price_per_1k FROM model_prices "
               "WHERE provider=$1 AND model=$2 AND enabled LIMIT 1"),
    ("providers", "SELECT id, name, enabled FROM provider_configs"),
    ("trace_exists", "SELECT request_id FROM request_traces WHERE request_id = ANY($1::varchar[])"),
]


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))] if ordered else 0.0


async def connect_cost(dsn: str, rounds: int) -> list[float]:
    out = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        conn = await asyncpg.connect(dsn)
        out.append((time.perf_counter() - t0) * 1000)
        await conn.close()
    return out


async def warm_rtt(dsn: str, rounds: int = 50) -> list[float]:
    conn = await asyncpg.connect(dsn)
    out = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        await conn.fetchval("select 1")
        out.append((time.perf_counter() - t0) * 1000)
    await conn.close()
    return out


async def first_plan_cost(dsn: str) -> tuple[dict[str, tuple[float, float]], float, float]:
    """First execution of each statement on a fresh connection vs a second pass.

    `asyncpg` prepares a statement on first use (Parse/Describe) and PostgreSQL plans it at
    Bind time on every execution. So "first" pays prepare + plan, "second" pays plan only.
    """
    conn = await asyncpg.connect(dsn)
    first: dict[str, float] = {}
    second: dict[str, float] = {}
    for name, sql in STMTS:
        t0 = time.perf_counter()
        try:
            await conn.fetch(sql, *_args(name))
        except Exception:  # noqa: BLE001 - a missing row/type must not stop the probe
            await conn.fetch(sql, *_args(name))
        first[name] = (time.perf_counter() - t0) * 1000
    for name, sql in STMTS:
        t0 = time.perf_counter()
        await conn.fetch(sql, *_args(name))
        second[name] = (time.perf_counter() - t0) * 1000
    await conn.close()
    return {k: (first[k], second[k]) for k in first}, sum(first.values()), sum(second.values())


def _args(name: str) -> tuple:
    return {
        "api_key": ("x",),
        "project": ("x",),
        "team": ("x",),
        "route": ("x",),
        "price1": ("x", "y"),
        "price2": ("x", "y"),
        "providers": (),
        "trace_exists": (["x"],),
    }[name]


async def pool_pressure(dsn: str, holders: int = 8) -> list[float]:
    """How long the *second* session of a request waits when 8 requests each hold one.

    Mirrors the chat path exactly: the request session is held for the whole request, and
    `_maybe_shadow` asks for a second connection before the response is written. With
    pool_size=5 + max_overflow=10 there are 15 slots, so 8 holders fit — but 8 holders x 2
    = 16 does not, and the 16th acquisition is where the wait appears.
    """
    url = dsn.replace("postgresql://", "postgresql+asyncpg://")
    engine = create_async_engine(url, pool_size=5, max_overflow=10, pool_timeout=1.0)
    waits: list[float] = []

    async def one() -> None:
        async with engine.connect() as held:  # the request session
            await held.exec_driver_sql("select 1")
            t0 = time.perf_counter()
            async with engine.connect() as second:  # the shadow session
                await second.exec_driver_sql("select 1")
            waits.append((time.perf_counter() - t0) * 1000)

    try:
        await asyncio.gather(*(one() for _ in range(holders)))
    finally:
        await engine.dispose()
    return waits


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--rounds", type=int, default=5)
    args = ap.parse_args()

    cs = await connect_cost(args.dsn, args.rounds)
    print(f"connect (asyncpg.connect, n={len(cs)}): "
          f"p50 {statistics.median(cs):.1f} ms, max {max(cs):.1f} ms  "
          f"{[f'{c:.1f}' for c in cs]}")

    rtt = await warm_rtt(args.dsn)
    print(f"warm statement round trip (select 1, n={len(rtt)}): "
          f"p50 {statistics.median(rtt):.3f} ms, p95 {_pct(rtt, 0.95):.3f} ms")

    per_stmt, first_total, second_total = await first_plan_cost(args.dsn)
    print(f"\nfirst pass on a fresh connection: {first_total:.1f} ms | second pass: {second_total:.1f} ms")
    print("| statement | first (ms) | second (ms) |")
    print("|---|---|---|")
    for name, (a, b) in per_stmt.items():
        print(f"| {name} | {a:.3f} | {b:.3f} |")

    waits = await pool_pressure(args.dsn)
    print(f"\npool pressure (8 holders + 1 extra session each, pool 5+10): "
          f"second-session wait p50 {statistics.median(waits):.1f} ms, max {max(waits):.1f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))