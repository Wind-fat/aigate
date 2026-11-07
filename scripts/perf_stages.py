"""Per-stage latency of `POST /v1/chat/completions`, SQLite vs real PostgreSQL.

Why this script exists: the load test (`scripts/loadtest.py`) reports one number per
request (p50/p95). That number cannot say *which* stage got slower when the database
changes — auth, budget SUM, route lookup, price lookup, the provider call, the shadow
probe, the metering hand-off — so the PG-vs-SQLite delta was measured but not located.

Method, and why it is shaped like this:

* the gateway reports its own stage timings in `x_aigate.latency_ms` (auth / scope /
  quota / budget / guard / route / provider_cfg / upstream / price / shadow / persist).
  Those clocks are taken inside the handler, so they exclude the socket, the middleware
  and — importantly — the dependency teardown that closes the request's DB session.
* `client_ms` is measured around the whole HTTP exchange on a keep-alive socket, so
  `client_ms - gateway_total` is the residue that the handler never sees (serialization,
  middleware, connection teardown incl. the implicit ROLLBACK).
* requests are sent **sequentially** by default: with anything else the schedule, not the
  stage, decides the number. `--concurrency` exists to reproduce the queueing level, and
  then the per-stage numbers are honest only as "what the caller waited for", not "what
  the stage cost".

Usage (server is started and stopped by this script, like scripts/loadtest.py):

    .venv/Scripts/python.exe scripts/perf_stages.py --label sqlite \
        --db-url sqlite+aiosqlite:///./perf-stages.db --n 20

    .venv/Scripts/python.exe scripts/perf_stages.py --label pg \
        --db-url postgresql+asyncpg://postgres:@127.0.0.1:50242/aigate_perf --n 20

    # same thing at N=8 concurrent clients, 40 requests per client
    .venv/Scripts/python.exe scripts/perf_stages.py --label pg --db-url ... \
        --n 0 --concurrency 8 --concurrent-requests 40

`--json path.json` dumps the raw samples so a table in the docs can be regenerated
without a re-run. No new runtime dependency: stdlib + the project's own deps.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from loadtest import (  # noqa: E402 - sibling script, single source of truth for the rig
    MODEL,
    _configure_env,
    _connect,
    _exchange,
    _seed,
    _start_server,
    _wait_ready,
)

# Stage keys in the order the request pays them. `upstream` contains `provider_cfg`.
STAGES = [
    "auth_ms",
    "scope_ms",
    "quota_ms",
    "budget_ms",
    "guard_ms",
    "cache_ms",
    "route_ms",
    "provider_cfg_ms",
    "upstream_ms",
    "price_ms",
    "shadow_ms",
    "persist_ms",
]


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def _row(name: str, values: list[float]) -> str:
    if not values:
        return f"| {name} | - | - | - |"
    return (
        f"| {name} | {statistics.median(values):.1f} | {_percentile(values, 0.95):.1f} "
        f"| {max(values):.1f} |"
    )


def _body() -> bytes:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "perf probe: two plus two?"}],
        "temperature": 0,
    }
    return json.dumps(payload).encode()


async def _one(port: int, key: str, conn, body: bytes) -> dict:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    t0 = time.perf_counter()
    status, _hdrs, raw = await _exchange(conn, "POST", "/v1/chat/completions", body, headers)
    client_ms = (time.perf_counter() - t0) * 1000
    out: dict = {"status": status, "client_ms": client_ms}
    if status == 200:
        data = json.loads(raw)
        timings = data.get("x_aigate", {}).get("latency_ms", {})
        out["timings"] = timings
        out["gateway_total_ms"] = timings.pop("gateway_total", None)
    else:
        out["body"] = raw[:300].decode("utf-8", "replace")
    return out


async def measure_sequential(port: int, key: str, n: int, warmup: int = 3) -> list[dict]:
    conn = await _connect(port)
    body = _body()
    # Warm-up: first request pays connection setup + asyncpg prepared statements +
    # provider_config TTL fill. Those are real, but they are not per-request cost.
    for _ in range(warmup):
        await _one(port, key, conn, body)
    samples = []
    for _ in range(n):
        samples.append(await _one(port, key, conn, body))
    conn[1].close()
    return samples


async def measure_concurrent(port: int, key: str, clients: int, per_client: int) -> list[dict]:
    barrier = asyncio.Barrier(clients)
    body = _body()

    async def worker() -> list[dict]:
        conn = await _connect(port)
        out = []
        await barrier.wait()
        for _ in range(per_client):
            out.append(await _one(port, key, conn, body))
        conn[1].close()
        return out

    results = await asyncio.gather(*(worker() for _ in range(clients)))
    return [s for group in results for s in group]


def report(label: str, samples: list[dict], concurrency: int) -> dict:
    ok = [s for s in samples if s["status"] == 200]
    print(f"\n## {label} (concurrency={concurrency}, n={len(samples)}, 200={len(ok)})")
    if len(ok) != len(samples):
        bad = [s for s in samples if s["status"] != 200]
        print(f"non-200: {len(bad)} e.g. {bad[0]['status']} {bad[0].get('body', '')[:200]}")
    stage_values: dict[str, list[float]] = {}
    for s in ok:
        for k, v in s.get("timings", {}).items():
            stage_values.setdefault(k, []).append(float(v))

    print("| stage | P50 (ms) | P95 (ms) | max (ms) |")
    print("|---|---|---|---|")
    total = 0.0
    for stage in STAGES:
        vals = stage_values.get(stage, [])
        if stage == "upstream_ms":
            # provider_cfg is inside upstream; print it, but do not double count.
            continue
        total += statistics.median(vals) if vals else 0.0
        print(_row(stage, vals))
    client = [s["client_ms"] for s in ok]
    handler = [float(s.get("gateway_total_ms", 0)) for s in ok]
    print(_row("gateway_total (handler)", handler))
    print(_row("client_ms (whole exchange)", client))
    print(
        f"\nsum(median stage) = {total:.1f} ms; "
        f"median(handler) = {statistics.median(handler) if handler else 0:.1f} ms; "
        f"median(client) = {statistics.median(client) if client else 0:.1f} ms; "
        f"residue(client-handler) = "
        f"{(statistics.median(client) - statistics.median(handler)) if client else 0:.1f} ms"
    )
    return {
        "label": label,
        "concurrency": concurrency,
        "n": len(samples),
        "stages": {k: {"p50": statistics.median(v), "p95": _percentile(v, 0.95), "max": max(v)}
                   for k, v in stage_values.items()},
        "gateway_total": {"p50": statistics.median(handler) if handler else 0,
                          "p95": _percentile(handler, 0.95) if handler else 0},
        "client_ms": {"p50": statistics.median(client) if client else 0,
                      "p95": _percentile(client, 0.95) if client else 0},
        "samples": samples,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--label", default="run")
    p.add_argument("--db-url", default="")
    p.add_argument("--n", type=int, default=20, help="sequential requests (0 to skip)")
    p.add_argument(
        "--warmup",
        type=int,
        default=3,
        help="requests before measuring; 0 keeps the first sample cold (pays connection "
        "setup), which is how the '~90 ms per request on PG' number was first produced",
    )
    p.add_argument("--concurrency", type=int, default=0, help="concurrent clients (0 to skip)")
    p.add_argument("--concurrent-requests", type=int, default=40, help="requests per client")
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--json", default="", help="write raw samples here")
    args = p.parse_args()

    db_url = args.db_url or f"sqlite+aiosqlite:///{(ROOT / 'perf-stages.db').as_posix()}"
    env = _configure_env(Path("unused.db"), args.port, url=db_url)
    key = asyncio.run(_seed(Path("unused.db"), env))

    log_path = ROOT / f"perf-server-{args.label}.log"
    proc, handle = _start_server(env, args.port, log_path)
    payload: dict = {"label": args.label, "db_url": db_url.split("://")[0], "runs": []}
    try:
        asyncio.run(_wait_ready(args.port, proc))
        if args.n:
            samples = asyncio.run(measure_sequential(args.port, key, args.n, args.warmup))
            payload["runs"].append(report(args.label, samples, 1))
        if args.concurrency:
            samples = asyncio.run(
                measure_concurrent(args.port, key, args.concurrency, args.concurrent_requests)
            )
            payload["runs"].append(report(f"{args.label} @N={args.concurrency}", samples,
                                          args.concurrency))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001 - shutdown, not measurement
            proc.kill()
        handle.close()

    if args.json:
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    print(f"\nserver log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())