"use client";

import { useCallback, useEffect, useState } from "react";
import { useSession } from "@/lib/session";

type User = {
  id: string;
  email: string;
  name: string;
  role: string;
  status: string;
};

const ROLES = ["viewer", "admin", "owner"];

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
  const [role, setRole] = useState("admin");
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [curPw, setCurPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [pwMsg, setPwMsg] = useState<string | null>(null);

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
        body: JSON.stringify({ email, name, password, role }),
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
          <div style={{ flex: "0 0 auto", minWidth: 0 }}>
            <button onClick={create} disabled={!email || password.length < 10}>
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
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id}>
                <td className="mono">{u.email}</td>
                <td>{u.name || "—"}</td>
                <td>
                  <span className={`badge ${u.role === "owner" ? "blue" : ""}`}>{u.role}</span>
                </td>
                <td>
                  <span className={`badge ${u.status === "active" ? "ok" : "err"}`}>{u.status}</span>
                </td>
                <td>
                  {u.status === "active" && myRole === "owner" && (
                    <button className="ghost tiny" onClick={() => disable(u.id)}>
                      停用
                    </button>
                  )}
                </td>
              </tr>
            ))}
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