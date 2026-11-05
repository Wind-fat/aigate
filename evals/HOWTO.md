# 评测执行器（evals/run.py）

对**正在运行的网关**发真实 HTTP 请求，按 YAML 里的 `assert` 判分，输出 markdown 报告。
用例格式见 `evals/README.md`；`smoke.yaml` 是冒烟集，`regression.yaml` 是 15 条回归集。

## 一次性准备

```bash
cd /d/code/aigate
rm -f eval_tmp.db
AIGATE_DATABASE_URL="sqlite+aiosqlite:///./eval_tmp.db" uv run python scripts/seed.py

# seed.py 没有 bad-request 路由（只在 tests/conftest.py 里有），评测需要它
AIGATE_DATABASE_URL="sqlite+aiosqlite:///./eval_tmp.db" uv run python evals/bootstrap.py

AIGATE_DATABASE_URL="sqlite+aiosqlite:///./eval_tmp.db" \
  uv run uvicorn aigate.main:app --port 8092 &
```

## 跑评测

```bash
uv run python evals/run.py smoke --base-url http://127.0.0.1:8092
uv run python evals/run.py regression --base-url http://127.0.0.1:8092
uv run python evals/run.py smoke regression --md evals/report.md --json evals/report.json
make eval DATASET=regression          # 需要 AIGATE_EVAL_BASE_URL 或默认 :8080

# 只跑某条，便于复现失败
uv run python evals/run.py regression --filter reg-014
```

不传 `--api-key` 时会用 `X-Admin-Token` 自动签一把 **只覆盖本次数据集所用模型** 的 key；
`__forbidden_model__` 这类占位模型故意不放进 scope，否则 403 用例永远测不出东西。
`--allow-all-models` 可关闭这个行为。

## 退出码

| code | 含义 |
|---:|---|
| 0 | 全部用例通过 |
| 1 | 至少一条用例断言失败 |
| 2 | 执行器错误：数据集不合法、网关不可达、拿不到 key |

## 除 README 字段外，本执行器额外支持

- `temperature` / `stream` / `params`（max_tokens、top_p、stop、user…）
- `auth: none | invalid`
- `repeat: N`：连发 N 次，`assert` 判最后一次；`assert_first` 判第一次（缓存 warm-up 用）
- 消息内容里的 `{nonce}` / `{run_id}` 占位符，避免历史缓存 / 旧数据让断言失真
- `assert` 键：`http_status`、`error_code`、`contains`、`not_contains`、`not_empty`、
  `degraded`、`fallback_attempts`、`cache_hit`、`model_used`、`finish_reason`、
  `stream_error_code`、`max_latency_ms`、`max_cost_cny`、`min_cost_cny`、`must_refuse`、
  `json_schema`（内置最小校验，无额外依赖）
- `judge` 未接入：出现该字段会直接报数据集错误，避免"看起来跑了其实没判"

`fallback_attempts` 不是响应体字段，执行器会用 `request_id` 查
`GET /admin/v1/traces/{id}` 的 `route_path`，统计真正打到上游的尝试次数
（跳过熔断的候选不算），这样才能证明"4xx 没有触发 fallback"。

## 注意

- mock provider 是 `chat-default` 和 `failover-demo` 共用的；连续失败达到
  `circuit_threshold`（默认 3）会熔断，影响后续正常用例。`regression.yaml` 已把
  failover 用例放最后且只制造 2 次失败，仍建议每个数据集跑在干净进程上。
- 本机若开着系统 HTTP 代理，执行器与 curl 都必须绕过：`run.py` 内部
  `httpx.Client(trust_env=False)`，curl 用 `--noproxy '*'`。