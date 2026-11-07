"""Start a throwaway real PostgreSQL for local verification (no Docker, no system psql).

`pgserver` (pip, wheel per Python <=3.12) bundles the PostgreSQL binaries, so this is the one
way to get a real server on a Windows box that has neither Docker nor psql. The binary lives in
its own venv so it never becomes a runtime dependency of the gateway:

    uv venv .pgvenv -p 3.12
    uv pip install --python .pgvenv/Scripts/python.exe pgserver
    .pgvenv/Scripts/python.exe scripts/pg_local.py --pgdata .pgdata --port 5433

It prints the async SQLAlchemy URL and then blocks; Ctrl-C stops the server.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pgdata", default=".pgdata", help="data directory (created if missing)")
    args = p.parse_args()

    import pgserver

    pgdata = Path(args.pgdata).resolve()
    pgdata.parent.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(pgdata, cleanup_mode=None)

    uri = server.get_uri()
    print(f"PGDATA={pgdata}", flush=True)
    print(f"PID={server.get_pid()}", flush=True)
    print(f"URI={uri}", flush=True)
    print(f"AIGATE_DATABASE_URL=postgresql+asyncpg://{uri.split('://', 1)[1]}", flush=True)
    print("ready; Ctrl-C to stop", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())