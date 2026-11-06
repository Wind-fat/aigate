"use client";

import { useCallback, useEffect, useState } from "react";
import { adminGet, adminPut, money, pct } from "@/lib/api";
import { useSession } from "@/lib/session";
import { toast } from "@/lib/toast";

type Project = {
  id: string;
  slug: string;
  name: string;
  team: string;
  status: string;
  rpm_limit: number;
  monthly_budget_cny: number | null;
  month_spend_cny: number;
  budget_used_pct: number | null;
  keys_active: number;
};

type Draft = { rpm_limit: string; monthly_budget_cny: string };

/**
 * Budgets and rate limits.
 *
 * This page exists because these two numbers are the only ones a manager ever asks to
 * change, and until now they required a SQL client. Editing is per-row and inline: a
 * modal for two fields is more clicks than the task deserves.
 */
export default function ProjectsPage() {
  const [items, setItems] = useState<Project[]>([]);
  const [month, setMonth] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>({ rpm_limit: "", monthly_budget_cny: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { canWrite } = useSession();

  const load = useCallback(() => {
    adminGet<{ items: Project[]; month: string }>("/projects/summary")
      .then((d) => {
        setItems(d.items);
        setMonth(d.month);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function startEdit(p: Project) {
    setEditing(p.id);
    setDraft({
      rpm_limit: String(p.rpm_limit),
      monthly_budget_cny: p.monthly_budget_cny === null ? "" : String(p.monthly_budget_cny),
    });
  }

  async function save(p: Project) {
    setBusy(true);
    try {
      const body: Record<string, unknown> = {
        rpm_limit: Number(draft.rpm_limit),
        // empty input = no cap; the API treats an explicit null as "unlimited"
        monthly_budget_cny: draft.monthly_budget_cny.trim() === "" ? null : Number(draft.monthly_budget_cny),
      };
      await adminPut(`/projects/${p.id}`, body);
      toast(`${p.slug} 已保存`);
      setEditing(null);
      load();
    } catch (e) {
      toast((e as Error).message, "err");
    } finally {
      setBusy(false);
    }
  }

  async function toggleStatus(p: Project) {
    const next = p.status === "active" ? "paused" : "active";
    try {
      await adminPut(`/projects/${p.id}`, { status: next });
      toast(next === "paused" ? `${p.slug} 已暂停（请求会被 403 拒绝）` : `${p.slug} 已恢复`);
      load();
    } catch (e) {
      toast((e as Error).message, "err");
    }
  }

  const total = items.reduce((sum, p) => sum + p.month_spend_cny, 0);
  const near = items.filter((p) => (p.budget_used_pct ?? 0) >= 80).length;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>预算与配额</h1>
          <div className="sub">
            {month} 起算 · {items.length} 个项目 · 本月合计 {money(total)}
            {near > 0 ? ` · ${near} 个已用超 80%` : ""}
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

      <div className="panel">
        {items.length === 0 ? (
          <div className="empty">还没有项目。跑一次 `uv run python scripts/seed.py` 建示例租户。</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>团队 / 项目</th>
                <th>状态</th>
                <th>RPM</th>
                <th>月预算</th>
                <th>本月已用</th>
                <th>用量</th>
                <th>活跃 Key</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((p) => {
                const isEditing = editing === p.id;
                const used = p.budget_used_pct ?? 0;
                const tone = used >= 100 ? "err" : used >= 80 ? "warn" : "ok";
                return (
                  <tr key={p.id}>
                    <td className="mono">
                      {p.team}/{p.slug}
                    </td>
                    <td>
                      <span className={`badge ${p.status === "active" ? "ok" : "warn"}`}>{p.status}</span>
                    </td>
                    <td className="mono">
                      {isEditing ? (
                        <input
                          style={{ width: 70 }}
                          value={draft.rpm_limit}
                          onChange={(e) => setDraft({ ...draft, rpm_limit: e.target.value })}
                        />
                      ) : (
                        p.rpm_limit
                      )}
                    </td>
                    <td className="mono">
                      {isEditing ? (
                        <input
                          style={{ width: 100 }}
                          placeholder="留空=不限"
                          value={draft.monthly_budget_cny}
                          onChange={(e) => setDraft({ ...draft, monthly_budget_cny: e.target.value })}
                        />
                      ) : p.monthly_budget_cny === null ? (
                        <span className="sub">不限</span>
                      ) : (
                        `¥${p.monthly_budget_cny}`
                      )}
                    </td>
                    <td className="mono">{money(p.month_spend_cny)}</td>
                    <td>
                      {p.budget_used_pct === null ? (
                        <span className="sub">—</span>
                      ) : (
                        <div className="meter" title={`${pct(used)} of budget`}>
                          <div
                            className={`meter-fill ${tone}`}
                            style={{ width: `${Math.min(100, used)}%` }}
                          />
                          <span className="meter-label">{pct(used)}</span>
                        </div>
                      )}
                    </td>
                    <td className="mono">{p.keys_active}</td>
                    <td>
                      {isEditing ? (
                        <>
                          <button disabled={busy} onClick={() => save(p)} style={{ marginRight: 6 }}>
                            保存
                          </button>
                          <button className="ghost tiny" onClick={() => setEditing(null)}>
                            取消
                          </button>
                        </>
                      ) : canWrite ? (
                        <>
                          <button className="ghost tiny" onClick={() => startEdit(p)} style={{ marginRight: 6 }}>
                            编辑
                          </button>
                          <button className="ghost tiny" onClick={() => toggleStatus(p)}>
                            {p.status === "active" ? "暂停" : "恢复"}
                          </button>
                        </>
                      ) : (
                        <span className="sub">只读</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="panel">
        <h2>这两个数字的含义</h2>
        <table>
          <tbody>
            <tr>
              <td className="mono">RPM</td>
              <td>每个项目每 60 秒的请求上限。超了返回 429，<b>且不落账单</b>——所以报表里的次数可能小于客户端调用次数。</td>
            </tr>
            <tr>
              <td className="mono">月预算</td>
              <td>自然月累计成本的硬上限。用完返回 <code>gateway.budget_exhausted</code>，不会静默降级成便宜模型。</td>
            </tr>
            <tr>
              <td className="mono">暂停</td>
              <td>保留 Key 和历史，立即停止计费（403 <code>gateway.project_paused</code>）。迁移、下线、演示结束都用它。</td>
            </tr>
          </tbody>
        </table>
      </div>
    </>
  );
}