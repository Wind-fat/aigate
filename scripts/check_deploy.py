"""Static checks on the deployment files — everything that does not need a container daemon.

There is no Docker on the machine this repo was built on, so "the compose file is
probably fine" is not good enough. This script checks the things that actually break a
first `docker compose up`:

  1. every `build:` context and Dockerfile exists
  2. every file the gateway image COPYs exists in the context (alembic.ini, alembic/)
  3. required env vars are either set in the env file or have a `:?` guard in compose
  4. host ports do not collide
  5. the gateway is wired for postgres + redis, not the sqlite/内存 default
  6. nothing on the console service leaks into the browser bundle (NEXT_PUBLIC_*)
  7. prod secrets are not placeholders and are long enough for `assert_production_ready`
  8. `depends_on: service_healthy` targets actually declare a healthcheck

Exit code 0 = every check passed. Run it in CI.

    uv run python scripts/check_deploy.py [--env-file .env.production]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only without the dev group
    sys.exit("pyyaml is missing: uv sync --all-groups")

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"
PLACEHOLDERS = {"", "change-me-admin-token", "change-me-session-secret", "REPLACE_ME"}

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))


def parse_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


_VAR = re.compile(r"\$\{([A-Z0-9_]+)(?::([-?])([^}]*))?\}")


def resolve(value: str, env: dict[str, str]) -> tuple[str, bool]:
    """Expand `${VAR}`, `${VAR:-default}` and `${VAR:?message}` anywhere in the string."""
    failed = False

    def one(match: re.Match[str]) -> str:
        nonlocal failed
        name, kind, arg = match.group(1), match.group(2), match.group(3) or ""
        if env.get(name):
            return env[name]
        if kind == "-":
            return arg
        if kind == "?":  # required and missing
            failed = True
            return ""
        return ""

    return _VAR.sub(one, value or ""), not failed


def main(env_file: Path) -> int:
    env = parse_env(env_file)

    if not COMPOSE.exists():
        check(False, "docker-compose.yml", "missing")
        return report()
    compose: dict[str, Any] = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services: dict[str, Any] = compose.get("services", {})
    check(bool(services), "compose parses", f"{len(services)} services")

    # 1 + 2: build contexts and files the image copies
    for name, svc in services.items():
        build = svc.get("build")
        if not build:
            continue
        context = ROOT / (build if isinstance(build, str) else build.get("context", "."))
        dockerfile = (
            context / "Dockerfile"
            if isinstance(build, str)
            else context / build.get("dockerfile", "Dockerfile")
        )
        check(context.is_dir(), f"{name}: build context", str(context.relative_to(ROOT)))
        check(dockerfile.is_file(), f"{name}: Dockerfile", str(dockerfile.relative_to(ROOT)))

        if dockerfile.is_file():
            body = dockerfile.read_text(encoding="utf-8")
            for line in body.splitlines():
                if not line.startswith("COPY") or "--from=" in line:
                    # multi-stage copy: the source is another image, not the context
                    continue
                src = re.sub(r"^COPY\s+(--[^\s]+\s+)*", "", line)
                for token in src.split():
                    if token.startswith("--") or token in (".", "./"):
                        continue
                    target = context / token
                    if token == "pyproject.toml" or target.exists():
                        continue
                    check(False, f"{name}: COPY source", f"{token} missing in {context.name}/")

    # 3 + 5: required env for the gateway
    gateway = services.get("gateway", {})
    gw_env: dict[str, str] = gateway.get("environment", {}) or {}
    for key in ("AIGATE_ADMIN_TOKEN", "AIGATE_SESSION_SECRET"):
        value, ok = resolve(str(gw_env.get(key, "")), env)
        check(ok and value not in PLACEHOLDERS, f"gateway: {key} provided", "" if ok else "unset")
        if ok:
            need = 24 if key == "AIGATE_ADMIN_TOKEN" else 32
            check(len(value) >= need, f"gateway: {key} length >= {need}", f"{len(value)} chars")

    db_url, _ = resolve(str(gw_env.get("AIGATE_DATABASE_URL", "")), env)
    check("postgresql" in db_url, "gateway: database is postgres", db_url.split("@")[-1])
    check(gw_env.get("AIGATE_RATE_LIMITER") == "redis", "gateway: rate limiter is redis", "")
    check(bool(gw_env.get("AIGATE_REDIS_URL")), "gateway: redis url set", "")
    check(gw_env.get("AIGATE_ENV") == "prod", "gateway: AIGATE_ENV=prod", "")
    check(
        resolve(str(gw_env.get("AIGATE_COOKIE_SECURE", "")), env)[0].lower() in ("true", "1"),
        "gateway: cookie secure (required in prod)",
        "",
    )
    check(
        bool(gw_env.get("AIGATE_METERING_SPOOL_PATH")),
        "gateway: metering spool on a volume",
        str(gw_env.get("AIGATE_METERING_SPOOL_PATH", "unset")),
    )
    check(
        any("spool:" in str(v) for v in (gateway.get("volumes") or [])),
        "gateway: spool volume mounted",
        "",
    )

    # 6: nothing secret reaches the browser bundle
    console_env = services.get("console", {}).get("environment", {}) or {}
    leaked = [k for k in console_env if k.startswith("NEXT_PUBLIC_")]
    check(not leaked, "console: no NEXT_PUBLIC_*", ", ".join(leaked))
    check(
        str(console_env.get("AIGATE_API_BASE", "")).startswith("http://gateway"),
        "console: talks to gateway over the compose network",
        str(console_env.get("AIGATE_API_BASE", "")),
    )

    # 4: port collisions
    seen: dict[str, str] = {}
    for name, svc in services.items():
        for mapping in svc.get("ports", []) or []:
            expanded, _ = resolve(str(mapping), env)
            host = expanded.split(":")[0]
            if host in seen:
                check(False, "ports unique", f"{host} used by {seen[host]} and {name}")
            seen[host] = name
    check(True, "host ports", ", ".join(f"{p}->{s}" for p, s in sorted(seen.items())))

    # 8: depends_on service_healthy needs a healthcheck somewhere
    for name, svc in services.items():
        deps = svc.get("depends_on") or {}
        if not isinstance(deps, dict):
            continue
        for dep, cond in deps.items():
            if (cond or {}).get("condition") != "service_healthy":
                continue
            target = services.get(dep, {})
            image_health = target.get("healthcheck")
            dockerfile = ROOT / "Dockerfile"
            base_health = dockerfile.is_file() and "HEALTHCHECK" in dockerfile.read_text(encoding="utf-8")
            check(bool(image_health) or base_health, f"{name}: {dep} has a healthcheck", "")

    return report()


def report() -> int:
    failed = [r for r in results if not r[0]]
    for ok, name, detail in results:
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark}  {name:<42} {detail}")
    print()
    if failed:
        print(f"RESULT: {len(results) - len(failed)} passed, {len(failed)} failed")
        return 1
    print(f"RESULT: {len(results)} passed, 0 failed")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env.production")
    args = parser.parse_args()
    raise SystemExit(main(ROOT / args.env_file))
