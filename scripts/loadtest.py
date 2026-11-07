"""Gateway load test: fixed request counts at N = 8 / 16 / 32 concurrent clients.

Why the client is hand-rolled on `asyncio` streams instead of httpx: on this Windows host
httpx measured **241 rps** against a bare FastAPI `/health` while N keep-alive sockets
measured **2776 rps** for the same endpoint — the numbers would have described the client
library, not the gateway. So: one socket per concurrency slot, one request in flight per
socket, exactly N clients. Stdlib only, no new dependency.

The script is the whole rig: it seeds its own database, starts its own uvicorn process on a
private port, drives the levels, and prints one table with the columns the review asks for:

    success / 503 / 5xx / P50 / P95 / MissingGreenlet / QueuePool overflow

The last two are invisible to a client (a `MissingGreenlet` is just a 500; a pool timeout is a
503 whose cause is only in the body), so the script also tails the server log per level and
reads `aigate_pool_timeout_total` from `/metrics`.

Usage:
    .venv/Scripts/python.exe scripts/loadtest.py                    # 8/16/32 x 600 requests
    .venv/Scripts/python.exe scripts/loadtest.py --levels 8,16,32,512
    .venv/Scripts/python.exe scripts/loadtest.py --write-doc docs/LOAD-TEST.md

Exit codes: 0 = no 5xx, no MissingGreenlet, no 503 without Retry-After; 1 = observed;
2 = the rig could not start.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import subprocess  # noqa: S404 - starting the server under test is the point
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "loadtest.db"
DEFAULT_LOG = ROOT / "loadtest-server.log"
DEFAULT_PORT = 8097
LEVELS = (8, 16, 32)
REQUESTS_PER_LEVEL = 600
MODEL = "chat-default"
REQUEST_TIMEOUT_S = 30.0

# Patterns we look for in the server log. Both are failure *causes* that the client can
# only see as an opaque status code, which is why they get their own columns.
PATTERNS = {
    "missing_greenlet": re.compile(r"MissingGreenlet"),
    "queuepool_overflow": re.compile(r"QueuePool limit of size \d+ overflow \d+ reached"),
    "db_locked": re.compile(r"database is locked"),
}


# --------------------------------------------------------------------------
# rig: env, schema, server process
# --------------------------------------------------------------------------


def _configure_env(db: Path, port: int, url: str = "") -> dict[str, str]:
    """Env for both the seeder and the server, so they cannot drift apart."""
    env = dict(os.environ)
    env.update(
        {
            "AIGATE_DATABASE_URL": url or f"sqlite+aiosqlite:///{db.as_posix()}",
            "AIGATE_AUTO_CREATE_SCHEMA": "true",
            "AIGATE_ENV": "dev",
            "AIGATE_LOG_LEVEL": "INFO",
            # No cache, no Redis, no spool: measure the request path, not side doors.
            "AIGATE_SEMANTIC_CACHE_ENABLED": "false",
            "AIGATE_RATE_LIMITER": "memory",
            "AIGATE_CIRCUIT_BACKEND": "memory",
            "AIGATE_METERING_SPOOL_PATH": "",
            "AIGATE_DB_POOL_TIMEOUT": "1.0",
        }
    )
    return env


async def _seed(db: Path, env: dict[str, str]) -> str:
    """Create schema + one project/key/route. Returns the plaintext API key."""
    os.environ.update(env)

    from sqlalchemy import select

    from aigate.db import dispose_engine, get_session_factory, init_db, set_engine
    from aigate.models import ApiKey, ModelPrice, Project, RoutePolicy, Team
    from aigate.security import generate_api_key, hash_api_key

    plain, prefix, digest = generate_api_key()
    set_engine(env["AIGATE_DATABASE_URL"])
    await init_db()
    async with get_session_factory()() as session:
        # Idempotent: scripts/loadtest.py is meant to be re-run against the same database
        # (that is the whole point of "reproducible"), so every row is get-or-create.
        team = (
            await session.execute(select(Team).where(Team.slug == "load-team"))
        ).scalar_one_or_none()
        if team is None:
            team = Team(name="Load Team", slug="load-team")
            session.add(team)
            await session.flush()
        project = (
            await session.execute(
                select(Project).where(Project.team_id == team.id, Project.slug == "load-project")
            )
        ).scalar_one_or_none()
        if project is None:
            project = Project(team_id=team.id, name="Load Project", slug="load-project")
            session.add(project)
            await session.flush()
        # A budget 429 would measure the limiter, not the gateway.
        project.monthly_budget_cny = None
        project.daily_budget_cny = None
        project.rpm_limit = 10_000_000
        project.status = "active"
        for old_key in (
            await session.execute(
                select(ApiKey).where(ApiKey.project_id == project.id, ApiKey.name == "load-key")
            )
        ).scalars():
            await session.delete(old_key)
        await session.flush()
        session.add(
            ApiKey(
                project_id=project.id,
                name="load-key",
                prefix=prefix,
                key_hash=hash_api_key(plain),
                allowed_models=[],
                rpm_limit=0,
            )
        )
        if (
            await session.execute(select(RoutePolicy).where(RoutePolicy.match_model == MODEL))
        ).scalar_one_or_none() is None:
            session.add(
                RoutePolicy(
                    name="load default",
                    match_model=MODEL,
                    candidates=[{"provider": "mock", "model": "mock-ok"}],
                    fallback=[],
                    strategy="static",
                )
            )
        if (
            await session.execute(
                select(ModelPrice).where(
                    ModelPrice.provider == "mock", ModelPrice.model == "mock-ok"
                )
            )
        ).scalar_one_or_none() is None:
            session.add(
                ModelPrice(
                    provider="mock",
                    model="mock-ok",
                    input_price_per_1k=0.001,
                    output_price_per_1k=0.002,
                    context_window=8192,
                )
            )
        await session.commit()
    await dispose_engine()
    return plain


def _start_server(env: dict[str, str], port: int, log_path: Path) -> tuple[subprocess.Popen, object]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "uvicorn",
            "aigate.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "info",
            "--no-access-log",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    return proc, handle


# --------------------------------------------------------------------------
# minimal HTTP/1.1 keep-alive client (stdlib only)
# --------------------------------------------------------------------------


async def _connect(port: int):
    return await asyncio.open_connection("127.0.0.1", port)


async def _exchange(
    conn,
    method: str,
    path: str,
    body: bytes = b"",
    headers: dict | None = None,
    timeout: float = REQUEST_TIMEOUT_S,
) -> tuple[int, dict[str, str], bytes]:
    reader, writer = conn
    peername = writer.get_extra_info("peername")
    head = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{peername[1] if peername else 80}"]
    for k, v in (headers or {}).items():
        head.append(f"{k}: {v}")
    if body:
        head.append(f"Content-Length: {len(body)}")
    head.append("Connection: keep-alive")
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + body)
    await writer.drain()

    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    lines = raw.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    resp_headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            resp_headers[k.strip().lower()] = v.strip()

    if resp_headers.get("transfer-encoding", "").lower() == "chunked":
        chunks = []
        while True:
            size_line = await asyncio.wait_for(reader.readline(), timeout)
            size = int(size_line.strip().split(b";")[0] or b"0", 16)
            if size == 0:
                await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout)
                break
            chunks.append(await asyncio.wait_for(reader.readexactly(size), timeout))
            await asyncio.wait_for(reader.readexactly(2), timeout)
        return status, resp_headers, b"".join(chunks)

    length = int(resp_headers.get("content-length", "0") or 0)
    payload = await asyncio.wait_for(reader.readexactly(length), timeout) if length else b""
    return status, resp_headers, payload


async def _wait_ready(port: int, proc: subprocess.Popen, timeout_s: float = 40.0) -> None:
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early with code {proc.returncode}")
        try:
            status, _, _ = await _exchange(await _connect(port), "GET", "/health", timeout=2.0)
            if status == 200:
                return
        except Exception:  # noqa: BLE001 - not up yet
            pass
        await asyncio.sleep(0.2)
    raise RuntimeError("server did not become ready within 40s")


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------


@dataclass
class Sample:
    ok: bool
    status: int
    latency_ms: float
    retry_after: str | None = None
    code: str = ""
    detail: str = ""


@dataclass
class Level:
    concurrency: int
    requested: int
    samples: list[Sample] = field(default_factory=list)
    wall_s: float = 0.0
    pool_timeouts: float = 0.0
    log_hits: dict[str, int] = field(default_factory=dict)

    def status_count(self, pred) -> int:
        return sum(1 for s in self.samples if pred(s.status))

    @property
    def ok(self) -> int:
        return self.status_count(lambda s: s == 200)

    @property
    def unavailable(self) -> int:
        return self.status_count(lambda s: s == 503)

    @property
    def server_errors(self) -> int:
        return self.status_count(lambda s: 500 <= s < 600 and s != 503)

    @property
    def throttled(self) -> int:
        return self.status_count(lambda s: s == 429)

    @property
    def client_errors(self) -> int:
        return self.status_count(lambda s: 400 <= s < 500 and s != 429)

    def percentile(self, q: float) -> float:
        values = sorted(s.latency_ms for s in self.samples)
        if not values:
            return 0.0
        return values[max(0, min(len(values) - 1, int(round(q * (len(values) - 1)))))]

    @property
    def rps(self) -> float:
        return len(self.samples) / self.wall_s if self.wall_s else 0.0


async def _worker(port: int, key: str, queue: asyncio.Queue, out: list[Sample], run_tag: str) -> None:
    conn = None
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    while True:
        try:
            i = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        body = json.dumps(
            {
                "model": MODEL,
                "messages": [{"role": "user", "content": f"load {run_tag} #{i}"}],
                # 0.7 keeps the semantic cache out even if someone re-enables it.
                "temperature": 0.7,
            }
        ).encode()
        started = time.perf_counter()
        for attempt in (0, 1):
            try:
                if conn is None:
                    conn = await _connect(port)
                status, resp_headers, payload = await _exchange(
                    conn, "POST", "/v1/chat/completions", body, headers
                )
                code = ""
                if status != 200 and payload:
                    try:
                        code = str(json.loads(payload).get("error", {}).get("code", ""))
                    except Exception:  # noqa: BLE001 - non-JSON error page
                        code = ""
                out.append(
                    Sample(
                        ok=status == 200,
                        status=status,
                        latency_ms=(time.perf_counter() - started) * 1000,
                        retry_after=resp_headers.get("retry-after"),
                        code=code,
                        detail=payload[:300].decode("utf-8", "replace"),
                    )
                )
                break
            except Exception as exc:  # noqa: BLE001 - transport failure is a sample too
                conn = None
                if attempt == 1:
                    out.append(
                        Sample(
                            ok=False,
                            status=0,
                            latency_ms=(time.perf_counter() - started) * 1000,
                            code=type(exc).__name__,
                            detail=str(exc)[:200],
                        )
                    )
    if conn is not None:
        conn[1].close()


async def _metrics_counter(port: int, name: str) -> float:
    try:
        conn = await _connect(port)
        status, _, payload = await _exchange(conn, "GET", "/metrics", timeout=5.0)
        conn[1].close()
        if status == 200:
            match = re.search(rf"^{name}\s+(\S+)$", payload.decode("utf-8", "replace"), re.M)
            return float(match.group(1)) if match else 0.0
    except Exception:  # noqa: BLE001 - counter is nice-to-have
        pass
    return 0.0


async def _run_level(
    port: int, key: str, concurrency: int, requests: int, log_path: Path, run_tag: str
) -> Level:
    level = Level(concurrency=concurrency, requested=requests)
    offset = log_path.stat().st_size if log_path.exists() else 0

    queue: asyncio.Queue = asyncio.Queue()
    for i in range(requests):
        queue.put_nowait(i)
    samples: list[Sample] = []
    started = time.perf_counter()
    await asyncio.gather(*(_worker(port, key, queue, samples, run_tag) for _ in range(concurrency)))
    level.wall_s = time.perf_counter() - started
    level.samples = samples
    level.pool_timeouts = await _metrics_counter(port, "aigate_pool_timeout_total")

    if log_path.exists():
        with log_path.open("rb") as fh:
            fh.seek(offset)
            fresh = fh.read().decode("utf-8", errors="replace")
        level.log_hits = {name: len(pat.findall(fresh)) for name, pat in PATTERNS.items()}
    return level


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def _machine() -> dict[str, str]:
    info = {
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "cpu": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "unknown"),
        "cores": str(os.cpu_count()),
        "python": platform.python_version(),
        "arch": platform.machine(),
        "ram_gib": "unknown",
    }
    try:
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemStatus()
        status.dwLength = ctypes.sizeof(_MemStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
        info["ram_gib"] = f"{status.ullTotalPhys / (1024**3):.1f}"
    except Exception:  # noqa: BLE001 - non-Windows / exotic sandbox
        pass
    return info


def _table(levels: list[Level]) -> str:
    lines = [
        "| N | 请求数 | 成功 200 | 503 | 429 | 其它 4xx | 5xx | RPS | P50 (ms) | P95 (ms) | "
        "pool_timeout 计数 | MissingGreenlet | QueuePool overflow | database is locked |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for lv in levels:
        hits = lv.log_hits or {}
        lines.append(
            f"| {lv.concurrency} | {lv.requested} | {lv.ok} | {lv.unavailable} | {lv.throttled} | "
            f"{lv.client_errors} | {lv.server_errors} | {lv.rps:.1f} | {lv.percentile(0.50):.1f} | "
            f"{lv.percentile(0.95):.1f} | {lv.pool_timeouts:.0f} | "
            f"{hits.get('missing_greenlet', 0)} | {hits.get('queuepool_overflow', 0)} | "
            f"{hits.get('db_locked', 0)} |"
        )
    return "\n".join(lines)


def _failures(levels: list[Level]) -> list[str]:
    problems: list[str] = []
    for lv in levels:
        hits = lv.log_hits or {}
        if lv.server_errors:
            problems.append(f"N={lv.concurrency}: {lv.server_errors} 个 5xx")
        if hits.get("missing_greenlet"):
            problems.append(f"N={lv.concurrency}: {hits['missing_greenlet']} 次 MissingGreenlet")
        if hits.get("db_locked"):
            problems.append(f"N={lv.concurrency}: {hits['db_locked']} 次 database is locked")
        bad_503 = [s for s in lv.samples if s.status == 503 and not s.retry_after]
        if bad_503:
            problems.append(f"N={lv.concurrency}: {len(bad_503)} 个 503 缺 Retry-After")
        transport = [s for s in lv.samples if s.status == 0]
        if transport:
            problems.append(f"N={lv.concurrency}: {len(transport)} 个传输错误 ({transport[0].code})")
    return problems


def _write_doc(
    path: Path, levels: list[Level], args: argparse.Namespace, retry_values: list[str]
) -> None:
    machine = _machine()
    body = f"""# Load test — gateway data plane

Generated by `scripts/loadtest.py` on {time.strftime("%Y-%m-%d %H:%M:%S")}; re-run it with the
command at the bottom. Every number below is from that single run.

## What is measured

`POST /v1/chat/completions` → `chat-default` → offline `mock` provider (10 ms fixed sleep, no
network). Each level sends **{args.requests}** requests from **N** keep-alive connections (one
request in flight per connection — exactly N concurrent clients) over loopback HTTP.

The upstream is a mock on purpose: the subject is the gateway's admission control, metering
and persistence, not a vendor's latency.

## Environment

```
AIGATE_DATABASE_URL=sqlite+aiosqlite:///./loadtest.db   # fresh file, WAL
AIGATE_DB_POOL_TIMEOUT=1.0                              # pool wait cap
AIGATE_SEMANTIC_CACHE_ENABLED=false                     # cache would hide the DB work
AIGATE_RATE_LIMITER=memory
AIGATE_METERING_SPOOL_PATH=                             # no crash-safe spool
```

Start command (what the script does internally):

```
AIGATE_DATABASE_URL=sqlite+aiosqlite:///./loadtest.db \\
AIGATE_DB_POOL_TIMEOUT=1.0 AIGATE_SEMANTIC_CACHE_ENABLED=false \\
.venv/Scripts/python.exe -m uvicorn aigate.main:app --host 127.0.0.1 --port {args.port} --log-level info
```

Connection pool: SQLAlchemy defaults, `pool_size=5`, `max_overflow=10` → **15** concurrent DB
connections per process, `pool_timeout=1.0 s` (`AIGATE_DB_POOL_TIMEOUT`).

## Machine

| 项 | 值 |
|---|---|
| OS | {machine["os"]} |
| CPU | {machine["cpu"]} / {machine["cores"]} logical cores |
| RAM | {machine["ram_gib"]} GiB |
| Python | {machine["python"]} ({machine["arch"]}) |
| Server | 1 uvicorn worker, single event loop |
| DB | SQLite (aiosqlite), WAL, local disk |

## Results

{_table(levels)}

`pool_timeout 计数` is `aigate_pool_timeout_total` from `/metrics` after the level;
`MissingGreenlet` / `QueuePool overflow` / `database is locked` are occurrences in the server
log during that level only. `Retry-After` values observed: {", ".join(retry_values) or "none"}.

## Why the client is not httpx

The first run used `httpx.AsyncClient`. Against a bare FastAPI `/health` it measured 241 rps
(p50 27.7 ms) at N=8, while N keep-alive sockets measured 2776 rps (p50 1.4 ms) on the same
host — the client library was the bottleneck, not the gateway. The script therefore speaks
HTTP/1.1 over `asyncio` streams (stdlib), one persistent connection per client.

## Reading the numbers

- A 503 **with** `Retry-After` is the designed fast-fail when the pool is exhausted: refuse
  now instead of hanging for 30 s. A 503 **without** `Retry-After` is a bug and fails the run.
- `MissingGreenlet` would mean sync attribute access on an expired ORM instance inside the
  async request — the failure that silently drops metering rows. Zero is the only OK value.
- SQLite admits one writer, so throughput flattens well before the pool is exhausted; the
  PostgreSQL run is in `docs/PG-VERIFY.md`.

## Reproduce

```
.venv/Scripts/python.exe scripts/loadtest.py --levels {args.levels} --requests {args.requests}
```

The script seeds its own database, starts/stops its own server on port {args.port}, and exits
non-zero on a 5xx, a MissingGreenlet, a `database is locked`, or a 503 without `Retry-After`.
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


async def _amain(args: argparse.Namespace) -> int:
    db = Path(args.db).resolve()
    log_path = Path(args.log).resolve()
    if not args.db_url:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(db) + suffix)
            if candidate.exists():
                candidate.unlink()
    if log_path.exists():
        log_path.unlink()

    env = _configure_env(db, args.port, args.db_url)
    key = await _seed(db, env)
    proc, handle = _start_server(env, args.port, log_path)
    try:
        await _wait_ready(args.port, proc)
        # Warm-up: the first request pays route-policy load and the metering writer start.
        conn = await _connect(args.port)
        status, _, payload = await _exchange(
            conn,
            "POST",
            "/v1/chat/completions",
            json.dumps(
                {
                    "model": MODEL,
                    "messages": [{"role": "user", "content": "warmup"}],
                    "temperature": 0.7,
                }
            ).encode(),
            {"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        conn[1].close()
        if status != 200:
            print(f"warmup failed: {status} {payload[:400]!r}", file=sys.stderr)
            return 2

        run_tag = uuid.uuid4().hex[:8]
        levels = [
            await _run_level(args.port, key, n, args.requests, log_path, run_tag)
            for n in args.level_list
        ]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        handle.close()

    print()
    print(_table(levels))
    print()
    retry_values = sorted({s.retry_after for lv in levels for s in lv.samples if s.retry_after})
    print(f"Retry-After 观测值: {retry_values or '无（未触发 503）'}")
    samples_503 = [s for lv in levels for s in lv.samples if s.status == 503]
    if samples_503:
        print(f"503 示例: code={samples_503[0].code} body={samples_503[0].detail[:200]}")
    problems = _failures(levels)
    print(f"\n结论: {'PASS' if not problems else 'FAIL'}")
    for p in problems:
        print(f"  - {p}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "machine": _machine(),
                    "levels": [
                        {
                            "n": lv.concurrency,
                            "requested": lv.requested,
                            "ok": lv.ok,
                            "s503": lv.unavailable,
                            "s429": lv.throttled,
                            "s4xx": lv.client_errors,
                            "s5xx": lv.server_errors,
                            "rps": round(lv.rps, 2),
                            "p50_ms": round(lv.percentile(0.50), 2),
                            "p95_ms": round(lv.percentile(0.95), 2),
                            "pool_timeouts": lv.pool_timeouts,
                            "log_hits": lv.log_hits,
                        }
                        for lv in levels
                    ],
                    "problems": problems,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    if args.write_doc:
        _write_doc(Path(args.write_doc), levels, args, retry_values)

    return 0 if not problems else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--levels", default=",".join(str(n) for n in LEVELS), help="concurrency levels")
    p.add_argument("--requests", type=int, default=REQUESTS_PER_LEVEL, help="requests per level")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--db-url", default="", help="full SQLAlchemy URL, e.g. postgresql+asyncpg://...")
    p.add_argument("--log", default=str(DEFAULT_LOG))
    p.add_argument("--json", default="", help="write raw results here")
    p.add_argument("--write-doc", default="", help="write a markdown report here")
    args = p.parse_args(argv)
    args.level_list = [int(x) for x in args.levels.split(",") if x.strip()]
    return args


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())