"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { adminGet, money, ms, shortTime } from "@/lib/api";

type Trace = {
  request_id: string;
  prompt_preview: string;
  prompt_sha256: string;
  prompt_raw: string | null;
  prompt_version: string;
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
  persist_ms: "落库",
  __overhead__: "未计入（框架/网络）",
  gateway_total: "总耗时",
};

export default function TracePage() {
  const params = useParams<{ id: string }>();
  const [trace, setTrace] = useState<Trace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [shadows, setShadows] = useState<
    { model: string; provider: string; ok: boolean; similarity: number | null; latency_ms: number; cost_cny: number; preview: string }[]
  >([]);

  useEffect(() => {
    adminGet<Trace>(`/traces/${params.id}`)
      .then(setTrace)
      .catch((e: Error) => setError(e.message));
    adminGet<{ items: typeof shadows }>(`/shadow?request_id=${params.id}`)
      .then((d) => setShadows(d.items))
      .catch(() => undefined); // shadow data is optional; its absence is not an error
  }, [params.id]);

  if (error) {
    return (
      <div className="panel">
        <h2>没有找到这条调用链</h2>
        <div className="err mono">{error}</div>
      </div>
    );
  }

  if (!trace) return <div className="empty">加载中…</div>;

  const stages = Object.entries(trace.stage_timings ?? {}).filter(([k]) => k !== "gateway_total");
  const measured = stages.reduce((sum, [, v]) => sum + (v || 0), 0);
  // The card above says 644ms while the stages add up to 596ms. Rather than let a
  // reviewer discover that difference, show it: it is real (framework, connection
  // setup, serialisation) and it is 8% of this request.
  const overhead = Math.max(0, (trace.usage?.latency_ms ?? measured) - measured);
  const rows2: [string, number][] = overhead > 0 ? [...stages, ["__overhead__", overhead]] : stages;
  const totalMs = measured + overhead || 1;
  let cursor = 0;
  const waterfall = rows2.map(([key, value]) => {
    const left = (cursor / totalMs) * 100;
    cursor += value || 0;
    return {
      key,
      value,
      left,
      width: ((value || 0) / totalMs) * 100,
      share: Math.round(((value || 0) / totalMs) * 1000) / 10,
    };
  });

  return (
    <>
      <div className="page-head">
        <div>
          <h1>调用链</h1>
          <div className="sub mono">
            {trace.request_id} · {shortTime(trace.created_at)} · HTTP {trace.http_status}
          </div>
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <button
            className="ghost tiny"
            onClick={() => {
              navigator.clipboard?.writeText(trace.request_id).then(
                () => {
                  setCopied(true);
                  window.setTimeout(() => setCopied(false), 1500);
                },
                () => undefined,
              );
            }}
          >
            {copied ? "已复制" : "复制 request_id"}
          </button>
          <a className="sub" href="/usage">
            ← 返回明细
          </a>
        </div>
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
        <h2>耗时瀑布</h2>
        <div className="sub" style={{ marginBottom: 10 }}>
          每一段的位置是它在总耗时里的真实起点。看「卡在哪」比看「谁最长」有用：
          上游占了 90% 就是供应商的问题，缓存查找变大就是向量检索变慢了。
        </div>
        {stages.length === 0 ? (
          <div className="empty">无分段数据</div>
        ) : (
          <>
            <div className="wf-row wf-head">
              <div>阶段</div>
              <div>时间轴（总 {totalMs} ms）</div>
              <div style={{ textAlign: "right" }}>占比</div>
            </div>
            {waterfall.map((seg) => (
              <div className="wf-row" key={seg.key}>
                <div>
                  {STAGE_LABELS[seg.key] ?? seg.key}
                  <span className="sub"> {seg.value}ms</span>
                </div>
                <div className="wf-track">
                  <div
                    className={`wf-fill ${seg.key === "upstream_ms" ? "upstream" : seg.key === "__overhead__" ? "overhead" : ""}`}
                    style={{ marginLeft: `${seg.left}%`, width: `${seg.width}%` }}
                  />
                </div>
                <div style={{ textAlign: "right" }} className="mono">
                  {seg.share}%
                </div>
              </div>
            ))}
          </>
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

      {shadows.length > 0 && (
        <div className="panel">
          <h2>影子对照（不影响本次响应）</h2>
          <table>
            <thead>
              <tr>
                <th>候选</th>
                <th>结果</th>
                <th>相似度</th>
                <th>延迟</th>
                <th>影子成本</th>
              </tr>
            </thead>
            <tbody>
              {shadows.map((sh, i) => (
                <tr key={i}>
                  <td className="mono">
                    {sh.provider}/{sh.model}
                  </td>
                  <td>
                    <span className={`badge ${sh.ok ? "ok" : "err"}`}>{sh.ok ? "ok" : "failed"}</span>
                  </td>
                  <td className="mono">{sh.similarity ?? "—"}</td>
                  <td className="mono">{sh.latency_ms} ms</td>
                  <td className="mono">{money(sh.cost_cny)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="sub" style={{ marginTop: 8 }}>
            相似度是字符级比对，只用来提示“这两个答案差别很大”，不是质量评分。
          </div>
        </div>
      )}

      <div className="panel">
        <h2>Prompt（脱敏后）</h2>
        <div className="answer">{trace.prompt_preview || "—"}</div>
        {trace.prompt_raw && (
          <>
            <h2 style={{ marginTop: 14 }}>Prompt 原文（AIGATE_TRACE_PROMPT_MODE=raw）</h2>
            <div className="answer mono" style={{ whiteSpace: "pre-wrap" }}>
              {trace.prompt_raw}
            </div>
            <div className="sub" style={{ marginTop: 6 }}>
              这一段是用户实际输入，未经脱敏。只有显式打开 raw 模式才会存。
            </div>
          </>
        )}
        {trace.prompt_version && (
          <div className="sub" style={{ marginTop: 8 }}>
            prompt 版本：<span className="mono">{trace.prompt_version}</span>（参与缓存 key，改版本即失效）
          </div>
        )}
        <div className="sub" style={{ marginTop: 8 }}>
          PII 命中 {trace.pii_masked} 处 · sha256 {trace.prompt_sha256.slice(0, 24)}…
        </div>
      </div>
    </>
  );
}