"use client";

import { Fragment, useCallback, useEffect, useState } from "react";
import { useSession } from "@/lib/session";

type User = {
  id: string;
  email: string;
  name: string;
  role: string;
  status: string;
  project_scope?: string[];
};

const ROLES = ["viewer", "finance", "admin", "owner"];

/**
 * Members. Three roles only, because a local team tool does not need a permission
 * matrix — it needs "who can log in" and "who can change things".
 */
export default function MembersPage() {
  const [users, setUsers] = useState<User[]>([]);
  const { me, role: myRole } = useSession();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("viewer");
  const [projects, setProjects] = useState<{ id: string; slug: string; team: string }[]>([]);
  const [scope, setScope] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [curPw, setCurPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [pwMsg, setPwMsg] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<{
    role: string;
    scope: string[];
    status: string;
    password: string;
  }>({ role: "viewer", scope: [], status: "active", password: "" });

  async function changePassword() {
    setPwMsg(null);
    const res = await fetch("/api/console/me/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current: curPw, new: newPw }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      setPwMsg(body?.error?.message ?? body?.error?.code ?? `${res.status}`);
      return;
    }
    setPwMsg("已更新，下次登录用新密码");
    setCurPw("");
    setNewPw("");
  }

  const load = useCallback(() => {
    fetch("/api/admin/projects", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : { items: [] }))
      .then((d) => setProjects(d.items ?? []))
      .catch(() => undefined);
    fetch("/api/console/users", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status}`))))
      .then((d) => setUsers(d.items))
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function create() {
    setError(null);
    setOk(null);
    try {
      const res = await fetch("/api/console/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, name, password, role, project_scope: scope }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body?.error?.message ?? body?.error?.code ?? `${res.status}`);
      setOk(`已创建 ${body.email}`);
      setEmail("");
      setName("");
      setPassword("");
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  function startEdit(u: User) {
    setEditing(u.id);
    setDraft({
      role: u.role,
      scope: u.project_scope ?? [],
      status: u.status,
      password: "",
    });
    setError(null);
    setOk(null);
  }

  async function saveEdit(id: string) {
    setError(null);
    try {
      const body: Record<string, unknown> = { role: draft.role, project_scope: draft.scope };
      if (draft.status) body.status = draft.status;
      if (draft.password) body.password = draft.password;
      const res = await fetch(`/api/console/users/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(payload?.error?.message ?? payload?.error?.code ?? `${res.status}`);
      setOk(`已保存：${(payload.changed ?? []).join("、") || "没有变化"}`);
      setEditing(null);
      setDraft({ role: "viewer", scope: [], status: "active", password: "" });
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function disable(id: string) {
    try {
      await fetch(`/api/console/users/${id}/disable`, { method: "POST" });
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>成员</h1>
          <div className="sub">
            共 {users.length} 人{myRole ? ` · 你是 ${myRole}` : ""}
          </div>
        </div>
        <button className="ghost" onClick={load}>
          刷新
        </button>
      </div>

      {(myRole === "owner" || myRole === "admin") && (
      <div className="panel">
        <h2>新增成员</h2>
        <div className="row">
          <div>
            <label>邮箱</label>
            <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="a@team.com" />
          </div>
          <div>
            <label>显示名</label>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="可选" />
          </div>
          <div>
            <label>初始密码（≥10 位）</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
            />
          </div>
          <div>
            <label>角色</label>
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label>可见项目{role === "viewer" ? "（开发者必选）" : "（仅 viewer 生效）"}</label>
            <select
              multiple
              value={scope}
              onChange={(e) =>
                setScope(Array.from(e.target.selectedOptions).map((o) => o.value))
              }
              style={{ minHeight: 66 }}
            >
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.team}/{p.slug}
                </option>
              ))}
            </select>
          </div>
          <div style={{ flex: "0 0 auto", minWidth: 0 }}>
            <button
              onClick={create}
              disabled={!email || password.length < 10 || (role === "viewer" && scope.length === 0)}
            >
              创建
            </button>
          </div>
        </div>
        {error && (
          <div className="err mono" style={{ marginTop: 10 }}>
            {error}
          </div>
        )}
        {ok && (
          <div className="mono" style={{ marginTop: 10, color: "var(--ok)" }}>
            {ok}
          </div>
        )}
      </div>
      )}

      <div className="panel">
        <h2>账号</h2>
        <table>
          <thead>
            <tr>
              <th>邮箱</th>
              <th>名称</th>
              <th>角色</th>
              <th>可见项目</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => {
              const scoped = u.project_scope ?? [];
              const needsScope = u.role === "viewer" && scoped.length === 0;
              const canEdit =
                (myRole === "owner" || myRole === "admin") && (u.role !== "owner" || myRole === "owner");
              return (
                <Fragment key={u.id}>
                  <tr>
                    <td className="mono">{u.email}</td>
                    <td>{u.name || "—"}</td>
                    <td>
                      <span className={`badge ${u.role === "owner" ? "blue" : ""}`}>{u.role}</span>
                    </td>
                    <td className="sub">
                      {u.role === "viewer" || u.role === "finance" ? (
                        scoped.length === 0 ? (
                          <span style={{ color: needsScope ? "var(--err)" : undefined }}>
                            {needsScope ? "未分配（什么也看不到）" : "全部"}
                          </span>
                        ) : (
                          scoped.map((id) => projects.find((p) => p.id === id)?.slug ?? id).join(", ")
                        )
                      ) : (
                        "全部"
                      )}
                    </td>
                    <td>
                      <span className={`badge ${u.status === "active" ? "ok" : "err"}`}>{u.status}</span>
                    </td>
                    <td>
                      {canEdit && (
                        <button className="ghost tiny" onClick={() => startEdit(u)}>
                          编辑
                        </button>
                      )}
                      {u.status === "active" && myRole === "owner" && u.email !== me?.name && (
                        <button className="ghost tiny" onClick={() => disable(u.id)}>
                          停用
                        </button>
                      )}
                    </td>
                  </tr>
                  {editing === u.id && (
                    <tr>
                      <td colSpan={6}>
                        <div className="row" style={{ alignItems: "flex-end" }}>
                          <div>
                            <label>角色</label>
                            <select
                              value={draft.role}
                              onChange={(e) => setDraft({ ...draft, role: e.target.value })}
                            >
                              {ROLES.filter((r) => r !== "owner" || myRole === "owner").map((r) => (
                                <option key={r} value={r}>
                                  {r}
                                </option>
                              ))}
                            </select>
                          </div>
                          <div>
                            <label>可见项目（viewer 至少一个）</label>
                            <select
                              multiple
                              value={draft.scope}
                              onChange={(e) =>
                                setDraft({
                                  ...draft,
                                  scope: Array.from(e.target.selectedOptions).map((o) => o.value),
                                })
                              }
                              style={{ minHeight: 66 }}
                            >
                              {projects.map((p) => (
                                <option key={p.id} value={p.id}>
                                  {p.team}/{p.slug}
                                </option>
                              ))}
                            </select>
                          </div>
                          <div>
                            <label>状态</label>
                            <select
                              value={draft.status}
                              onChange={(e) => setDraft({ ...draft, status: e.target.value })}
                            >
                              <option value="active">active</option>
                              <option value="disabled">disabled</option>
                            </select>
                          </div>
                          <div>
                            <label>重置密码（留空=不改）</label>
                            <input
                              type="password"
                              value={draft.password}
                              onChange={(e) => setDraft({ ...draft, password: e.target.value })}
                              placeholder="≥10 位"
                              autoComplete="new-password"
                            />
                          </div>
                          <div style={{ flex: "0 0 auto", minWidth: 0 }}>
                            <button onClick={() => saveEdit(u.id)}>保存</button>
                            <button className="ghost" onClick={() => setEditing(null)}>
                              取消
                            </button>
                          </div>
                        </div>
                        <div className="sub" style={{ marginTop: 6 }}>
                          改完立即生效：被降权的人下一次请求就按新角色判权限，不需要重新登录。
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>我的密码</h2>
        <div className="row">
          <div>
            <label>当前密码</label>
            <input
              type="password"
              value={curPw}
              onChange={(e) => setCurPw(e.target.value)}
              autoComplete="current-password"
            />
          </div>
          <div>
            <label>新密码（≥10 位）</label>
            <input
              type="password"
              value={newPw}
              onChange={(e) => setNewPw(e.target.value)}
              autoComplete="new-password"
            />
          </div>
          <div style={{ flex: "0 0 auto", minWidth: 0 }}>
            <button onClick={changePassword} disabled={newPw.length < 10}>
              更新
            </button>
          </div>
        </div>
        {pwMsg && (
          <div className="sub mono" style={{ marginTop: 10 }}>
            {pwMsg}
          </div>
        )}
      </div>

      <div className="panel">
        <h2>角色说明</h2>
        <table>
          <tbody>
            <tr>
              <td className="mono">viewer</td>
              <td>只读：能看概览、明细、Trace、审计，不能签发 Key 或改配置</td>
            </tr>
            <tr>
              <td className="mono">admin</td>
              <td>可读写：签发/吊销 Key、切模型、管成员（不能创建 owner）</td>
            </tr>
            <tr>
              <td className="mono">owner</td>
              <td>全部权限，包括创建/停用 owner</td>
            </tr>
          </tbody>
        </table>
      </div>
    </>
  );
}