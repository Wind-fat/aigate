"use client";

import { useCallback, useEffect, useState } from "react";
import { adminGet, money, ms, pct, shortTime } from "@/lib/api";
import { AreaChart, BarList, Spark } from "@/components/charts";

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
  success_rate: number;
  cache_hit_rate: number;
  cost_cny: number;
  avg_latency_ms: number;
  p95_latency_ms: number;
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
  const [data, setData] = useState<Overview | null>(null);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [days, setDays] = useState(7);

  const load = useCallback(() => {
    adminGet<Overview>(`/overview?days=${days}`)
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
    adminGet<{ items: Alert[] }>("/alerts?limit=5")
      .then((d) => setAlerts(d.items))
      .catch(() => undefined);
  }, [days]);

  useEffect(() => {
    load();
    const timer = setInterval(load, 10_000);
    return () => clearInterval(timer);
  }, [load]);

  if (error) {
    return (
      <div className="panel">
        <h2>连接失败</h2>
        <p className="sub">
          {error}
          <br />
          检查网关是否在 8080 运行，以及 <code>console/.env.local</code> 里的 token 是否匹配。
        </p>
      </div>
    );
  }

  if (!data) return <div className="empty">加载中…</div>;

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
  const errRate = data.window.requests ? (data.window.errors / data.window.requests) * 100 : 0;

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
          <button className="ghost" onClick={load}>
            刷新
          </button>
        </div>
      </div>

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
          <div className="v">{money(data.today.cost_cny)}</div>
          <Spark values={data.series.map((s) => s.cost_cny)} />
          <div className="h">{data.window.days} 天合计 {money(data.window.cost_cny)}</div>
        </div>
        <div className="card">
          <div className="k">成功率</div>
          <div className="v">{pct(data.today.success_rate)}</div>
          <div className="h">
            窗口内错误 {data.window.errors} · {errRate.toFixed(1)}%
          </div>
        </div>
        <div className="card">
          <div className="k">P95 延迟</div>
          <div className="v">{ms(data.today.p95_latency_ms)}</div>
          <div className="h">均值 {ms(data.today.avg_latency_ms)}</div>
        </div>
        <div className="card">
          <div className="k">缓存命中率</div>
          <div className="v">{pct(data.today.cache_hit_rate)}</div>
          <div className="h">缓存命中仍记一条 cost=0 的账单</div>
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
          <div className="empty">窗口内没有错误</div>
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