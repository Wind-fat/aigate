"use client";

import { useCallback, useEffect, useState } from "react";
import { adminGet, UNKNOWN } from "@/lib/api";

/**
 * 运行状态。值班打开的第一个页面不是日志，而是这里。
 *
 * 每个数字都对应一句"出事时先看它"：
 *   数据库延迟  → SQLite 锁 / PG 连接池
 *   记账队列    → 落库在排队（queued）还是有上次崩溃的尾巴（spooled）
 *   熔断        → 哪家被判死了、多久后自动探测
 *   未确认告警  → 预算类问题，点进去看处置
 *
 * 不做图表：这一页要的是"扫一眼就知道"，趋势在概览页。
 */

type Provider = {
  name: string;
  enabled: boolean;
  configured: boolean;
  key_source: string;
  /**
   * null = 未知。熔断快照失败时后端不再回落成 `{open:false}`，
   * 前端必须把这三态分开：正常 / 已熔断 / 未知。
   */
  circuit: { open?: boolean; consecutive_failures?: number; opened_times?: number } | null;
};

type Health = {
  version: string;
  env: string;
  uptime_s: number | null;
  db: { ok: boolean; latency_ms: number | null; backend: string };
  breaker: {
    backend: string;
    threshold: number;
    cooldown_s: number;
    state: Record<string, unknown>;
    /** null = 快照成功；字符串 = 读不出来，此时所有熔断结论都是未知 */
    error?: string | null;
  };
  providers: Provider[];
  metering: Record<string, number | string | boolean>;
  counters: Record<string, number>;
  alerts: { open: number; items: { level: string; scope: string; metric: string; message: string }[] };
  counts: { teams: number; projects: number };
};

/** 只有真的 number 才是数字；缺字段是未知，不是 0。 */
function counter(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

function countText(value: unknown): string {
  const n = counter(value);
  return n === null ? UNKNOWN : String(n);
}

function uptime(seconds: number | null): string {
  if (!seconds) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

export default function OpsPage() {
  const [data, setData] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    adminGet<Health>("/ops-health")
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [load]);

  if (error) {
    return (
      <div className="panel">
        <h2>连不上网关</h2>
        <div className="sub" style={{ marginBottom: 12 }}>{error}</div>
        <div className="sub" style={{ marginBottom: 12 }}>
          这一页在拿不到数据时不会给任何「正常」结论。
        </div>
        <button className="ghost" onClick={load}>
          重试
        </button>
      </div>
    );
  }
  if (!data) return <div className="empty">加载中…</div>;

  // 熔断：三态。`breaker.error` 或 state.__error__ 有值 = 快照没读到 = 全部未知。
  const breakerError =
    data.breaker.error ??
    (typeof data.breaker.state?.__error__ === "string" ? data.breaker.state.__error__ : null);
  const openCircuits = data.providers.filter((p) => p.circuit?.open === true);
  const unknownCircuits = data.providers.filter((p) => p.circuit === null).length;
  const unconfigured = data.providers.filter((p) => p.enabled && !p.configured);

  // 记账：`enabled` 是后端的显式信号。未启用时 queued/written/spooled 根本不存在，
  // 不能 `Number(undefined ?? 0)` 变成"0 积压、0 残留"。
  const meteringEnabled = data.metering.enabled === true;
  const queued = meteringEnabled ? counter(data.metering.queued) : null;
  const spooled = meteringEnabled ? counter(data.metering.spooled) : null;
  const written = meteringEnabled ? counter(data.metering.written) : null;
  const hitRate =
    data.counters.cache_served > 0
      ? ((data.counters.cache_hits / data.counters.cache_served) * 100).toFixed(1)
      : null;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>运行状态</h1>
          <div className="sub">
            v{data.version} · {data.env} · 已运行 {uptime(data.uptime_s)} · 每 5 秒刷新
          </div>
        </div>
        <button className="ghost" onClick={load}>
          刷新
        </button>
      </div>

      <div className="cards">
        <div className="card">
          <div className="k">数据库</div>
          <div className="v" style={{ color: data.db.ok ? "var(--ok)" : "var(--err)" }}>
            {data.db.ok ? `${data.db.latency_ms} ms` : "不可用"}
          </div>
          <div className="h">{data.db.backend}</div>
        </div>
        <div className="card">
          <div className="k">记账队列</div>
          {meteringEnabled ? (
            <>
              <div className="v">{queued === null ? UNKNOWN : queued}</div>
              <div className="h">
                已写 {countText(written)} 行 · spool 残留 {countText(spooled)}
              </div>
            </>
          ) : (
            <>
              <div className="v">
                <span className="badge">未启用</span>
              </div>
              <div className="h">记账没开：调用不会写进账单，队列数字无从谈起</div>
            </>
          )}
        </div>
        <div className="card">
          <div className="k">熔断</div>
          {breakerError ? (
            <>
              <div className="v">
                <span className="badge">未知</span>
              </div>
              <div className="h">
                快照读取失败：{breakerError}
                <button className="ghost tiny" onClick={load} style={{ marginLeft: 8 }}>
                  重试
                </button>
              </div>
            </>
          ) : openCircuits.length > 0 ? (
            <>
              <div className="v" style={{ color: "var(--err)" }}>
                {openCircuits.length} 家
              </div>
              <div className="h">
                {data.breaker.backend} · 阈值 {data.breaker.threshold} 次 / 冷却{" "}
                {data.breaker.cooldown_s}s
              </div>
            </>
          ) : data.providers.length === 0 ? (
            <>
              <div className="v">
                <span className="badge">未知</span>
              </div>
              <div className="h">没有已配置的供应商，无法判断熔断状态</div>
            </>
          ) : (
            <>
              <div className="v" style={{ color: "var(--ok)" }}>
                全部正常
              </div>
              <div className="h">
                {data.providers.length} 家快照均读到且未熔断 · {data.breaker.backend} · 阈值{" "}
                {data.breaker.threshold} 次 / 冷却 {data.breaker.cooldown_s}s
              </div>
            </>
          )}
        </div>
        <div className="card">
          <div className="k">未确认告警</div>
          <div className="v" style={{ color: data.alerts.open ? "var(--warn)" : undefined }}>
            {data.alerts.open}
          </div>
          <div className="h">
            {data.counts.teams} 个团队 · {data.counts.projects} 个项目
          </div>
        </div>
        <div className="card">
          <div className="k">缓存命中率</div>
          <div className="v">{hitRate ? `${hitRate}%` : "—"}</div>
          <div className="h">
            命中 {data.counters.cache_hits} / 查询 {data.counters.cache_served}
          </div>
        </div>
        <div className="card">
          <div className="k">上游异常</div>
          <div className="v">{data.counters.upstream_errors}</div>
          <div className="h">
            降级 {data.counters.fallbacks} · 限流 {data.counters.rate_limited}
          </div>
        </div>
      </div>

      {unconfigured.length > 0 && (
        <div className="panel" style={{ borderColor: "#5a3a1f" }}>
          <h2>有 {unconfigured.length} 家供应商缺密钥</h2>
          <div className="sub">
            {unconfigured.map((p) => p.name).join("、")} —— 路由里如果指到它们，请求会 502
            `provider_unconfigured`。去「供应商」页录入密钥。
          </div>
        </div>
      )}

      <div className="panel">
        <h2>上游</h2>
        {breakerError && (
          <div className="sub" style={{ marginBottom: 8 }}>
            熔断快照读取失败
            {unknownCircuits > 0 ? `（${unknownCircuits} 家状态未知）` : ""}
            ——「熔断」列显示「未知」的都不是"闭合"，是查不到。
          </div>
        )}
        {data.providers.length === 0 ? (
          <div className="empty">没有已配置的供应商</div>
        ) : (
        <table>
          <thead>
            <tr>
              <th>供应商</th>
              <th>配置</th>
              <th>密钥来源</th>
              <th>熔断</th>
              <th>连续失败</th>
              <th>累计打开</th>
            </tr>
          </thead>
          <tbody>
            {data.providers.map((p) => (
              <tr key={p.name}>
                <td className="mono">{p.name}</td>
                <td>
                  {!p.enabled ? (
                    <span className="badge warn">已停用</span>
                  ) : p.configured ? (
                    <span className="badge ok">就绪</span>
                  ) : (
                    <span className="badge err">缺密钥</span>
                  )}
                </td>
                <td className="sub">{p.key_source}</td>
                <td>
                  {p.circuit === null ? (
                    <span className="badge">未知</span>
                  ) : p.circuit.open ? (
                    <span className="badge err">已熔断</span>
                  ) : (
                    <span className="sub">闭合</span>
                  )}
                </td>
                <td className="mono">
                  {p.circuit === null ? UNKNOWN : (p.circuit.consecutive_failures ?? 0)}
                </td>
                <td className="mono">
                  {p.circuit === null ? UNKNOWN : (p.circuit.opened_times ?? 0)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        )}
      </div>

      <div className="split">
        <div className="panel">
          <h2>记账</h2>
          {!meteringEnabled && (
            <div className="sub" style={{ marginBottom: 8 }}>
              记账未启用（writer 未配置）：下面的字段不会出现，也没有 0 可以看。
            </div>
          )}
          <table>
            <tbody>
              {Object.entries(data.metering)
                .filter(([k]) => k !== "enabled" && k !== "note")
                .map(([k, v]) => (
                  <tr key={k}>
                    <td className="mono">{k}</td>
                    <td className="mono">{String(v)}</td>
                  </tr>
                ))}
            </tbody>
          </table>
          <div className="sub" style={{ marginTop: 8 }}>
            queued 长期不降 = 落库跟不上；spooled &gt; 0 且不变 = 上次崩溃留下的尾巴，重启会回放。
          </div>
        </div>

        <div className="panel">
          <h2>告警</h2>
          {data.alerts.items.length === 0 ? (
            <div className="empty">没有未确认告警</div>
          ) : (
            <div className="stack">
              {data.alerts.items.map((a) => (
                <div key={`${a.scope}-${a.metric}`} className="chain-row" style={{ gap: 10 }}>
                  <span className={`badge ${a.level === "critical" ? "err" : "warn"}`}>{a.level}</span>
                  <span className="mono">{a.message}</span>
                </div>
              ))}
            </div>
          )}
          <div className="sub" style={{ marginTop: 8 }}>
            处置：提额或暂停项目（「预算配额」页），确认后横条消失。
          </div>
        </div>
      </div>
    </>
  );
}