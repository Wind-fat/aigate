"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { adminGet, money, ms, shortTime } from "@/lib/api";

type Trace = {
  request_id: string;
  prompt_preview: string;
  prompt_sha256: string;
  pii_masked: number;
  stage_timings: Record<string, number>;
  route_path: { attempt?: number; provider?: string; model?: string; ok?: boolean; error?: string; skipped?: string; latency_ms?: number }[];
  http_status: number;
  created_at: string;
  usage: {
    provider: string;
    model_used: string;
    input_tokens: number;
    output_tokens: number;
    cost_cny: number;
    latency_ms: number;
    ttft_ms: number | null;
    status: string;
    cache_hit: string | null;
    degraded: boolean;
  } | null;
};

const STAGE_LABELS: Record<string, string> = {
  scope_ms: "模型白名单",
  quota_ms: "配额",
  budget_ms: "预算",
  guard_ms: "脱敏",
  cache_ms: "缓存查找",
  upstream_ms: "上游调用",
  gateway_total: "总耗时",
};

export default function TracePage() {
  const params = useParams<{ id: string }>();
  const [trace, setTrace] = useState<Trace | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    adminGet<Trace>(`/traces/${params.id}`)
      .then(setTrace)
      .catch((e: Error) => setError(e.message));
  }, [params.id]);

  if (error) {
    return (
      <div className="panel">
        <h2>没有找到这条 Trace</h2>
        <div className="err mono">{error}</div>
      </div>
    );
  }

  if (!trace) return <div className="empty">加载中…</div>;

  const stages = Object.entries(trace.stage_timings ?? {});
  const maxStage = Math.max(1, ...stages.map(([, v]) => v));

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Trace</h1>
          <div className="sub mono">
            {trace.request_id} · {shortTime(trace.created_at)} · HTTP {trace.http_status}
          </div>
        </div>
        <a className="sub" href="/usage">
          ← 返回明细
        </a>
      </div>

      <div className="cards">
        <div className="card">
          <div className="k">成本</div>
          <div className="v">{money(trace.usage?.cost_cny)}</div>
        </div>
        <div className="card">
          <div className="k">总耗时</div>
          <div className="v">{ms(trace.usage?.latency_ms)}</div>
        </div>
        <div className="card">
          <div className="k">TTFT</div>
          <div className="v">{trace.usage?.ttft_ms ? `${trace.usage.ttft_ms} ms` : "—"}</div>
        </div>
        <div className="card">
          <div className="k">状态</div>
          <div className="v">
            <span
              className={`badge ${
                trace.usage?.status === "error" ? "err" : trace.usage?.status === "cached" ? "blue" : "ok"
              }`}
            >
              {trace.usage?.status ?? "unknown"}
            </span>
          </div>
          <div className="h">
            {trace.usage?.cache_hit ? `cache ${trace.usage.cache_hit}` : "cache miss"}
          </div>
        </div>
      </div>

      <div className="panel">
        <h2>耗时分解</h2>
        {stages.length === 0 ? (
          <div className="empty">无分段数据</div>
        ) : (
          stages.map(([key, value]) => (
            <div className="wf-row" key={key}>
              <div>{STAGE_LABELS[key] ?? key}</div>
              <div className="wf-track">
                <div className="wf-fill" style={{ width: `${(value / maxStage) * 100}%` }} />
              </div>
              <div style={{ textAlign: "right" }}>{value} ms</div>
            </div>
          ))
        )}
      </div>

      <div className="panel">
        <h2>路由路径</h2>
        {trace.route_path?.length ? (
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>provider</th>
                <th>model</th>
                <th>结果</th>
                <th>耗时</th>
              </tr>
            </thead>
            <tbody>
              {trace.route_path.map((hop, idx) => (
                <tr key={idx}>
                  <td className="mono">{hop.attempt ?? idx + 1}</td>
                  <td className="mono">{hop.provider ?? "—"}</td>
                  <td className="mono">{hop.model ?? "—"}</td>
                  <td>
                    {hop.skipped ? (
                      <span className="badge warn">skipped: {hop.skipped}</span>
                    ) : hop.ok ? (
                      <span className="badge ok">ok</span>
                    ) : (
                      <span className="badge err">{hop.error ?? "error"}</span>
                    )}
                  </td>
                  <td className="mono">{hop.latency_ms !== undefined ? `${hop.latency_ms} ms` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty">无路由记录（可能命中缓存）</div>
        )}
      </div>

      <div className="panel">
        <h2>Prompt（脱敏后）</h2>
        <div className="answer">{trace.prompt_preview || "—"}</div>
        <div className="sub" style={{ marginTop: 8 }}>
          PII 命中 {trace.pii_masked} 处 · sha256 {trace.prompt_sha256.slice(0, 24)}…
        </div>
      </div>
    </>
  );
}