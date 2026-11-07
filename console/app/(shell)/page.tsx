"use client";

import { useEffect, useState } from "react";
import { adminGet, money, ms, pct, shortTime, UNKNOWN } from "@/lib/api";
import { AreaChart, BarList, Spark } from "@/components/charts";
import { ErrorPanel, LoadingBlock, StaleBanner } from "@/components/async-state";
import { useResource } from "@/lib/use-resource";

/**
 * 概览。这一页要回答三个问题，顺序不能乱：
 *
 *   1. 现在正常吗？   —— 成功率 / P95 / 错误数，四个数字 + 趋势线
 *   2. 钱花在哪？     —— 成本趋势、Top 模型、按团队
 *   3. 出事了吗？     —— 预算告警、最近错误
 *
 * 图表是手写 SVG（components/charts.tsx）：数据点少、要读的是形状不是炫技。
 */

type Summary = {
  requests: number;
  errors: number;
  cached: number;
  /**
   * 窗口内是否有任何调用。false 时下面三个比率/延迟都是 null（后端返回）。
   * 这不是可选装饰：没有它就无法区分"0% 命中率"和"没有数据"。
   */
  has_data: boolean;
  success_rate: number | null;
  cache_hit_rate: number | null;
  cost_cny: number;
  avg_latency_ms: number | null;
  p95_latency_ms: number | null;
};

type Overview = {
  today: Summary;
  window: Summary & { days: number };
  series: { date: string; requests: number; cost_cny: number; errors: number }[];
  top_models: { model: string; requests: number; cost_cny: number }[];
  recent_errors: { request_id: string; model: string; code: string | null; created_at: string }[];
  by_team: { team: string; requests: number; cost_cny: number }[];
  open_alerts: number;
};

type Alert = {
  id: string;
  level: string;
  scope_slug: string;
  scope_type: string;
  metric: string;
  message: string;
  created_at: string;
};

function delta(now: number, before: number): { text: string; up: boolean } | null {
  if (!before) return null;
  const change = ((now - before) / before) * 100;
  if (Math.abs(change) < 1) return null;
  return { text: `${change > 0 ? "+" : ""}${change.toFixed(0)}%`, up: change > 0 };
}

export default function OverviewPage() {
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [days, setDays] = useState(7);

  // 每 10 秒轮询。以前任何一次失败都 `if (error) return <连接失败>`，把已经加载好的
  // 整页数据一起扔掉——网关重启 5 秒，页面就闪一次"连接失败"，然后要等下一轮。
  // 现在：只有"一次都没成功"才给错误页；有数据时降级成顶部横条 + 数据截止时间。
  const resource = useResource<Overview>(() => adminGet<Overview>(`/overview?days=${days}`), [days], {
    intervalMs: 10_000,
  });
  const data = resource.data;

  useEffect(() => {
    adminGet<{ items: Alert[] }>("/alerts?limit=5")
      .then((d) => setAlerts(d.items))
      .catch(() => undefined);
  }, [data]);

  if (!data) {
    return resource.error ? (
      <ErrorPanel title="连接失败" error={resource.error} onRetry={resource.reload} />
    ) : (
      <div className="panel">
        <LoadingBlock label="正在读取概览…" />
      </div>
    );
  }

  const requestsSeries = data.series.map((s) => ({ label: s.date.slice(5), value: s.requests }));
  const costSeries = data.series.map((s) => ({
    label: s.date.slice(5),
    value: Number(s.cost_cny.toFixed(6)),
  }));
  const half = Math.floor(data.series.length / 2);
  const firstHalf = data.series.slice(0, half).reduce((n, s) => n + s.requests, 0);
  const secondHalf = data.series.slice(half).reduce((n, s) => n + s.requests, 0);
  const trend = delta(secondHalf, firstHalf);

  const windowRequests = Math.max(1, data.window.requests);
  const errRate = data.window.requests ? (data.window.errors / data.window.requests) * 100 : null;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>概览</h1>
          <div className="sub">最近 {data.window.days} 天 · 每 10 秒自动刷新</div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
            <option value={1}>近 1 天</option>
            <option value={7}>近 7 天</option>
            <option value={30}>近 30 天</option>
          </select>
          <button className="ghost" onClick={resource.reload}>
            刷新
          </button>
        </div>
      </div>

      {resource.error && (
        <StaleBanner
          loadedAt={resource.loadedAt}
          failedAt={resource.failedAt}
          error={resource.error}
          onRetry={resource.reload}
        />
      )}

      {!data.window.has_data && (
        <div className="panel">
          <h2>窗口内没有调用</h2>
          <div className="sub">
            最近 {data.window.days} 天数据库里没有任何调用记录。成功率 / 延迟 / 命中率显示为
            「{UNKNOWN}」——它们现在是未知，不是 100%、0 ms 或 0%。
          </div>
        </div>
      )}

      {alerts.length > 0 && (
        <div className="panel" style={{ borderColor: "#5a3a1f" }}>
          <h2>预算告警（{alerts.length}）</h2>
          <div className="stack">
            {alerts.map((a) => (
              <div key={a.id} className="chain-row" style={{ gap: 10 }}>
                <span className={`badge ${a.level === "critical" ? "err" : "warn"}`}>{a.level}</span>
                <span className="mono">{a.message}</span>
                <span className="sub">{shortTime(a.created_at)}</span>
              </div>
            ))}
          </div>
          <div className="sub" style={{ marginTop: 8 }}>
            按（范围 / 指标 / 周期）去重；warn 升到 critical 会重新变成未确认。处置见「预算配额」页。
          </div>
        </div>
      )}

      <div className="cards">
        <div className="card">
          <div className="k">今日请求</div>
          <div className="v">{data.today.requests}</div>
          <Spark values={data.series.map((s) => s.requests)} />
          <div className="h">
            缓存命中 {data.today.cached}
            {trend && (
              <span style={{ color: trend.up ? "var(--ok)" : "var(--warn)" }}> · 环比 {trend.text}</span>
            )}
          </div>
        </div>
        <div className="card">
          <div className="k">今日成本</div>
          <div className="v">{data.today.has_data ? money(data.today.cost_cny) : UNKNOWN}</div>
          <Spark values={data.series.map((s) => s.cost_cny)} />
          <div className="h">
            {data.window.has_data
              ? `${data.window.days} 天合计 ${money(data.window.cost_cny)}`
              : "窗口内无调用记录"}
          </div>
        </div>
        <div className="card">
          <div className="k">成功率</div>
          <div className="v">{pct(data.today.success_rate)}</div>
          <div className="h">
            {data.today.has_data
              ? `今日错误 ${data.today.errors}`
              : "今日无调用 —— 成功率未知"}
            {errRate !== null && ` · 窗口内错误率 ${errRate.toFixed(1)}%`}
          </div>
        </div>
        <div className="card">
          <div className="k">P95 延迟</div>
          <div className="v">{ms(data.today.p95_latency_ms)}</div>
          <div className="h">
            {data.today.has_data
              ? `均值 ${ms(data.today.avg_latency_ms)}`
              : "今日无调用 —— 延迟未知"}
          </div>
        </div>
        <div className="card">
          <div className="k">缓存命中率</div>
          <div className="v">{pct(data.today.cache_hit_rate)}</div>
          <div className="h">
            {data.today.has_data
              ? `命中 ${data.today.cached} / 请求 ${data.today.requests}`
              : "今日无调用 —— 命中率未知"}
          </div>
        </div>
        <div className="card">
          <div className="k">窗口请求</div>
          <div className="v">{data.window.requests}</div>
          <div className="h">{data.window.days} 天 · 未确认告警 {data.open_alerts}</div>
        </div>
      </div>

      <div className="panel">
        <h2>请求量</h2>
        <AreaChart
          data={requestsSeries}
          format={(v) => `${v} 次`}
          stroke="var(--accent)"
          fill="rgba(88, 166, 255, 0.16)"
        />
      </div>

      <div className="panel">
        <h2>成本（元）</h2>
        <AreaChart
          data={costSeries}
          format={(v) => money(v)}
          stroke="#d29922"
          fill="rgba(210, 153, 34, 0.14)"
        />
        <div className="sub" style={{ marginTop: 6 }}>
          成本曲线与请求量不同形，说明单价或 token 长度在变——这两条线是分开看的。
        </div>
      </div>

      <div className="split">
        <div className="panel">
          <h2>Top 模型</h2>
          <BarList
            items={data.top_models.map((m) => ({
              label: m.model,
              value: m.requests,
              hint: money(m.cost_cny),
            }))}
            format={(v) => `${v} 次`}
            empty="窗口内没有调用"
          />
        </div>

        <div className="panel">
          <h2>按团队</h2>
          <BarList
            items={data.by_team.map((t) => ({
              label: t.team,
              value: t.requests,
              hint: `${((t.requests / windowRequests) * 100).toFixed(0)}%`,
              tone: "#3fb950",
            }))}
            format={(v) => `${v} 次`}
            empty="窗口内没有调用"
          />
        </div>
      </div>

      <div className="panel">
        <h2>最近错误</h2>
        {data.recent_errors.length === 0 ? (
          <div className="empty">
            {data.window.has_data ? "窗口内没有错误" : "窗口内没有调用（不是「没有错误」）"}
          </div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>模型</th>
                <th>错误码</th>
                <th>request_id</th>
              </tr>
            </thead>
            <tbody>
              {data.recent_errors.map((e) => (
                <tr key={e.request_id}>
                  <td className="mono">{shortTime(e.created_at)}</td>
                  <td className="mono">{e.model}</td>
                  <td>
                    <span className="badge err">{e.code ?? "unknown"}</span>
                  </td>
                  <td className="mono">
                    <a href={`/traces/${e.request_id}`}>{e.request_id.slice(0, 18)}…</a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}