"use client";

import { useCallback, useEffect, useState } from "react";
import { adminGet, money, ms, shortTime } from "@/lib/api";

type Item = {
  request_id: string;
  provider: string;
  model_requested: string;
  model_used: string;
  input_tokens: number;
  output_tokens: number;
  cost_cny: number;
  latency_ms: number;
  ttft_ms: number | null;
  status: string;
  error_code: string | null;
  cache_hit: string | null;
  degraded: boolean;
  created_at: string;
};

const STATUSES = ["", "ok", "cached", "fallback", "error"];

function statusBadge(item: Item) {
  if (item.status === "cached") return <span className="badge blue">cached {item.cache_hit}</span>;
  if (item.status === "error") return <span className="badge err">{item.error_code ?? "error"}</span>;
  if (item.status === "fallback") return <span className="badge warn">fallback</span>;
  return <span className="badge ok">ok</span>;
}

export default function UsagePage() {
  const [items, setItems] = useState<Item[]>([]);
  const [status, setStatus] = useState("");
  const [onlyCached, setOnlyCached] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    const params = new URLSearchParams({ limit: "80" });
    if (status) params.set("status", status);
    if (onlyCached) params.set("only_cached", "true");
    adminGet<{ items: Item[] }>(`/usage?${params.toString()}`)
      .then((d) => {
        setItems(d.items);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, [status, onlyCached]);

  useEffect(() => {
    load();
  }, [load]);

  const totalCost = items.reduce((sum, i) => sum + i.cost_cny, 0);
  const cached = items.filter((i) => i.cache_hit).length;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>调用明细</h1>
          <div className="sub">
            最近 {items.length} 条 · 合计 {money(totalCost)} · 缓存命中 {cached} 条
          </div>
        </div>
        <button className="ghost" onClick={load}>
          刷新
        </button>
      </div>

      <div className="panel">
        <div className="row">
          <div>
            <label>状态</label>
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s === "" ? "全部" : s}
                </option>
              ))}
            </select>
          </div>
          <div style={{ flex: "0 0 auto", minWidth: 0 }}>
            <label>只看缓存命中</label>
            <input
              type="checkbox"
              checked={onlyCached}
              onChange={(e) => setOnlyCached(e.target.checked)}
              style={{ width: 18, height: 18, marginTop: 6 }}
            />
          </div>
        </div>
      </div>

      {error && (
        <div className="panel">
          <div className="err mono">{error}</div>
        </div>
      )}

      <div className="panel">
        {items.length === 0 ? (
          <div className="empty">没有记录</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>状态</th>
                <th>模型</th>
                <th>tokens</th>
                <th>成本</th>
                <th>延迟</th>
                <th>request_id</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.request_id}>
                  <td className="mono">{shortTime(item.created_at)}</td>
                  <td>{statusBadge(item)}</td>
                  <td className="mono">
                    {item.model_requested}
                    {item.model_used && item.model_used !== item.model_requested ? (
                      <span className="sub"> → {item.model_used}</span>
                    ) : null}
                  </td>
                  <td className="mono">
                    {item.input_tokens}/{item.output_tokens}
                  </td>
                  <td className="mono">{money(item.cost_cny)}</td>
                  <td className="mono">
                    {ms(item.latency_ms)}
                    {item.ttft_ms ? <span className="sub"> ttft {item.ttft_ms}</span> : null}
                  </td>
                  <td className="mono">
                    <a href={`/traces/${item.request_id}`}>{item.request_id.slice(0, 16)}…</a>
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