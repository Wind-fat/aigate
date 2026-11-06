# Grafana

一个可直接导入的看板：[`aigate-dashboard.json`](aigate-dashboard.json)。

## 导入（两步）

1. **让 Prometheus 抓网关**

```yaml
# prometheus.yml
scrape_configs:
  - job_name: aigate
    metrics_path: /metrics
    static_configs:
      - targets: ["gateway:8080"]
```

2. **导入看板**：Grafana → Dashboards → Import → 上传 JSON → 选择你的 Prometheus 数据源
   （看板里的变量名是 `DS_PROMETHEUS`，导入时会让你选）。

## 看板里有什么，以及为什么没有别的

| 面板 | 指标 | 说明 |
|---|---|---|
| 请求速率 | `aigate_requests_total` | 唯一的“活着的”信号 |
| 错误率 | `aigate_upstream_errors_total` / 请求数 | 只算上游错误，4xx 是调用方的问题，不该进这个面板 |
| 降级速率 | `aigate_fallbacks_total{provider}` | 按供应商拆；某一家涨说明它在抖 |
| 缓存命中 | `aigate_cache_served_total{layer}` | L1 精确 / L2 语义分开看 |
| 平均延迟 | `aigate_http_duration_ms_total` / `aigate_http_requests_total` | **均值**，不是 p95 |
| 限流拒绝 | `aigate_rate_limited_total` | 突然涨 = 有人在跑循环，或限额配小了 |
| PII 命中 | `aigate_pii_masked_total` | 突然归零要查脱敏是否还生效 |

**为什么没有 p95、没有金额**：

- 指标里只有计数器，没有直方图。要 p95 得加 `Histogram`，那会显著增加内存与基数；
  这个项目里 p95 由 `usage_records` 现算（控制台「概览」/「调用明细」），更准也更便宜。
- 金额需要按 `team/project/model` 三维聚合，任一维都是高基数。Prometheus 不是账本，
  账单在 `usage_records`，看板在「预算配额」页。

这两条都是取舍，不是没做完——写在这里，省得有人照着别的网关的看板来找 p99。