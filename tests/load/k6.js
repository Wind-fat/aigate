// k6 equivalent of tests/load/loadtest.py — same target, same accounting rules.
//
//   k6 run -e KEY=sk-ag-... tests/load/k6.js
//   k6 run -e KEY=sk-ag-... -e CONCURRENCY=20 -e REQUESTS=200 -e CACHED=1 tests/load/k6.js
//
// Accounting rules copied from the Python script on purpose, so the two tools are
// comparable:
//   * 429 is *throttled*, reported separately from real errors (the gateway limits
//     per project, default 60 rpm; a run that hits it measured the limiter, not
//     throughput).
//   * `--cached` sends a fixed prompt pool with temperature=0, the only payload
//     shape the semantic cache accepts.
//   * exit code is non-zero when a threshold is breached (k6 does that natively).
//
// Behind a system proxy, 127.0.0.1 needs NO_PROXY=127.0.0.1 — k6 honours
// HTTP_PROXY/NO_PROXY, unlike httpx with trust_env=False.

import http from 'k6/http';
import { check } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

const BASE = (__ENV.BASE || 'http://127.0.0.1:8093').replace(/\/$/, '');
const MODEL = __ENV.MODEL || 'chat-default';
const CONCURRENCY = Number(__ENV.CONCURRENCY || 20);
const REQUESTS = Number(__ENV.REQUESTS || 200);
const CACHED = String(__ENV.CACHED || '0') === '1';
const KEY = __ENV.KEY || __ENV.AIGATE_KEY || '';

const MAX_ERROR_RATE = Number(__ENV.MAX_ERROR_RATE || 0.01);
const MAX_THROTTLE_RATE = Number(__ENV.MAX_THROTTLE_RATE || 0.01);
const MAX_P95_MS = Number(__ENV.MAX_P95_MS || 800);
const MAX_P99_MS = Number(__ENV.MAX_P99_MS || 1500);

const CACHED_PROMPTS = [
  'how do I reset my password',
  'where is my invoice for last month',
  'what is the refund window',
  'how do I enable two factor auth',
  'can I change my billing email',
];

const gatewayMs = new Trend('gateway_ms', true);
const upstreamMs = new Trend('upstream_ms', true);
const cacheHits = new Rate('cache_hits');
const throttled = new Counter('throttled_429');
const serverErrors = new Counter('server_errors');

export const options = {
  scenarios: {
    chat: {
      executor: 'shared-iterations',
      vus: CONCURRENCY,
      iterations: REQUESTS,
      maxDuration: '10m',
    },
  },
  thresholds: {
    // Throttling and real failures fail the run separately, exactly like the
    // Python script's --max-throttle-rate / --max-error-rate.
    http_req_failed: [`rate<${MAX_ERROR_RATE + MAX_THROTTLE_RATE}`],
    'http_req_duration{expected_response:true}': [`p(95)<${MAX_P95_MS}`, `p(99)<${MAX_P99_MS}`],
    checks: ['rate>0.99'],
  },
};

if (!KEY) {
  throw new Error('missing API key: pass -e KEY=sk-ag-... or -e AIGATE_KEY=...');
}

function bodyFor(i) {
  if (CACHED) {
    return JSON.stringify({
      model: MODEL,
      temperature: 0,
      messages: [{ role: 'user', content: CACHED_PROMPTS[i % CACHED_PROMPTS.length] }],
    });
  }
  return JSON.stringify({
    model: MODEL,
    messages: [{ role: 'user', content: `load probe ${i}: summarise ticket #${i}` }],
  });
}

export default function () {
  const res = http.post(`${BASE}/v1/chat/completions`, bodyFor(__ITER), {
    headers: {
      Authorization: `Bearer ${KEY}`,
      'Content-Type': 'application/json',
    },
    tags: { name: 'chat_completions' },
    timeout: '30s',
  });

  const ok = res.status === 200;
  if (res.status === 429) {
    throttled.add(1);
  } else if (res.status >= 500) {
    serverErrors.add(1);
  }

  let meta = null;
  if (ok) {
    try {
      meta = res.json().x_aigate || null;
    } catch (e) {
      meta = null;
    }
    if (meta) {
      const timings = meta.latency_ms || {};
      if (typeof timings.gateway_total === 'number') gatewayMs.add(timings.gateway_total);
      if (typeof timings.upstream_ms === 'number') upstreamMs.add(timings.upstream_ms);
      cacheHits.add(Boolean(meta.cache_hit));
    }
  }

  check(res, {
    'status is 200': () => ok,
    'not throttled': () => res.status !== 429,
  });
}

function p(metric, q) {
  const values = {
    'p(50)': metric ? metric.values['p(50)'] : undefined,
    'p(95)': metric ? metric.values['p(95)'] : undefined,
    'p(99)': metric ? metric.values['p(99)'] : undefined,
  };
  return values[q];
}

export function handleSummary(data) {
  const m = data.metrics;
  const dur = m.http_req_duration ? m.http_req_duration.values : {};
  const reqs = m.http_reqs ? m.http_reqs.values : {};
  const failed = m.http_req_failed ? m.http_req_failed.values.rate : 0;
  const throttleCount = m.throttled_429 ? m.throttled_429.values.count : 0;
  const total = reqs.count || 1;
  const hitRate = m.cache_hits ? m.cache_hits.values.rate : 0;

  const lines = [
    '='.repeat(74),
    'aigate load test (k6) — POST /v1/chat/completions',
    '='.repeat(74),
    `  target          ${BASE}/v1/chat/completions`,
    `  model           ${MODEL}   mode: ${CACHED ? 'cached (temperature=0)' : 'plain'}`,
    `  concurrency     ${CONCURRENCY}`,
    `  requests        ${total}`,
    `  rps             ${(reqs.rate || 0).toFixed(1)}`,
    `  throttled 429   ${throttleCount} (${((throttleCount / total) * 100).toFixed(1)}%)`,
    `  failed          ${(failed * 100).toFixed(1)}% (includes 429 — see line above)`,
    `  cache hit rate  ${(hitRate * 100).toFixed(1)}%`,
    '--------------------------------------------------------------------------',
    `  client latency  p50 ${(dur['p(50)'] || 0).toFixed(0)}ms  ` +
      `p95 ${(dur['p(95)'] || 0).toFixed(0)}ms  p99 ${(dur['p(99)'] || 0).toFixed(0)}ms`,
    `  gateway total   p50 ${p(m.gateway_ms, 'p(50)') || 0}ms  ` +
      `p95 ${p(m.gateway_ms, 'p(95)') || 0}ms  p99 ${p(m.gateway_ms, 'p(99)') || 0}ms`,
    `  upstream        p50 ${p(m.upstream_ms, 'p(50)') || 0}ms  ` +
      `p95 ${p(m.upstream_ms, 'p(95)') || 0}ms  p99 ${p(m.upstream_ms, 'p(99)') || 0}ms`,
    '='.repeat(74),
    '',
  ];

  return {
    stdout: lines.join('\n'),
    'summary.json': JSON.stringify(data, null, 2),
  };
}