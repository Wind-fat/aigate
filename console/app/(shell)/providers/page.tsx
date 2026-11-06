"use client";

import { useCallback, useEffect, useState } from "react";
import { adminGet, adminPost, adminPut } from "@/lib/api";
import { useSession } from "@/lib/session";
import { toast } from "@/lib/toast";

type Model = {
  model: string;
  input_price_per_1k: number;
  output_price_per_1k: number;
  context_window: number;
  enabled: boolean;
};

type Provider = {
  name: string;
  label: string;
  base_url: string;
  api_key_env: string;
  enabled: boolean;
  configured: boolean | null;
  models: Model[];
  used_by_routes?: string[];
  is_mock?: boolean;
};

type TestResult = { ok: boolean; status: number | null; latency_ms: number; detail: string };

const SOURCE_HINT: Record<string, string> = {
  dashscope: "阿里云百炼 · 通义千问系列",
  deepseek: "DeepSeek 开放平台",
  vllm: "自建 / 私有化推理端点",
  mock: "离线 Mock，不发网络请求（演示与测试用）",
};

/**
 * 供应商：这个网关到底在跟谁说话。
 *
 * 页面的核心不是"配置"，是**可被证明**：每个来源都能一键真打一次 `/models`，
 * 401 / 404 / 超时都会原样返回并给出下一步。密钥本身永远不显示也不入库——
 * 这里只记录它在哪个环境变量里。
 */
export default function ProvidersPage() {
  const { can } = useSession();
  const canWrite = can("providers:write");
  const [items, setItems] = useState<Provider[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<Partial<Provider>>({});
  const [testing, setTesting] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, TestResult>>({});
  const [error, setError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");

  const load = useCallback(() => {
    adminGet<{ items: Provider[] }>("/providers")
      .then((d) => {
        setItems(d.items);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function save(name: string) {
    try {
      await adminPut(`/providers/${name}`, {
        label: draft.label,
        base_url: draft.base_url,
        api_key_env: draft.api_key_env,
      });
      toast(`${name} 已保存`);
      setEditing(null);
      load();
    } catch (e) {
      toast((e as Error).message, "err");
    }
  }

  async function toggle(p: Provider) {
    try {
      await adminPut(`/providers/${p.name}`, { enabled: !p.enabled });
      toast(`${p.name} ${p.enabled ? "已停用" : "已启用"}`);
      load();
    } catch (e) {
      toast((e as Error).message, "err");
    }
  }

  async function test(name: string) {
    setTesting(name);
    try {
      const res = await adminPost<TestResult>(`/providers/${name}/test`);
      setResults((prev) => ({ ...prev, [name]: res }));
      toast(res.ok ? `${name} 连通` : `${name} 不可用：${res.detail}`, res.ok ? "ok" : "err");
    } catch (e) {
      toast((e as Error).message, "err");
    } finally {
      setTesting(null);
    }
  }

  async function create() {
    const name = newName.trim().toLowerCase();
    if (!name) return;
    try {
      await adminPost(`/providers?name=${encodeURIComponent(name)}`);
      toast(`已新增 ${name}，接着填 base_url 和密钥变量名`);
      setNewName("");
      load();
    } catch (e) {
      toast((e as Error).message, "err");
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>供应商</h1>
          <div className="sub">
            业务代码只认虚拟模型名；这里决定它最终打到哪一家。密钥不入库，只记录它在哪个环境变量里。
          </div>
        </div>
        <button className="ghost" onClick={load}>
          刷新
        </button>
      </div>

      {error && (
        <div className="panel">
          <div className="err mono">{error}</div>
        </div>
      )}

      {canWrite && (
        <div className="panel">
          <h2>接入新供应商</h2>
          <div className="row">
            <div>
              <label>标识（小写，路由里用这个名字）</label>
              <input
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="azure-east"
              />
            </div>
            <div style={{ flex: "0 0 auto", minWidth: 0 }}>
              <button onClick={create} disabled={!newName.trim()}>
                新增
              </button>
            </div>
          </div>
          <div className="sub" style={{ marginTop: 8 }}>
            新增后给它填 base_url、密钥所在的变量名，再到「模型路由」里把候选指过来。模型单价在 model_prices 里维护。
          </div>
        </div>
      )}

      {items.map((p) => {
        const isEditing = editing === p.name;
        const res = results[p.name];
        return (
          <div className="panel" key={p.name}>
            <div className="page-head" style={{ marginBottom: 10 }}>
              <div>
                <h2 style={{ margin: 0 }}>
                  <span className="mono">{p.name}</span>{" "}
                  <span className="sub" style={{ textTransform: "none" }}>
                    {p.label}
                  </span>{" "}
                  {!p.enabled && <span className="badge warn">已停用</span>}
                  {p.enabled && p.configured === false && <span className="badge err">未配置密钥</span>}
                  {p.enabled && p.configured === true && <span className="badge ok">已配置</span>}
                </h2>
                <div className="sub">{SOURCE_HINT[p.name] ?? "自定义来源"}</div>
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button className="ghost tiny" onClick={() => test(p.name)} disabled={testing === p.name}>
                  {testing === p.name ? "测试中…" : "测试连通"}
                </button>
                {canWrite && !isEditing && (
                  <>
                    <button
                      className="ghost tiny"
                      onClick={() => {
                        setEditing(p.name);
                        setDraft({ label: p.label, base_url: p.base_url, api_key_env: p.api_key_env });
                      }}
                    >
                      编辑
                    </button>
                    <button className="ghost tiny" onClick={() => toggle(p)}>
                      {p.enabled ? "停用" : "启用"}
                    </button>
                  </>
                )}
              </div>
            </div>

            {res && (
              <div className={`panel`} style={{ background: "transparent", border: "none", padding: 0 }}>
                <div className={`mono ${res.ok ? "" : "err"}`} style={{ marginBottom: 8 }}>
                  {res.ok ? "✅" : "❌"} {res.detail}
                  {res.status ? ` · HTTP ${res.status}` : ""} · {res.latency_ms}ms
                </div>
              </div>
            )}

            {isEditing ? (
              <div className="row" style={{ marginBottom: 10 }}>
                <div>
                  <label>显示名</label>
                  <input
                    value={draft.label ?? ""}
                    onChange={(e) => setDraft({ ...draft, label: e.target.value })}
                  />
                </div>
                <div>
                  <label>base_url</label>
                  <input
                    value={draft.base_url ?? ""}
                    onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
                    placeholder="https://api.example.com/v1"
                  />
                </div>
                <div>
                  <label>密钥所在环境变量</label>
                  <input
                    className="mono"
                    value={draft.api_key_env ?? ""}
                    onChange={(e) => setDraft({ ...draft, api_key_env: e.target.value })}
                    placeholder="AIGATE_XXX_API_KEY"
                  />
                </div>
                <div style={{ flex: "0 0 auto", minWidth: 0 }}>
                  <button onClick={() => save(p.name)}>保存</button>
                  <button className="ghost tiny" onClick={() => setEditing(null)} style={{ marginLeft: 6 }}>
                    取消
                  </button>
                </div>
              </div>
            ) : (
              <table>
                <tbody>
                  <tr>
                    <td className="sub" style={{ width: 110 }}>
                      base_url
                    </td>
                    <td className="mono">{p.base_url || "—"}</td>
                  </tr>
                  <tr>
                    <td className="sub">密钥来源</td>
                    <td className="mono">
                      {p.api_key_env || "—"}
                      <span className="sub">（值不入库、不回显）</span>
                    </td>
                  </tr>
                  <tr>
                    <td className="sub">被这些路由使用</td>
                    <td className="mono">
                      {p.used_by_routes?.length ? p.used_by_routes.join(", ") : "—"}
                    </td>
                  </tr>
                </tbody>
              </table>
            )}

            <h2 style={{ marginTop: 14 }}>模型与单价</h2>
            {p.models.length === 0 ? (
              <div className="empty">这个来源还没有登记模型（model_prices 里没有它的行）</div>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>模型</th>
                    <th>输入 / 1K</th>
                    <th>输出 / 1K</th>
                    <th>上下文</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {p.models.map((m) => (
                    <tr key={m.model}>
                      <td className="mono">{m.model}</td>
                      <td className="mono">¥{m.input_price_per_1k}</td>
                      <td className="mono">¥{m.output_price_per_1k}</td>
                      <td className="mono">{m.context_window}</td>
                      <td>
                        <span className={`badge ${m.enabled ? "ok" : "warn"}`}>
                          {m.enabled ? "可用" : "停用"}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        );
      })}
    </>
  );
}