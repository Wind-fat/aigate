#!/usr/bin/env bash
# One command, whole acceptance run: lint, unit tests, end-to-end smoke, deploy wiring.
#
# Exists separately from the Makefile because `make` is not installed everywhere
# (it is missing on the Windows dev box this was built on) — the script is the source of
# truth and `make accept` is the convenience wrapper.
set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "1/4 lint"
uv run ruff check src tests scripts evals || fail=1

step "2/4 unit + api tests"
uv run pytest -q || fail=1

step "3/4 end-to-end smoke (real process, mock provider)"
bash scripts/smoke.sh || fail=1

step "4/4 deployment wiring (no docker needed)"
if [ -f .env.production ]; then
  uv run python scripts/check_deploy.py --env-file .env.production || fail=1
else
  echo "  SKIP  .env.production not present (cp .env.production.example .env.production)"
fi

if [ "$fail" -eq 0 ]; then
  printf '\n\033[32mALL GOOD\033[0m\n'
else
  printf '\n\033[31mSOMETHING FAILED\033[0m\n'
fi
exit "$fail"
