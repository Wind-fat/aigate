"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { adminGet, adminPost, money, ms } from "@/lib/api";

/**
 * 工作台：验证「这个网关能不能给业务用」的地方。
 *
 * 三个刻意的设计：
 *
 * 1. **默认流式**。非流式看起来更整齐，但真实业务大多是流式，切到非流式再测等于没测。
 * 2. **A/B 并排**。选两个虚拟模型同时打，延迟/成本/输出并排放——换模型这件事必须能
 *    眼看着比较，而不是靠两轮回忆。
 * 3. **结果里带 request_id**。看完觉得不对劲，一键跳到调用链，而不是回去翻日志。
 *
 * Key 只存在 localStorage，并且只在本机开发环境签发（7 天过期，写清用途）。
 */

type Meta = {
  request_id: string;
  provider: string;
  model_used: string;
  degraded: boolean;
  cache_hit: string | null;
  pii_masked: number;
  cost_cny: number;
  latency_ms?: Record<string, number>;
  ttft_ms?: number | null;
};

type Models = {
  policies: { match_model: string; candidates: { provider: string; model: string }[] }[];
};
type Projects = { items: { id: string; slug: string; team: string }[] };

type Side = {
  model: string;
  text: string;
  meta: Meta | null;
  error: string | null;
  running: boolean;
  startedAt: number | null;
  firstTokenMs: number | null;
};

type HistoryItem = { prompt: string; at: number; models: string[] };

const KEY_STORAGE = "aigate.playground.key";

function blankSide(model: string): Side {
  return { model, text: "", meta: null, error: null, running: false, startedAt: null, firstTokenMs: null };
}

export default function PlaygroundPage() {
  const [models, setModels] = useState<string[]>(["chat-default"]);
  const [compare, setCompare] = useState(false);
  const [temperature, setTemperature] = useState(0);
  const [maxTokens, setMaxTokens] = useState("");
  const [useStream, setUseStream] = useState(true);
  const [system, setSystem] = useState("");
  const [prompt, setPrompt] = useState("用一句话解释什么是 LLM 网关");
  const [apiKey, setApiKey] = useState("");
  const [projectId, setProjectId] = useState("");
  const [sides, setSides] = useState<Side[]>([blankSide("chat-default")]);
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    setApiKey(window.localStorage.getItem(KEY_STORAGE) ?? "");
    // 从调用链页「在工作台重放」过来时带着 prompt / model
    const params = new URLSearchParams(window.location.search);
    const p = params.get("prompt");
    const m = params.get("model");
    if (p) setPrompt(p);
    if (m) setSides((prev) => [{ ...prev[0], model: m }, ...prev.slice(1)]);
    try {
      const saved = window.localStorage.getItem("aigate.playground.history");
      if (saved) setHistory(JSON.parse(saved));
    } catch {
      /* ignore */
    }
    adminGet<Models>("/models")
      .then((d) => {
        const names = d.policies.map((p) => p.match_model).filter((n) => n !== "__default__");
        if (names.length) {
          setModels(names);
          const first = names.includes("chat-default") ? "chat-default" : names[0];
          const second = names.find((n) => n !== first) ?? first;
          setSides([blankSide(first), blankSide(second)]);
        }
      })
      .catch(() => undefined);
    adminGet<Projects>("/projects")
      .then((d) => d.items[0] && setProjectId(d.items[0].id))
      .catch(() => undefined);
  }, []);

  const issueKey = useCallback(async () => {
    if (!projectId) return;
    try {
      const res = await adminPost<{ api_key: string }>(
        `/keys?project_id=${projectId}&name=playground-${Date.now().toString(36).slice(-5)}&environment=dev&expires_in_days=7&description=${encodeURIComponent("工作台临时调试")}`,
      );
      setApiKey(res.api_key);
      window.localStorage.setItem(KEY_STORAGE, res.api_key);
    } catch (e) {
      setSides((prev) => prev.map((s) => ({ ...s, error: (e as Error).message })));
    }
  }, [projectId]);

  function patchSide(index: number, patch: Partial<Side>) {
    setSides((prev) => prev.map((s, i) => (i === index ? { ...s, ...patch } : s)));
  }

  async function runOne(index: number, model: string, signal: AbortSignal) {
    const messages = [
      ...(system.trim() ? [{ role: "system", content: system.trim() }] : []),
      { role: "user", content: prompt },
    ];
    const body: Record<string, unknown> = { model, messages, temperature, stream: useStream };
    if (maxTokens.trim()) body.max_tokens = Number(maxTokens);

    const t0 = Date.now();
    patchSide(index, {
      running: true,
      text: "",
      meta: null,
      error: null,
      startedAt: t0,
      firstTokenMs: null,
    });

    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
      body: JSON.stringify(body),
      signal,
    });

    if (!res.ok) {
      const payload = await res.json().catch(() => ({}));
      const err = payload?.error ?? {};
      throw new Error(err.message || err.code || `${res.status}`);
    }

    if (!useStream) {
      const payload = await res.json();
      patchSide(index, {
        text: payload.choices?.[0]?.message?.content ?? "",
        meta: payload.x_aigate ?? null,
        running: false,
        firstTokenMs: Date.now() - t0,
      });
      return;
    }

    const reader = res.body!.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let text = "";
    let ttft: number | null = null;

    // SSE 帧以空行分隔；`: aigate {...}` 是网关自己的元数据注释，不是协议的一部分。
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";

      for (const frame of frames) {
        for (const line of frame.split("\n")) {
          if (line.startsWith(": aigate ")) {
            try {
              patchSide(index, { meta: JSON.parse(line.slice(9)) });
            } catch {
              /* 元数据坏了不影响回答 */
            }
            continue;
          }
          if (!line.startsWith("data: ")) continue;
          const raw = line.slice(6);
          if (raw === "[DONE]") continue;
          let chunk: {
            error?: { code?: string; message?: string };
            choices?: { delta?: { content?: string } }[];
          };
          try {
            chunk = JSON.parse(raw);
          } catch {
            continue; // keep-alive 或非 JSON 帧
          }
          if (chunk.error) throw new Error(chunk.error.message || chunk.error.code || "stream_error");
          const delta = chunk.choices?.[0]?.delta?.content;
          if (delta) {
            if (ttft === null) ttft = Date.now() - t0;
            text += delta;
            patchSide(index, { text, firstTokenMs: ttft });
          }
        }
      }
    }
    patchSide(index, { running: false, text });
  }

  async function send() {
    if (!apiKey) {
      setSides((prev) => prev.map((s) => ({ ...s, error: "先点右上角「签发临时 Key」" })));
      return;
    }
    setBusy(true);
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    const active = compare ? sides : [sides[0]];
    const tasks = active.map((side, i) =>
      runOne(i, side.model, controller.signal).catch((e: unknown) => {
        const message = e instanceof Error ? e.message : String(e);
        if (!message.includes("abort")) patchSide(i, { error: message, running: false });
      }),
    );

    try {
      await Promise.all(tasks);
    } finally {
      setBusy(false);
      const item: HistoryItem = { prompt, at: Date.now(), models: active.map((s) => s.model) };
      const next = [item, ...history].slice(0, 10);
      setHistory(next);
      window.localStorage.setItem("aigate.playground.history", JSON.stringify(next));
    }
  }

  function stop() {
    abortRef.current?.abort();
    setBusy(false);
    setSides((prev) => prev.map((s) => (s.running ? { ...s, running: false } : s)));
  }

  const shown = compare ? sides : [sides[0]];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>工作台</h1>
          <div className="sub">
            {apiKey ? `Key ${apiKey.slice(0, 14)}…（仅本机浏览器保存）` : "未签发 Key"} · 经控制台同源代理
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {busy && (
            <button className="ghost" onClick={stop}>
              停止
            </button>
          )}
          <button className="ghost" onClick={issueKey} disabled={!projectId}>
            签发临时 Key
          </button>
        </div>
      </div>

      <div className="panel">
        <div className="row" style={{ alignItems: "flex-end", flexWrap: "wrap" }}>
          <div>
            <label>{compare ? "模型 A" : "模型"}</label>
            <select
              value={sides[0]?.model ?? ""}
              onChange={(e) => patchSide(0, { model: e.target.value })}
            >
              {models.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </div>
          {compare && (
            <div>
              <label>模型 B</label>
              <select
                value={sides[1]?.model ?? ""}
                onChange={(e) => patchSide(1, { model: e.target.value })}
              >
                {models.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </div>
          )}
          <div>
            <label>temperature</label>
            <input
              type="number"
              step="0.1"
              min="0"
              max="2"
              value={temperature}
              onChange={(e) => setTemperature(Number(e.target.value))}
            />
          </div>
          <div>
            <label>max_tokens（留空=默认）</label>
            <input value={maxTokens} onChange={(e) => setMaxTokens(e.target.value)} placeholder="512" />
          </div>
          <div style={{ flex: "0 0 auto", minWidth: 0 }}>
            <label>流式</label>
            <input
              type="checkbox"
              checked={useStream}
              onChange={(e) => setUseStream(e.target.checked)}
              style={{ width: 18, height: 18, marginTop: 6 }}
            />
          </div>
          <div style={{ flex: "0 0 auto", minWidth: 0 }}>
            <label>对比</label>
            <input
              type="checkbox"
              checked={compare}
              onChange={(e) => setCompare(e.target.checked)}
              style={{ width: 18, height: 18, marginTop: 6 }}
            />
          </div>
          <div style={{ flex: "0 0 auto", minWidth: 0, marginLeft: "auto" }}>
            <button onClick={send} disabled={busy}>
              {busy ? "请求中…" : "发送"}
            </button>
          </div>
        </div>

        <div style={{ marginTop: 12 }}>
          <label>System（可选，会作为第一条 system 消息发出）</label>
          <input
            value={system}
            onChange={(e) => setSystem(e.target.value)}
            placeholder="你是一名严谨的客服，回答不超过 3 句话。"
          />
        </div>

        <div style={{ marginTop: 10 }}>
          <label>User</label>
          <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={4} />
        </div>
        <div className="sub" style={{ marginTop: 6 }}>
          temperature=0 且非流式才会命中语义缓存；切到 <code>failover-demo</code> 看降级，切到{" "}
          <code>stream-interrupt</code> 看流式中断。
        </div>
      </div>

      <div className={compare ? "split" : ""}>
        {shown.map((side, index) => (
          <div className="panel" key={`${side.model}-${index}`}>
            <h2 style={{ textTransform: "none" }}>
              <span className="mono">{side.model}</span>
              {side.meta && (
                <span className="sub" style={{ marginLeft: 10 }}>
                  {side.meta.provider}/{side.meta.model_used}
                </span>
              )}
            </h2>

            <div className="answer" style={{ minHeight: 120 }}>
              {side.error ? (
                <span className="err mono">{side.error}</span>
              ) : side.text ? (
                side.text
              ) : side.running ? (
                "…"
              ) : (
                <span className="sub">—</span>
              )}
            </div>

            {side.meta && (
              <div className="meta" style={{ marginTop: 10 }}>
                {/* 流式响应里网关只给 ttft 与总量，没有分段耗时：不要用 0ms 假装知道 */}
                <div className="item">
                  <div className="k">首字</div>
                  <div className="v">
                    {side.meta.ttft_ms != null
                      ? ms(side.meta.ttft_ms)
                      : side.firstTokenMs != null
                        ? ms(side.firstTokenMs)
                        : "—"}
                  </div>
                </div>
                <div className="item">
                  <div className="k">总耗时</div>
                  <div className="v">
                    {side.meta.latency_ms?.gateway_total != null
                      ? ms(side.meta.latency_ms.gateway_total)
                      : "—"}
                  </div>
                </div>
                <div className="item">
                  <div className="k">成本</div>
                  <div className="v">{money(side.meta.cost_cny ?? 0)}</div>
                </div>
                <div className="item">
                  <div className="k">缓存</div>
                  <div className="v">{side.meta.cache_hit ?? "miss"}</div>
                </div>
                <div className="item">
                  <div className="k">状态</div>
                  <div className="v">
                    <span className={`badge ${side.meta.degraded ? "warn" : "ok"}`}>
                      {side.meta.degraded ? "degraded" : "primary"}
                    </span>
                  </div>
                </div>
                <div className="item">
                  <div className="k">request_id</div>
                  <div className="v">
                    <a href={`/traces/${side.meta.request_id}`}>
                      {side.meta.request_id.slice(0, 12)}…
                    </a>
                  </div>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      {history.length > 0 && (
        <div className="panel">
          <h2>本次会话的请求</h2>
          <div className="barlist">
            {history.map((h) => (
              <div
                key={h.at}
                className="barline"
                style={{ cursor: "pointer" }}
                onClick={() => setPrompt(h.prompt)}
                title="点击回填到输入框"
              >
                <div className="barline-label">{new Date(h.at).toLocaleTimeString()}</div>
                <div className="sub" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                  {h.prompt.slice(0, 60)}
                </div>
                <div className="mono sub">{h.models.join(" vs ")}</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  );
}