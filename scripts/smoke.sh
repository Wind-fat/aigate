#!/usr/bin/env bash
# End-to-end smoke test against a real server process.
# Proves the whole chain works without any cloud API key: seed -> start -> call -> assert.
set -uo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8099}"
BASE="http://127.0.0.1:${PORT}"
PASS=0
FAIL=0

check() { # check <name> <haystack> <needle>
  if printf '%s' "$2" | grep -q -- "$3"; then
    echo "  PASS  $1"
    PASS=$((PASS + 1))
  else
    echo "  FAIL  $1   (expected to find: $3)"
    echo "        got: $(printf '%s' "$2" | head -c 200)"
    FAIL=$((FAIL + 1))
  fi
}

echo "== 1. reset db + seed =="
rm -f aigate.db
SEED_OUT=$(uv run python scripts/seed.py 2>&1)
KEY=$(printf '%s' "$SEED_OUT" | grep 'API KEY' | awk '{print $4}')
if [ -z "${KEY:-}" ]; then echo "$SEED_OUT"; echo "seed failed"; exit 1; fi
echo "  demo key: ${KEY:0:14}... (len ${#KEY})"

echo "== 2. start gateway on :$PORT =="
uv run uvicorn aigate.main:app --port "$PORT" --log-level warning &
SRV=$!
cleanup() { kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null; }
trap cleanup EXIT

for _ in $(seq 1 60); do
  if curl -sf "$BASE/health" >/dev/null 2>&1; then break; fi
  sleep 0.3
done

echo "== 3. assertions =="
check "health" "$(curl -s "$BASE/health")" '"status":"ok"'

R1=$(curl -s "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"chat-default","messages":[{"role":"user","content":"\u4f60\u597d\uff0c\u4ecb\u7ecd\u4e00\u4e0b\u4f60\u81ea\u5df1"}]}')
check "chat 200 + model_used=mock-ok" "$R1" '"model_used":"mock-ok"'
check "chat priced" "$R1" '"priced":true'
check "chat not degraded" "$R1" '"degraded":false'

R2=$(curl -s "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"failover-demo","messages":[{"role":"user","content":"hi"}]}')
check "fallback degraded=true" "$R2" '"degraded":true'
check "fallback served by backup" "$R2" '"model_used":"mock-ok"'

check "401 without key" "$(curl -s "$BASE/v1/chat/completions" -H 'Content-Type: application/json' -d '{"model":"chat-default","messages":[{"role":"user","content":"x"}]}')" 'gateway.missing_credentials'

check "models list" "$(curl -s "$BASE/v1/models" -H "Authorization: Bearer $KEY")" 'chat-default'

check "metrics exposed" "$(curl -s "$BASE/metrics")" 'aigate_requests_total'

echo "== 4. db side effects =="
check "usage rows written" "$(uv run python -c "
import sqlite3,os
c=sqlite3.connect('aigate.db')
print(c.execute('select count(*) from usage_records').fetchone()[0])
")" '2'

echo
echo "RESULT: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ] || exit 1