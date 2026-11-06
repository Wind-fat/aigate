"use client";

import { useCallback, useEffect, useState } from "react";
import { adminGet, adminPost, money, ms } from "@/lib/api";

type Models = { policies: { match_model: string; candidates: { provider: string; model: string }[] }[] };
type Projects = { items: { id: string; slug: string; team: string }[] };

type Meta = {
  request_id: string;
  provider: string;
  model_used: string;
  degraded: boolean;
  cache_hit: string | null;
  pii_masked: number;
  cost_cny: number;
  saved_cny?: number;
  latency_ms: Record<string, number>;
};

const KEY_STORAGE = "aigate.playground.key";

export default function PlaygroundPage() {
  const [models, setModels] = useState<string[]>(["chat-default"]);
  const [model, setModel] = useState("chat-default");
  const [prompt, setPrompt] = useState("用一句话解释什么是 LLM 网关");
  const [temperature, setTemperature] = useState(0);
  const [stream, setStream] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [projectId, setProjectId] = useState("");

  const [answer, setAnswer] = useState("");
  const [meta, setMeta] = useState<Meta | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setApiKey(window.localStorage.getItem(KEY_STORAGE) ?? "");
    adminGet<Models>("/models")
      .then((d) => {
        const names = d.policies.map((p) => p.match_model).filter((n) => n !== "__default__");
        if (names.length) {
          setModels(names);
          setModel(names.includes("chat-default") ? "chat-default" : names[0]);
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
        `/keys?project_id=${projectId}&name=console-playground`,
      );
      setApiKey(res.api_key);
      window.localStorage.setItem(KEY_STORAGE, res.api_key);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [projectId]);

  function applyMeta(next: Partial<Meta>) {
    setMeta((prev) => ({ ...(prev ?? ({} as Meta)), ...next }) as Meta);
  }

  async function send() {
    if (!apiKey) {
      setError("先签发一个 Key（右上角按钮）");
      return;
    }
    setBusy(true);
    setError(null);
    setAnswer("");
    setMeta(null);

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
        body: JSON.stringify({
          model,
          messages: [{ role: "user", content: prompt }],
          temperature,
          stream,
        }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const err = body?.error ?? {};
        throw new Error(err.message || err.code || `${res.status}`);
      }

      if (!stream) {
        const body = await res.json();
        setAnswer(body.choices?.[0]?.message?.content ?? "");
        setMeta(body.x_aigate);
        return;
      }

      // SSE: parse `data:` frames, and pick up the gateway's trailing `: aigate` comment.
      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let text = "";

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
                applyMeta(JSON.parse(line.slice(9)));
              } catch {
                /* ignore malformed meta */
              }
            } else if (line.startsWith("data: ")) {
              const payload = line.slice(6);
              if (payload === "[DONE]") continue;
              try {
                const chunk = JSON.parse(payload);
                if (chunk.error) throw new Error(chunk.error.code);
                const delta = chunk.choices?.[0]?.delta?.content;
                if (delta) {
                  text += delta;
                  setAnswer(text);
                }
              } catch {
                /* ignore non-JSON keep-alive frames */
              }
            }
          }
        }
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>工作台</h1>
          <div className="sub">
            {apiKey ? `Key ${apiKey.slice(0, 14)}…` : "未签发 Key"} · 经控制台同源代理
          </div>
        </div>
        <button className="ghost" onClick={issueKey} disabled={!projectId}>
          签发临时 Key
        </button>
      </div>

      <div className="panel">
        <div className="row" style={{ marginBottom: 12 }}>
          <div>
            <label>模型</label>
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              {models.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </div>
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
          <div style={{ flex: "0 0 auto", minWidth: 0 }}>
            <label>流式</label>
            <input
              type="checkbox"
              checked={stream}
              onChange={(e) => setStream(e.target.checked)}
              style={{ width: 18, height: 18, marginTop: 6 }}
            />
          </div>
          <div style={{ flex: "0 0 auto", minWidth: 0 }}>
            <button onClick={send} disabled={busy}>
              {busy ? "请求中…" : "发送"}
            </button>
          </div>
        </div>

        <label>Prompt</label>
        <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} />
        <div className="sub" style={{ marginTop: 6 }}>
          提示：temperature=0 且非流式才会走语义缓存；把模型切到 <code>failover-demo</code> 看降级。
        </div>
      </div>

      {error && (
        <div className="panel">
          <h2>错误</h2>
          <div className="err mono">{error}</div>
        </div>
      )}

      <div className="panel">
        <h2>回答</h2>
        <div className="answer">{answer || (busy ? "…" : "—")}</div>
      </div>

      {meta && (
        <div className="panel">
          <h2>x_aigate 元数据</h2>
          <div className="meta">
            <div className="item">
              <div className="k">provider</div>
              <div className="v">{meta.provider}</div>
            </div>
            <div className="item">
              <div className="k">model_used</div>
              <div className="v">{meta.model_used}</div>
            </div>
            <div className="item">
              <div className="k">成本</div>
              <div className="v">{money(meta.cost_cny)}</div>
            </div>
            <div className="item">
              <div className="k">缓存</div>
              <div className="v">{meta.cache_hit ?? "miss"}</div>
            </div>
            <div className="item">
              <div className="k">降级</div>
              <div className="v">
                <span className={`badge ${meta.degraded ? "warn" : "ok"}`}>
                  {meta.degraded ? "degraded" : "primary"}
                </span>
              </div>
            </div>
            <div className="item">
              <div className="k">上游耗时</div>
              <div className="v">{ms(meta.latency_ms?.upstream_ms ?? meta.latency_ms?.gateway_total)}</div>
            </div>
            <div className="item">
              <div className="k">request_id</div>
              <div className="v">
                <a href={`/traces/${meta.request_id}`}>{meta.request_id.slice(0, 14)}…</a>
              </div>
            </div>
            {meta.saved_cny !== undefined && (
              <div className="item">
                <div className="k">本次省下</div>
                <div className="v">{money(meta.saved_cny)}</div>
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}