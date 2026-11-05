"use client";

import { useCallback, useEffect, useState } from "react";
import { adminGet, money, ms, pct, shortTime } from "@/lib/api";

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
};

export default function OverviewPage() {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    adminGet<Overview>("/overview?days=7")
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

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

  const max = Math.max(1, ...data.series.map((s) => s.requests));

  return (
    <>
      <div className="page-head">
        <div>
          <h1>概览</h1>
          <div className="sub">最近 {data.window.days} 天 · 每 10 秒自动刷新</div>
        </div>
        <button className="ghost" onClick={load}>
          刷新
        </button>
      </div>

      <div className="cards">
        <div className="card">
          <div className="k">今日请求</div>
          <div className="v">{data.today.requests}</div>
          <div className="h">缓存命中 {data.today.cached}</div>
        </div>
        <div className="card">
          <div className="k">今日成本</div>
          <div className="v">{money(data.today.cost_cny)}</div>
          <div className="h">7 天 {money(data.window.cost_cny)}</div>
        </div>
        <div className="card">
          <div className="k">成功率</div>
          <div className="v">{pct(data.today.success_rate)}</div>
          <div className="h">错误 {data.today.errors}</div>
        </div>
        <div className="card">
          <div className="k">P95 延迟</div>
          <div className="v">{ms(data.today.p95_latency_ms)}</div>
          <div className="h">均值 {ms(data.today.avg_latency_ms)}</div>
        </div>
        <div className="card">
          <div className="k">缓存命中率</div>
          <div className="v">{pct(data.today.cache_hit_rate)}</div>
          <div className="h">命中成本为 0</div>
        </div>
      </div>

      <div className="panel">
        <h2>请求量趋势</h2>
        {data.series.length === 0 ? (
          <div className="empty">还没有数据，先去工作台发一个请求</div>
        ) : (
          <div className="bars">
            {data.series.map((s) => (
              <div
                className="bar-wrap"
                key={s.date}
                title={`${s.date} · ${s.requests} 次 · ${money(s.cost_cny)}`}
              >
                <div className="bar-value">{s.requests}</div>
                <div
                  className={`bar${s.requests === 0 ? " zero" : ""}`}
                  style={{ height: `${Math.max(3, (s.requests / max) * 100)}%` }}
                />
                <div className="bar-label">{s.date.slice(5)}</div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="panel">
        <h2>Top 模型</h2>
        {data.top_models.length === 0 ? (
          <div className="empty">暂无</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>模型</th>
                <th>请求</th>
                <th>成本</th>
              </tr>
            </thead>
            <tbody>
              {data.top_models.map((m) => (
                <tr key={m.model}>
                  <td className="mono">{m.model}</td>
                  <td className="mono">{m.requests}</td>
                  <td className="mono">{money(m.cost_cny)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="panel">
        <h2>最近错误</h2>
        {data.recent_errors.length === 0 ? (
          <div className="empty">没有错误 🎉</div>
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