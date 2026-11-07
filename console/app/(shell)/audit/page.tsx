"use client";

import { useState } from "react";
import { adminGet, shortTime } from "@/lib/api";
import { ErrorPanel, LoadingBlock, StaleBanner } from "@/components/async-state";
import { useResource } from "@/lib/use-resource";

type Row = {
  id: string;
  actor: string;
  actor_role: string;
  action: string;
  target: string;
  method: string;
  status_code: number;
  ip: string;
  request_id: string;
  created_at: string;
};

const TONE: Record<string, string> = {
  create: "ok",
  revoke: "err",
  login: "blue",
  password: "warn",
};

function tone(action: string) {
  const verb = action.split(".").pop() ?? "";
  return TONE[verb] ?? "";
}

/**
 * Audit trail. Read-only by design — there is no delete button because an audit log
 * you can edit is not an audit log.
 */
export default function AuditPage() {
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");
  const resource = useResource<{ items: Row[] }>(() => {
    const params = new URLSearchParams({ limit: "200" });
    if (action) params.set("action", action);
    if (actor) params.set("actor", actor);
    return adminGet<{ items: Row[] }>(`/audit?${params.toString()}`);
  }, [action, actor]);
  // null = 还没回来；[] = 真的没有记录。以前两者都渲染"还没有审计记录"。
  const rows = resource.data?.items ?? null;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>审计</h1>
          <div className="sub">
            最近 {rows === null ? "—" : rows.length} 条写操作 · 只追加，无删除入口
          </div>
        </div>
        <a
          className="ghost"
          style={{ padding: "6px 10px", border: "1px solid var(--border)", borderRadius: 6 }}
          href={`/api/admin/audit.csv?limit=5000${action ? `&action=${encodeURIComponent(action)}` : ""}${
            actor ? `&actor=${encodeURIComponent(actor)}` : ""
          }`}
        >
          导出 CSV
        </a>
        <button className="ghost" onClick={resource.reload}>
          刷新
        </button>
      </div>

      <div className="panel">
        <div className="row">
          <div>
            <label>动作</label>
            <input value={action} onChange={(e) => setAction(e.target.value)} placeholder="如 key.create" />
          </div>
          <div>
            <label>操作者</label>
            <input
              value={actor}
              onChange={(e) => setActor(e.target.value)}
              placeholder="如 a@team.com"
            />
          </div>
        </div>
      </div>

      {rows !== null && resource.error && (
        <StaleBanner
          loadedAt={resource.loadedAt}
          failedAt={resource.failedAt}
          error={resource.error}
          onRetry={resource.reload}
        />
      )}
      {rows === null && resource.error && (
        <ErrorPanel error={resource.error} onRetry={resource.reload} />
      )}

      <div className="panel">
        {rows === null ? (
          <LoadingBlock label="正在读取审计记录…" />
        ) : rows.length === 0 ? (
          <div className="empty">还没有审计记录（签发一把 Key 就会产生一条）</div>
        ) : (
          <table className="audit">
            <thead>
              <tr>
                <th>时间</th>
                <th>操作者</th>
                <th>动作</th>
                <th>目标</th>
                <th>结果</th>
                <th>IP</th>
                <th>request_id</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td className="mono">{shortTime(r.created_at)}</td>
                  <td className="mono" title={r.actor}>
                    {r.actor}
                    {r.actor_role ? <span className="sub"> · {r.actor_role}</span> : null}
                  </td>
                  <td>
                    <span className={`badge ${tone(r.action)}`}>{r.action}</span>
                  </td>
                  <td className="mono">{r.target}</td>
                  <td>
                    <span className={`badge ${r.status_code < 400 ? "ok" : "err"}`}>{r.status_code}</span>
                  </td>
                  <td className="mono">{r.ip}</td>
                  <td className="mono" title={r.request_id}>
                    {r.request_id.slice(0, 11)}…
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