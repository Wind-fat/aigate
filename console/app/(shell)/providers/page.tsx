"use client";

import { useState } from "react";
import { adminDelete, adminGet, adminPost, adminPut } from "@/lib/api";
import { ErrorPanel, LoadingBlock, StaleBanner } from "@/components/async-state";
import { useResource } from "@/lib/use-resource";
import { useSession } from "@/lib/session";
import { toast } from "@/lib/toast";

/**
 * 供应商：这个网关到底在跟谁说话，以及「它现在到底能不能用」。
 *
 * 三条原则写在页面里，因为这个页面是运维唯一会来的地方：
 *
 * 1. **密钥可以在控制台录入**。只认环境变量的那版，运维刚拿到 key 时只能改 .env 重启，
 *    于是永远显示「未配置密钥」。现在录入的密钥加密落库，页面上只显示末四位。
 * 2. **"能用了"必须能证明**。每个来源都能一键真打一次 `/models`：401 是 key 错了，
 *    404 是 base_url 少了 /v1，超时是网络——三种情况给出的下一步完全不同。
 * 3. **停用要能一眼看出来**。停用的供应商在路由里仍会出现在候选链上，请求时会 502
 *    `provider_disabled`；卡片上的红点比日志里翻十分钟有用。
 */

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
  key_source?: "db" | "env" | "none";
  key_hint?: string;
  models: Model[];
  used_by_routes?: string[];
  is_mock?: boolean;
};

type TestResult = { ok: boolean; status: number | null; latency_ms: number; detail: string };

const SOURCE_HINT: Record<string, string> = {
  dashscope: "阿里云百炼 · 通义千问系列",
  deepseek: "DeepSeek 开放平台",
  openai: "OpenAI 官方 / 任意 OpenAI 兼容端点",
  vllm: "自建 / 私有化推理端点",
  mock: "离线 Mock，不发网络请求（演示与测试用）",
  "mock-alt": "离线 Mock 的第二实例，用来演示降级",
};

const KEY_SOURCE_LABEL: Record<string, string> = {
  db: "控制台录入",
  env: "环境变量",
  none: "未配置",
};

/** 401 / 404 / 超时的下一步不一样，所以别只显示状态码。 */
function advice(res: TestResult): string {
  if (res.ok) return "可用：base_url 与密钥都能通";
  if (res.status === 401) return "密钥无效 → 请在下方录入新密钥，或检查环境变量";
  if (res.status === 403) return "密钥无权限 → 该 key 可能未开通此模型";
  if (res.status === 404) return "路径不对 → base_url 通常要以 /v1 结尾";
  if (res.latency_ms >= 10000) return "超时 → 检查网络/代理，或该端点在内网";
  return "连接失败 → 检查 base_url 与出网策略";
}

export default function ProvidersPage() {
  const { can } = useSession();
  const canWrite = can("providers:write");
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ label: string; base_url: string; api_key_env: string }>({
    label: "",
    base_url: "",
    api_key_env: "",
  });
  const [keyDrafts, setKeyDrafts] = useState<Record<string, string>>({});
  const [testing, setTesting] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, TestResult>>({});
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [newProvider, setNewProvider] = useState({
    name: "",
    label: "",
    base_url: "",
    api_key: "",
  });
  const [modelForm, setModelForm] = useState<string | null>(null);
  const [modelDraft, setModelDraft] = useState({
    model: "",
    input: "0.001",
    output: "0.002",
    ctx: "8192",
  });

  const resource = useResource<{ items: Provider[] }>(() => adminGet<{ items: Provider[] }>("/providers"), []);
  // null = 还没回来；[] = 真的没有配置供应商。以前首屏写"0 个来源 · 就绪 0"。
  const items = resource.data?.items ?? null;

  function load() {
    resource.reload();
  }

  async function save(name: string) {
    try {
      await adminPut(`/providers/${name}`, draft);
      toast(`${name} 已保存`);
      setEditing(null);
      load();
    } catch (e) {
      toast((e as Error).message, "err");
    }
  }

  async function saveKey(name: string) {
    const value = (keyDrafts[name] ?? "").trim();
    if (!value) return;
    try {
      const res = await adminPut<{ key_hint: string }>(`/providers/${name}/secret`, {
        api_key: value,
      });
      setKeyDrafts({ ...keyDrafts, [name]: "" });
      toast(`密钥已加密保存（末四位 ${res.key_hint}）`);
      load();
    } catch (e) {
      toast((e as Error).message, "err");
    }
  }

  async function clearKey(name: string) {
    try {
      await adminDelete(`/providers/${name}/secret`);
      toast(`${name} 的密钥已清除`);
      load();
    } catch (e) {
      toast((e as Error).message, "err");
    }
  }

  async function toggle(p: Provider) {
    try {
      await adminPut(`/providers/${p.name}`, { enabled: !p.enabled });
      toast(!p.enabled ? `${p.name} 已停用` : `${p.name} 已启用`);
      load();
    } catch (e) {
      toast((e as Error).message, "err");
    }
  }

  async function test(name: string) {
    setTesting(name);
    try {
      const res = await adminPost<TestResult>(`/providers/${name}/test`);
      setResults({ ...results, [name]: res });
    } catch (e) {
      toast((e as Error).message, "err");
    } finally {
      setTesting(null);
    }
  }

  async function create() {
    const name = newProvider.name.trim().toLowerCase();
    if (!name) return;
    try {
      const params = new URLSearchParams({
        name,
        label: newProvider.label.trim() || name,
        base_url: newProvider.base_url.trim(),
      });
      await adminPost(`/providers?${params.toString()}`);
      if (newProvider.api_key.trim()) {
        await adminPut(`/providers/${name}/secret`, { api_key: newProvider.api_key.trim() });
      }
      toast(`已接入 ${name}，可以测试连通了`);
      setNewProvider({ name: "", label: "", base_url: "", api_key: "" });
      setAdding(false);
      load();
    } catch (e) {
      toast((e as Error).message, "err");
    }
  }

  async function addModel(provider: string) {
    try {
      await adminPost("/models", {
        provider,
        model: modelDraft.model.trim(),
        input_price_per_1k: Number(modelDraft.input),
        output_price_per_1k: Number(modelDraft.output),
        context_window: Number(modelDraft.ctx || 8192),
      });
      toast(`${provider}/${modelDraft.model} 已登记，可以在路由里选了`);
      setModelForm(null);
      setModelDraft({ model: "", input: "0.001", output: "0.002", ctx: "8192" });
      load();
    } catch (e) {
      toast((e as Error).message, "err");
    }
  }

  const ready = (items ?? []).filter((p) => p.enabled && p.configured).length;
  const missing = (items ?? []).filter((p) => p.enabled && !p.configured).length;
  const off = (items ?? []).filter((p) => !p.enabled).length;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>供应商</h1>
          <div className="sub">
            {items === null ? "—" : items.length} 个来源 · 就绪 {items === null ? "—" : ready}
            {missing > 0 && ` · 缺密钥 ${missing}`}
            {off > 0 && ` · 已停用 ${off}`} · 业务代码只认虚拟模型名，换供应商不用改代码
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {canWrite && (
            <button className="ghost" onClick={() => setAdding((v) => !v)}>
              {adding ? "取消" : "接入新供应商"}
            </button>
          )}
          <button className="ghost" onClick={load}>
            刷新
          </button>
        </div>
      </div>

      {items !== null && resource.error && (
        <StaleBanner
          loadedAt={resource.loadedAt}
          failedAt={resource.failedAt}
          error={resource.error}
          onRetry={resource.reload}
        />
      )}
      {items === null && resource.error && !error && (
        <ErrorPanel error={resource.error} onRetry={resource.reload} />
      )}

      {error && (
        <div className="panel">
          <div className="err mono">{error}</div>
        </div>
      )}

      {adding && canWrite && (
        <div className="panel">
          <h2>接入新供应商</h2>
          <div className="sub" style={{ marginBottom: 10 }}>
            任何 OpenAI 兼容端点都能接：Azure OpenAI、私有化 vLLM、聚合网关、区域代理。
            接完到「模型路由」里把候选指过来即可，业务代码不用动。
          </div>
          <div className="row" style={{ alignItems: "flex-end" }}>
            <div>
              <label>标识（路由里用这个名字）</label>
              <input
                value={newProvider.name}
                onChange={(e) => setNewProvider({ ...newProvider, name: e.target.value })}
                placeholder="azure-east"
              />
            </div>
            <div>
              <label>显示名</label>
              <input
                value={newProvider.label}
                onChange={(e) => setNewProvider({ ...newProvider, label: e.target.value })}
                placeholder="Azure 东亚"
              />
            </div>
            <div style={{ flex: 2 }}>
              <label>base_url（通常以 /v1 结尾）</label>
              <input
                value={newProvider.base_url}
                onChange={(e) => setNewProvider({ ...newProvider, base_url: e.target.value })}
                placeholder="https://xxx.openai.azure.com/openai/deployments/.../v1"
              />
            </div>
            <div style={{ flex: 2 }}>
              <label>API Key（加密存储，可留空后补）</label>
              <input
                type="password"
                value={newProvider.api_key}
                onChange={(e) => setNewProvider({ ...newProvider, api_key: e.target.value })}
                placeholder="sk-..."
                autoComplete="new-password"
              />
            </div>
            <div style={{ flex: "0 0 auto", minWidth: 0 }}>
              <button onClick={create} disabled={!newProvider.name.trim()}>
                接入
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="prov-grid">
        {items === null ? (
          <LoadingBlock label="正在读取供应商配置…" />
        ) : items.length === 0 ? (
          <div className="empty">还没有配置任何供应商</div>
        ) : null}
        {(items ?? []).map((p) => {
          const isEditing = editing === p.name;
          const res = results[p.name];
          const ok = p.enabled && p.configured;
          return (
            <div className={`prov-card ${p.enabled ? "" : "off"}`} key={p.name}>
              <div className="prov-head">
                <div>
                  <div className="prov-title">
                    <span className={`dot ${ok ? "ok" : p.enabled ? "err" : "off"}`} />
                    <span className="mono">{p.name}</span>
                    <span className="sub">{p.label}</span>
                  </div>
                  <div className="sub">{SOURCE_HINT[p.name] ?? "自定义来源"}</div>
                </div>
                <div className="prov-badges">
                  {!p.enabled && <span className="badge warn">已停用</span>}
                  {p.enabled && !p.configured && <span className="badge err">缺密钥</span>}
                  {p.enabled && p.configured && <span className="badge ok">就绪</span>}
                  {p.is_mock && <span className="badge blue">演示</span>}
                </div>
              </div>

              <div className="prov-row">
                <span className="prov-label">端点</span>
                <span className="mono prov-value">{p.base_url || "—"}</span>
              </div>

              <div className="prov-row">
                <span className="prov-label">密钥</span>
                <span className="prov-value">
                  {p.key_source === "db" ? (
                    <>
                      <span className="mono">…{p.key_hint?.replace("…", "")}</span>{" "}
                      <span className="badge blue">控制台录入</span>
                    </>
                  ) : p.key_source === "env" ? (
                    <>
                      <span className="mono">{p.api_key_env}</span>{" "}
                      <span className="badge blue">环境变量</span>
                    </>
                  ) : (
                    <span style={{ color: "var(--warn)" }}>未配置</span>
                  )}
                </span>
              </div>

              {canWrite && !p.is_mock && (
                <div className="prov-keyline">
                  <input
                    type="password"
                    value={keyDrafts[p.name] ?? ""}
                    onChange={(e) => setKeyDrafts({ ...keyDrafts, [p.name]: e.target.value })}
                    placeholder={p.configured ? "粘贴新密钥以更换" : "粘贴 API Key（sk-...）"}
                    autoComplete="new-password"
                  />
                  <button
                    className="tiny"
                    onClick={() => saveKey(p.name)}
                    disabled={!(keyDrafts[p.name] ?? "").trim()}
                  >
                    保存密钥
                  </button>
                  {p.key_source === "db" && (
                    <button className="ghost tiny" onClick={() => clearKey(p.name)}>
                      清除
                    </button>
                  )}
                </div>
              )}

              {res && (
                <div className={`prov-test ${res.ok ? "ok" : "bad"}`}>
                  <div className="mono">
                    {res.ok ? "●" : "●"} {res.detail}
                    {res.status ? ` · HTTP ${res.status}` : ""} · {res.latency_ms}ms
                  </div>
                  <div className="sub">{advice(res)}</div>
                </div>
              )}

              <div className="prov-models">
                {p.models.length === 0 ? (
                  <span className="sub">还没有登记模型与单价</span>
                ) : (
                  p.models.map((m) => (
                    <span className="chip" key={m.model} title={`上下文 ${m.context_window}`}>
                      {m.model}
                      <span className="sub">
                        {" "}
                        ¥{m.input_price_per_1k}/¥{m.output_price_per_1k}
                      </span>
                    </span>
                  ))
                )}
                {canWrite && modelForm !== p.name && (
                  <button
                    className="ghost tiny"
                    onClick={() => {
                      setModelForm(p.name);
                      setModelDraft({ model: "", input: "0.001", output: "0.002", ctx: "8192" });
                    }}
                  >
                    ＋ 登记模型
                  </button>
                )}
              </div>

              {modelForm === p.name && (
                <div className="prov-keyline">
                  <input
                    placeholder="模型名，如 gpt-4o-mini"
                    value={modelDraft.model}
                    onChange={(e) => setModelDraft({ ...modelDraft, model: e.target.value })}
                    style={{ flex: 2 }}
                  />
                  <input
                    placeholder="输入 ¥/1k"
                    value={modelDraft.input}
                    onChange={(e) => setModelDraft({ ...modelDraft, input: e.target.value })}
                    style={{ width: 92 }}
                  />
                  <input
                    placeholder="输出 ¥/1k"
                    value={modelDraft.output}
                    onChange={(e) => setModelDraft({ ...modelDraft, output: e.target.value })}
                    style={{ width: 92 }}
                  />
                  <input
                    placeholder="上下文"
                    value={modelDraft.ctx}
                    onChange={(e) => setModelDraft({ ...modelDraft, ctx: e.target.value })}
                    style={{ width: 84 }}
                  />
                  <button
                    className="tiny"
                    disabled={!modelDraft.model.trim()}
                    onClick={() => addModel(p.name)}
                  >
                    保存
                  </button>
                  <button className="ghost tiny" onClick={() => setModelForm(null)}>
                    取消
                  </button>
                </div>
              )}

              {(p.used_by_routes?.length ?? 0) > 0 && (
                <div className="sub">
                  被这些虚拟模型使用：{p.used_by_routes?.join("、")}
                </div>
              )}

              {isEditing ? (
                <div className="prov-edit">
                  <div className="row" style={{ alignItems: "flex-end" }}>
                    <div>
                      <label>显示名</label>
                      <input
                        value={draft.label}
                        onChange={(e) => setDraft({ ...draft, label: e.target.value })}
                      />
                    </div>
                    <div style={{ flex: 2 }}>
                      <label>base_url</label>
                      <input
                        value={draft.base_url}
                        onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
                      />
                    </div>
                    <div style={{ flex: 2 }}>
                      <label>环境变量名（可选，控制台未录密钥时用它）</label>
                      <input
                        value={draft.api_key_env}
                        onChange={(e) => setDraft({ ...draft, api_key_env: e.target.value })}
                      />
                    </div>
                  </div>
                  <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                    <button className="tiny" onClick={() => save(p.name)}>
                      保存
                    </button>
                    <button className="ghost tiny" onClick={() => setEditing(null)}>
                      取消
                    </button>
                  </div>
                </div>
              ) : (
                <div className="prov-actions">
                  <button className="ghost tiny" onClick={() => test(p.name)} disabled={testing === p.name}>
                    {testing === p.name ? "测试中…" : "测试连通"}
                  </button>
                  {canWrite && (
                    <>
                      <button
                        className="ghost tiny"
                        onClick={() => {
                          setEditing(p.name);
                          setDraft({
                            label: p.label,
                            base_url: p.base_url,
                            api_key_env: p.api_key_env,
                          });
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
              )}
            </div>
          );
        })}
      </div>

      <div className="panel">
        <h2>密钥是怎么存的</h2>
        <div className="sub" style={{ lineHeight: 1.9 }}>
          <div>
            控制台录入的密钥用 <span className="mono">AES-128-CBC + HMAC</span>（Fernet）加密后写库，
            主密钥来自环境变量 <span className="mono">AIGATE_SECRET_KEY</span>。
            <b>单看数据库文件或它的备份解不开</b>；读接口只返回末四位用于人工核对。
          </div>
          <div>
            没有设置的团队可以继续用环境变量（K8s Secret 注入）：控制台未录密钥时自动回退，
            页面上的来源标签会显示「环境变量」。
          </div>
          <div>
            换主密钥会让已保存的密钥失效——控制台会提示「解不开，请重新录入」，不会静默变成空密钥。
          </div>
        </div>
      </div>
    </>
  );
}