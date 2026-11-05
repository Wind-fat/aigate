"""Load test for the gateway data plane: POST /v1/chat/completions.

Design notes (the parts that matter when reading the numbers):

  * One `httpx.AsyncClient` per run, `trust_env=False` — a system HTTP proxy turns
    every call to 127.0.0.1 into a proxy error on a developer laptop.
  * Latency is measured *client side* around the whole request, so it includes
    connection reuse, JSON encode/decode and the gateway's own DB writes. The
    gateway-reported `latency_ms` block is also collected, which is what lets the
    report show how much of the wall time is proxy overhead rather than upstream.
  * HTTP 429 is counted as *throttled*, not as an error. The gateway rate-limits
    per project (default 60 rpm) and a load test that runs into it has not
    measured throughput, it has measured the limiter. Both rates are reported and
    both can fail the run, for different reasons.
  * `--cached` sends a fixed pool of prompts with `temperature=0` (the only shape
    the semantic cache accepts) so that after the first pass the cache can serve
    the rest. Without it, cache hit rate is legitimately 0 and is not a failure.

Usage:
    uv run python tests/load/loadtest.py --concurrency 20 --requests 200
    uv run python tests/load/loadtest.py --cached --concurrency 10 --requests 100
    uv run python tests/load/loadtest.py --key "$AIGATE_KEY" --json out.json

Exit codes: 0 = thresholds met, 1 = a threshold was exceeded, 2 = bad invocation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_BASE = "http://127.0.0.1:8093"
DEFAULT_MODEL = "chat-default"

# Small, distinct prompts: in --cached mode the pool is what makes hits (and L1
# dedupe) possible; in plain mode each request gets a unique prompt so the cache
# can never mask the real upstream cost.
CACHED_PROMPTS = [
    "how do I reset my password",
    "where is my invoice for last month",
    "what is the refund window",
    "how do I enable two factor auth",
    "can I change my billing email",
]


@dataclass
class Sample:
    """One request, as the client saw it."""

    ok: bool
    status: int | None
    error_code: str | None
    throttled: bool
    latency_ms: float
    gateway_ms: float | None = None
    upstream_ms: float | None = None
    cache_hit: str | None = None
    degraded: bool = False


@dataclass
class Report:
    samples: list[Sample] = field(default_factory=list)
    duration_s: float = 0.0
    warmup: int = 0

    # ---- derived views ----------------------------------------------------
    @property
    def total(self) -> int:
        return len(self.samples)

    @property
    def throttled(self) -> list[Sample]:
        return [s for s in self.samples if s.throttled]

    @property
    def errors(self) -> list[Sample]:
        return [s for s in self.samples if not s.ok and not s.throttled]

    @property
    def oks(self) -> list[Sample]:
        return [s for s in self.samples if s.ok]

    @property
    def rps(self) -> float:
        return self.total / self.duration_s if self.duration_s > 0 else 0.0

    @property
    def error_rate(self) -> float:
        return len(self.errors) / self.total if self.total else 0.0

    @property
    def throttle_rate(self) -> float:
        return len(self.throttled) / self.total if self.total else 0.0

    @property
    def cache_hit_rate(self) -> float:
        hits = sum(1 for s in self.oks if s.cache_hit)
        return hits / len(self.oks) if self.oks else 0.0

    def percentiles(self, values: list[float]) -> dict[str, float]:
        return {f"p{q}": round(pct(values, q), 1) for q in (50, 90, 95, 99)}

    def latency(self) -> dict[str, float]:
        return self.percentiles([s.latency_ms for s in self.samples])

    def latency_ok(self) -> dict[str, float]:
        return self.percentiles([s.latency_ms for s in self.oks])

    def gateway_latency(self) -> dict[str, float]:
        return self.percentiles([s.gateway_ms for s in self.oks if s.gateway_ms is not None])

    def upstream_latency(self) -> dict[str, float]:
        return self.percentiles([s.upstream_ms for s in self.oks if s.upstream_ms is not None])

    def error_breakdown(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for s in self.errors + self.throttled:
            key = s.error_code or (f"http_{s.status}" if s.status else "transport")
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def pct(values: list[float], q: float) -> float:
    """Linear-interpolated percentile.

    Nearest-rank on small samples reports p99 *below* the max and nobody believes
    the number; interpolating between neighbours matches what dashboards show.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (q / 100.0) * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return float(ordered[low] + (ordered[high] - ordered[low]) * (pos - low))


def build_body(model: str, cached: bool, idx: int) -> dict[str, Any]:
    if cached:
        # temperature=0 + no assistant turn = the only payload the cache accepts.
        return {
            "model": model,
            "temperature": 0,
            "messages": [{"role": "user", "content": CACHED_PROMPTS[idx % len(CACHED_PROMPTS)]}],
        }
    return {
        "model": model,
        "messages": [{"role": "user", "content": f"load probe {idx}: summarise ticket #{idx}"}],
    }


async def one_request(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> Sample:
    started = time.perf_counter()
    try:
        resp = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:  # connect/read timeouts, resets, DNS
        return Sample(
            ok=False,
            status=None,
            error_code=f"transport:{type(exc).__name__}",
            throttled=False,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    latency_ms = (time.perf_counter() - started) * 1000.0
    if resp.status_code == 200:
        meta: dict[str, Any] = {}
        try:
            payload = resp.json()
            meta = payload.get("x_aigate") or {}
        except ValueError:
            return Sample(
                ok=False,
                status=200,
                error_code="bad_json",
                throttled=False,
                latency_ms=latency_ms,
            )
        timings = meta.get("latency_ms") or {}
        return Sample(
            ok=True,
            status=200,
            error_code=None,
            throttled=False,
            latency_ms=latency_ms,
            gateway_ms=_num(timings.get("gateway_total")),
            upstream_ms=_num(timings.get("upstream_ms")),
            cache_hit=meta.get("cache_hit"),
            degraded=bool(meta.get("degraded")),
        )

    code = None
    try:
        body_err = resp.json().get("error") or {}
        code = body_err.get("code")
    except ValueError:
        code = None
    throttled = resp.status_code == 429
    return Sample(
        ok=False,
        status=resp.status_code,
        error_code=code or f"http_{resp.status_code}",
        throttled=throttled,
        latency_ms=latency_ms,
    )


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


async def worker(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    model: str,
    cached: bool,
    start: int,
    stop: int,
    sink: list[Sample],
) -> None:
    for idx in range(start, stop):
        sink.append(await one_request(client, url, headers, build_body(model, cached, idx)))


async def run(args: argparse.Namespace, key: str) -> Report:
    url = args.base.rstrip("/") + "/v1/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "content-type": "application/json"}
    limits = httpx.Limits(max_connections=args.concurrency + 10, max_keepalive_connections=args.concurrency)
    timeout = httpx.Timeout(args.timeout)
    report = Report(warmup=args.warmup)

    async with httpx.AsyncClient(limits=limits, timeout=timeout, trust_env=False) as client:
        # Sequential warm-up: pay TCP/TLS setup, DB page cache and price lookups
        # before the clock starts, otherwise the first few samples are the p99.
        for i in range(args.warmup):
            await one_request(client, url, headers, build_body(args.model, args.cached, i))

        # Contiguous slices keep the request mix deterministic and the code free of
        # a shared counter/lock that would itself become the measurement.
        chunk = args.requests // args.concurrency
        ranges: list[tuple[int, int]] = []
        cursor = 0
        for w in range(args.concurrency):
            stop = cursor + chunk if w < args.concurrency - 1 else args.requests
            if cursor < stop:
                ranges.append((cursor, stop))
            cursor = stop

        started = time.perf_counter()
        await asyncio.gather(
            *(
                worker(client, url, headers, args.model, args.cached, lo, hi, report.samples)
                for lo, hi in ranges
            )
        )
        report.duration_s = time.perf_counter() - started

    return report


def evaluate(report: Report, args: argparse.Namespace) -> list[str]:
    """Every violated threshold, in human terms. Empty list = the run passed."""
    failures: list[str] = []
    if report.total == 0:
        return ["no requests were issued"]
    if report.error_rate > args.max_error_rate:
        failures.append(
            f"error_rate {report.error_rate:.2%} > {args.max_error_rate:.2%} "
            f"({len(report.errors)}/{report.total} errors, throttling excluded)"
        )
    if report.throttle_rate > args.max_throttle_rate:
        failures.append(
            f"throttle_rate {report.throttle_rate:.2%} > {args.max_throttle_rate:.2%} "
            f"({len(report.throttled)}/{report.total} HTTP 429 — gateway rate limit, "
            "not necessarily a defect)"
        )
    lat = report.latency()
    if lat["p95"] > args.max_p95_ms:
        failures.append(f"p95 {lat['p95']:.0f}ms > {args.max_p95_ms:.0f}ms")
    if lat["p99"] > args.max_p99_ms:
        failures.append(f"p99 {lat['p99']:.0f}ms > {args.max_p99_ms:.0f}ms")
    if args.min_rps and report.rps < args.min_rps:
        failures.append(f"rps {report.rps:.1f} < {args.min_rps:.1f}")
    return failures


def _fmt_percentiles(name: str, values: dict[str, float], suffix: str = "ms") -> str:
    if not values:
        return f"  {name:<16} n/a"
    body = "  ".join(f"{k} {v:g}{suffix}" for k, v in values.items())
    return f"  {name:<16} {body}"


def print_report(report: Report, args: argparse.Namespace, failures: list[str]) -> None:
    lat = report.latency()
    ok_lat = report.latency_ok()
    gw = report.gateway_latency()
    up = report.upstream_latency()
    latencies = [s.latency_ms for s in report.samples]
    mean = statistics.fmean(latencies) if latencies else 0.0

    print("=" * 74)
    print("aigate load test — POST /v1/chat/completions")
    print("=" * 74)
    print(f"  target          {args.base.rstrip('/')}/v1/chat/completions")
    print(f"  model           {args.model}   mode: {'cached (temperature=0)' if args.cached else 'plain'}")
    print(f"  concurrency     {args.concurrency}")
    print(f"  requests        {args.requests} (+{report.warmup} warm-up, not counted)")
    print(f"  duration        {report.duration_s:.2f}s")
    print(f"  rps             {report.rps:.1f}")
    print(f"  ok              {len(report.oks)} ({len(report.oks) / max(report.total, 1):.1%})")
    print(f"  throttled 429   {len(report.throttled)} ({report.throttle_rate:.1%})")
    print(f"  errors          {len(report.errors)} ({report.error_rate:.1%})")
    print(f"  cache hit rate  {report.cache_hit_rate:.1%}")
    degraded = sum(1 for s in report.oks if s.degraded)
    if degraded:
        print(f"  degraded        {degraded} ({degraded / max(len(report.oks), 1):.1%} served by a fallback)")
    print("-" * 74)
    print(_fmt_percentiles("client latency", lat))
    print(f"  {'':<16} min {min(latencies) if latencies else 0:.0f}ms  "
          f"mean {mean:.0f}ms  max {max(latencies) if latencies else 0:.0f}ms")
    print(_fmt_percentiles("  only 200s", ok_lat))
    print(_fmt_percentiles("gateway total", gw))
    print(_fmt_percentiles("upstream", up))
    if up.get("p50") and ok_lat.get("p50"):
        overhead = ok_lat["p50"] - up["p50"]
        print(f"  {'overhead':<16} p50 {overhead:.0f}ms client-side "
              f"({overhead / ok_lat['p50']:.0%} of p50)")
    breakdown = report.error_breakdown()
    if breakdown:
        print("-" * 74)
        print("  failures by code:")
        for code, count in breakdown.items():
            print(f"    {code:<34} {count}")
    print("-" * 74)
    print("  thresholds      "
          f"error<={args.max_error_rate:.1%}  throttle<={args.max_throttle_rate:.1%}  "
          f"p95<={args.max_p95_ms:.0f}ms  p99<={args.max_p99_ms:.0f}ms"
          + (f"  rps>={args.min_rps:.1f}" if args.min_rps else ""))
    if failures:
        print("  RESULT          FAIL")
        for f in failures:
            print(f"    - {f}")
    else:
        print("  RESULT          PASS")
    print("=" * 74)


def dump_json(report: Report, args: argparse.Namespace, failures: list[str], path: str) -> None:
    payload = {
        "target": args.base.rstrip("/") + "/v1/chat/completions",
        "model": args.model,
        "cached": args.cached,
        "concurrency": args.concurrency,
        "requests": args.requests,
        "duration_s": round(report.duration_s, 3),
        "rps": round(report.rps, 2),
        "latency_ms": report.latency(),
        "latency_ok_ms": report.latency_ok(),
        "gateway_ms": report.gateway_latency(),
        "upstream_ms": report.upstream_latency(),
        "ok": len(report.oks),
        "throttled": len(report.throttled),
        "errors": len(report.errors),
        "error_rate": round(report.error_rate, 5),
        "throttle_rate": round(report.throttle_rate, 5),
        "cache_hit_rate": round(report.cache_hit_rate, 5),
        "failure_codes": report.error_breakdown(),
        "failures": failures,
        "passed": not failures,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    print(f"json written to {path}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Load test POST /v1/chat/completions on a running aigate instance.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--base", default=os.environ.get("AIGATE_BASE", DEFAULT_BASE), help="gateway base URL")
    p.add_argument(
        "--key",
        default=os.environ.get("AIGATE_KEY") or os.environ.get("AIGATE_API_KEY"),
        help="API key (sk-ag-...); defaults to $AIGATE_KEY",
    )
    p.add_argument("--model", default=DEFAULT_MODEL, help="virtual model to call")
    p.add_argument("--concurrency", type=int, default=20, help="in-flight requests")
    p.add_argument("--requests", type=int, default=200, help="total requests for the run")
    p.add_argument(
        "--cached",
        action="store_true",
        help="repeat a prompt pool with temperature=0 so the cache can serve hits",
    )
    p.add_argument("--warmup", type=int, default=5, help="sequential un-timed requests first")
    p.add_argument("--timeout", type=float, default=30.0, help="per-request timeout (seconds)")
    p.add_argument("--json", dest="json_path", default=None, help="also write the report to this file")
    p.add_argument("--max-error-rate", type=float, default=0.01, help="fail above this true-error ratio")
    p.add_argument("--max-throttle-rate", type=float, default=0.01, help="fail above this 429 ratio")
    p.add_argument("--max-p95-ms", type=float, default=800.0, help="fail above this p95")
    p.add_argument("--max-p99-ms", type=float, default=1500.0, help="fail above this p99")
    p.add_argument("--min-rps", type=float, default=0.0, help="fail below this throughput (0 = off)")
    args = p.parse_args(argv)
    if args.concurrency < 1:
        p.error("--concurrency must be >= 1")
    if args.requests < args.concurrency:
        p.error("--requests must be >= --concurrency")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.key:
        print("error: no API key. Pass --key sk-ag-... or set AIGATE_KEY.", file=sys.stderr)
        return 2

    report = asyncio.run(run(args, args.key))
    failures = evaluate(report, args)
    print_report(report, args, failures)
    if args.json_path:
        dump_json(report, args, failures, args.json_path)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())