#!/usr/bin/env bash
# End-to-end smoke test against a real server process.
# Proves the whole chain works without any cloud API key: seed -> start -> call -> assert.
#
# Order matters: the circuit-breaker section runs LAST, because tripping the
# `mock` breaker also skips every other policy that routes to `mock`.
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
    echo "        got: $(printf '%s' "$2" | head -c 220)"
    FAIL=$((FAIL + 1))
  fi
}

chat() { # chat <model> <content> [extra-json-fields]
  local model="$1" content="$2" extra="${3:-}"
  curl -s "$BASE/v1/chat/completions" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"$content\"}]$extra}"
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

echo "== 3. baseline =="
check "health" "$(curl -s "$BASE/health")" '"status":"ok"'

R1=$(chat chat-default "hello")
check "chat 200 -> mock-ok" "$R1" '"model_used":"mock-ok"'
check "chat priced" "$R1" '"priced":true'
check "chat not degraded" "$R1" '"degraded":false'

check "401 without key" "$(curl -s "$BASE/v1/chat/completions" -H 'Content-Type: application/json' -d '{"model":"chat-default","messages":[{"role":"user","content":"x"}]}')" 'gateway.missing_credentials'
check "models list" "$(curl -s "$BASE/v1/models" -H "Authorization: Bearer $KEY")" 'chat-default'
check "metrics exposed" "$(curl -s "$BASE/metrics")" 'aigate_requests_total'

echo "== 4. streaming =="
STREAM=$(curl -sN "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"chat-stream","stream":true,"messages":[{"role":"user","content":"stream me"}]}')
check "sse chunks" "$STREAM" 'chat.completion.chunk'
check "sse done sentinel" "$STREAM" 'data: \[DONE\]'
check "sse gateway meta comment" "$STREAM" ': aigate'
check "sse ttft recorded" "$STREAM" '"ttft_ms"'

echo "== 5. PII masking =="
PII=$(chat chat-default "call me at 13812345678")
if printf '%s' "$PII" | grep -q '13812345678'; then
  echo "  FAIL  raw PII leaked upstream"
  FAIL=$((FAIL + 1))
else
  echo "  PASS  raw PII not present in response"
  PASS=$((PASS + 1))
fi
check "masked form present" "$PII" '138\*\*\*\*5678'

echo "== 5.5 semantic cache =="
C1=$(chat chat-default "cache probe question" ',"temperature":0')
C2=$(chat chat-default "cache probe question" ',"temperature":0')
check "first call is a miss" "$C1" '"cache_hit":null'
check "second call hits L1" "$C2" '"cache_hit":"L1"'
check "cache hit costs nothing" "$C2" '"cost_cny":0.0'
check "cache hit reports savings" "$C2" '"saved_cny":'

echo "== 6. fallback + circuit breaker (runs last on purpose) =="
for i in 1 2 3 4; do
  R=$(chat failover-demo "hi")
  check "failover #$i -> degraded, still 200" "$R" '"degraded":true'
done
check "fallback counter exposed" "$(curl -s "$BASE/metrics")" 'aigate_fallbacks_total'

echo "== 7. db side effects =="
# the accounting writer batches (default flush interval 200ms); give it a beat before
# reading the table directly, otherwise this check races the writer
sleep 0.6
ROWS=$(uv run python -c "
import sqlite3
c = sqlite3.connect('aigate.db')
print(c.execute('select count(*) from usage_records').fetchone()[0])
")
if [ "${ROWS:-0}" -ge 9 ]; then
  echo "  PASS  usage rows written ($ROWS >= 9)"
  PASS=$((PASS + 1))
else
  echo "  FAIL  expected >= 9 usage rows, got ${ROWS:-0}"
  FAIL=$((FAIL + 1))
fi

echo
echo "RESULT: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ] || exit 1