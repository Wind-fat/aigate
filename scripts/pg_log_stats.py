"""Summarize a PostgreSQL log that ran with `log_min_duration_statement = 0`.

Why: `pg_stat_statements` is not available in the portable pgserver build, and even when it
is, it hides the extended-protocol split that matters for an asyncpg client — `parse`,
`bind` (which is where PostgreSQL *plans* a parameterized statement) and `execute` are three
separate log entries with three separate durations. A "slow query" that is actually 6 ms of
planning on a 0.03 ms execute looks like a fast query in `pg_stat_statements` and like a
90 ms request in the gateway.

    .venv/Scripts/python.exe scripts/pg_log_stats.py .pgdata/log --from "14:35:58" --to "14:36:04"
    .venv/Scripts/python.exe scripts/pg_log_stats.py .pgdata/log --from ... --to ... --md

Statements are normalised to their first few words (`SELECT api_keys.id, ... FROM api_keys`
-> `SELECT api_keys ...`), which is what makes counting across requests meaningful.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
from pathlib import Path

LINE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+) \w+ \[(?P<pid>\d+)\] "
    r"LOG:  duration: (?P<ms>[0-9.]+) ms  (?P<rest>.*)$"
)
CONN = re.compile(r"^.*\[(?P<pid>\d+)\] LOG:  (?:connection authorized|connection received).*$")


def normalize(text: str) -> str:
    text = " ".join(text.split())
    text = re.sub(r"__asyncpg_stmt_[0-9a-f]+__", "<stmt>", text)
    phase = ""
    for p in ("parse", "bind", "execute", "statement"):
        if text.startswith(p + " "):
            phase, text = p, text[len(p) + 1 :]
            break
    text = re.sub(r"<stmt>: ", "", text)
    words = text.split()
    # First SELECT/FROM target is enough to tell api_keys from route_policies apart.
    head = " ".join(words[:6])
    return f"{phase}: {head}" if phase else head


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("log", type=Path)
    ap.add_argument("--from", dest="ts_from", default="")
    ap.add_argument("--to", dest="ts_to", default="")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--md", action="store_true", help="print markdown tables")
    args = ap.parse_args()

    counts: Counter[str] = Counter()
    totals: defaultdict[str, float] = defaultdict(float)
    slowest: list[tuple[float, str, str]] = []
    per_pid: defaultdict[str, int] = defaultdict(int)
    conns = 0
    matched = 0

    for raw in args.log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = CONN.match(raw)
        if m and args.ts_from <= raw[11:19] <= args.ts_to:
            conns += 1
            continue
        m = LINE.match(raw)
        if not m:
            continue
        ts = m.group("ts")[11:19]
        if args.ts_from and ts < args.ts_from:
            continue
        if args.ts_to and ts > args.ts_to:
            continue
        matched += 1
        ms = float(m.group("ms"))
        key = normalize(m.group("rest"))
        counts[key] += 1
        totals[key] += ms
        per_pid[m.group("pid")] += 1
        slowest.append((ms, m.group("ts"), m.group("rest")[:160]))

    print(f"window {args.ts_from or '-'} .. {args.ts_to or '-'}: {matched} statements, "
          f"{conns} connection events, {len(per_pid)} backends")
    if args.md:
        print("\n### 出现次数最多（statement 计数）\n")
        print("| 语句（归一化） | 次数 | 累计 ms | 平均 ms |")
        print("|---|---|---|---|")
    for key, n in counts.most_common(12):
        if args.md:
            print(f"| `{key[:70]}` | {n} | {totals[key]:.1f} | {totals[key] / n:.2f} |")
        else:
            print(f"{n:5d}  {totals[key]:9.1f} ms  avg {totals[key] / n:7.2f}  {key[:80]}")

    print("\n### 累计耗时最多\n" if args.md else "\ntotal time:")
    if args.md:
        print("| 语句（归一化） | 累计 ms | 次数 | 平均 ms |")
        print("|---|---|---|---|")
    for key, total in sorted(totals.items(), key=lambda kv: -kv[1])[: args.top]:
        if args.md:
            print(f"| `{key[:70]}` | {total:.1f} | {counts[key]} | {total / counts[key]:.2f} |")
        else:
            print(f"{total:9.1f} ms  x{counts[key]:<4d} {key[:80]}")

    print(f"\n### 单条最慢 top{args.top}\n" if args.md else "\nslowest single:")
    if args.md:
        print("| ms | 时间 | 语句 |")
        print("|---|---|---|")
    for ms, ts, text in sorted(slowest, reverse=True)[: args.top]:
        if args.md:
            print(f"| {ms:.3f} | {ts[11:]} | `{text[:110]}` |")
        else:
            print(f"{ms:9.3f} ms  {ts}  {text[:110]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())