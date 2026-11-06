"use client";

import { useSession } from "@/lib/session";
import { useCallback, useEffect, useState } from "react";
import { adminGet, adminPost, shortTime } from "@/lib/api";

type Key = {
  id: string;
  name: string;
  prefix: string;
  team: string;
  project: string;
  status: string;
  allowed_models: string[];
  created_at: string;
  last_used_at: string | null;
};

type Project = { id: string; slug: string; team: string; monthly_budget_cny: number | null; rpm_limit: number };

export default function KeysPage() {
  const [keys, setKeys] = useState<Key[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [name, setName] = useState("console-key");
  const [issued, setIssued] = useState<string | null>(null);
  // 留空 = 跟着项目走（所有已启用路由）
  const [models, setModels] = useState("");
  const [error, setError] = useState<string | null>(null);
  // A viewer sees exactly the same list and none of the buttons. Showing a button that
  // always 403s is worse than not showing it: it reads as a broken product, not a policy.
  const { canWrite } = useSession();

  const load = useCallback(() => {
    adminGet<{ items: Key[] }>("/keys")
      .then((d) => setKeys(d.items))
      .catch((e: Error) => setError(e.message));
    adminGet<{ items: Project[] }>("/projects")
      .then((d) => {
        setProjects(d.items);
        setProjectId((prev) => prev || d.items[0]?.id || "");
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function create() {
    try {
      const res = await adminPost<{ api_key: string }>(
        `/keys?project_id=${projectId}&name=${encodeURIComponent(name)}&allowed_models=${encodeURIComponent(models.trim())}`,
      );
      setIssued(res.api_key);
      setError(null);
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function revoke(id: string) {
    try {
      await adminPost(`/keys/${id}/revoke`);
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Key 管理</h1>
          <div className="sub">共 {keys.length} 把 · 明文只在创建时显示一次</div>
        </div>
      </div>

      {issued && (
        <div className="panel" style={{ borderColor: "#1f4d2b" }}>
          <h2>新 Key（只显示这一次）</h2>
          <div className="mono" style={{ wordBreak: "break-all", color: "var(--ok)" }}>
            {issued}
          </div>
          <div className="sub" style={{ marginTop: 8 }}>
            库里只存 SHA-256，关掉这个提示后就再也看不到了。
          </div>
        </div>
      )}

      {canWrite ? (
      <div className="panel">
        <h2>签发新 Key</h2>
        <div className="row">
          <div>
            <label>项目</label>
            <select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.team} / {p.slug}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label>名称</label>
            <input value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div>
            <label>允许的模型（逗号分隔，留空=全部）</label>
            <input
              value={models}
              onChange={(e) => setModels(e.target.value)}
              placeholder="chat-default,chat-cheap"
            />
          </div>
          <div style={{ flex: "0 0 auto", minWidth: 0 }}>
            <button onClick={create} disabled={!projectId}>
              签发
            </button>
          </div>
        </div>
        {error && <div className="err mono" style={{ marginTop: 10 }}>{error}</div>}
      </div>
      ) : (
        <div className="panel">
          <h2>只读账号</h2>
          <div className="sub">viewer 角色可以看明细和账单，不能签发或吊销 Key。需要权限找 owner。</div>
        </div>
      )}

      <div className="panel">
        <h2>已签发</h2>
        {keys.length === 0 ? (
          <div className="empty">还没有 Key</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>前缀</th>
                <th>名称</th>
                <th>归属</th>
                <th>状态</th>
                <th>可用范围</th>
                <th>最后使用</th>
                <th>创建</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.id}>
                  <td className="mono">{k.prefix}…</td>
                  <td>{k.name}</td>
                  <td className="mono">
                    {k.team}/{k.project}
                  </td>
                  <td>
                    <span className={`badge ${k.status === "active" ? "ok" : "err"}`}>{k.status}</span>
                  </td>
                  <td className="mono">
                    {k.allowed_models?.length ? k.allowed_models.join(", ") : <span className="sub">全部（随路由）</span>}
                  </td>
                  <td className="mono">{k.last_used_at ? shortTime(k.last_used_at) : "从未"}</td>
                  <td className="mono">{shortTime(k.created_at)}</td>
                  <td>
                    {canWrite && k.status === "active" && (
                      <button className="ghost" onClick={() => revoke(k.id)}>
                        吊销
                      </button>
                    )}
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
