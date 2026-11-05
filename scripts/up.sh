#!/usr/bin/env bash
# Start AIGate locally: gateway + console, then open the browser. Ctrl-C stops both.
set -uo pipefail
cd "$(dirname "$0")/.."

GATEWAY_PORT="${GATEWAY_PORT:-8080}"
CONSOLE_PORT="${CONSOLE_PORT:-3000}"
BASE="http://127.0.0.1:${GATEWAY_PORT}"

say() { printf '\033[1m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- prerequisites
[ -f .env ] || { say "first run: setting up"; uv run python scripts/setup.py; }
[ -f console/.env.local ] || uv run python scripts/setup.py

if [ ! -d console/node_modules ]; then
  say "installing console dependencies (one time, ~2 min)"
  (cd console && npm install --registry="${NPM_REGISTRY:-https://registry.npmmirror.com}" --no-audit --no-fund)
fi

if [ ! -d console/.next ]; then
  say "building console (one time, ~30 s)"
  (cd console && npm run build) || { echo "console build failed"; exit 1; }
fi

# ------------------------------------------------------------------- free ports
for port in "$GATEWAY_PORT" "$CONSOLE_PORT"; do
  for pid in $(netstat -ano 2>/dev/null | grep ":${port} " | grep LISTENING | awk '{print $5}' | sort -u); do
    say "port ${port} busy (pid ${pid}) — stopping it"
    MSYS_NO_PATHCONV=1 taskkill /F /PID "${pid}" >/dev/null 2>&1 || kill -9 "${pid}" 2>/dev/null
  done
done

# ----------------------------------------------------------------------- start
say "starting gateway on :${GATEWAY_PORT}"
uv run uvicorn aigate.main:app --port "$GATEWAY_PORT" --log-level warning &
GW=$!

for _ in $(seq 1 60); do
  curl -sf --noproxy '*' "$BASE/health" >/dev/null 2>&1 && break
  sleep 0.3
done
if ! curl -sf --noproxy '*' "$BASE/health" >/dev/null 2>&1; then
  echo "gateway did not start; run it manually to see the error:"
  echo "  uv run uvicorn aigate.main:app --port $GATEWAY_PORT"
  kill "$GW" 2>/dev/null
  exit 1
fi

say "starting console on :${CONSOLE_PORT}"
(cd console && npm run start -- --port "$CONSOLE_PORT") &
WEB=$!

for _ in $(seq 1 60); do
  curl -sf --noproxy '*' "http://127.0.0.1:${CONSOLE_PORT}/" >/dev/null 2>&1 && break
  sleep 0.3
done

cleanup() {
  say "stopping"
  kill "$WEB" 2>/dev/null
  kill "$GW" 2>/dev/null
  wait "$WEB" "$GW" 2>/dev/null
}
trap cleanup EXIT INT TERM

say "ready → http://127.0.0.1:${CONSOLE_PORT}   (api: ${BASE}/docs)"
say "Ctrl-C to stop"

python -c "import webbrowser;webbrowser.open('http://127.0.0.1:${CONSOLE_PORT}')" >/dev/null 2>&1 &

wait