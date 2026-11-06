"use client";

import { useCallback, useEffect, useState } from "react";
import { adminGet, adminPost, adminPut, money, pct } from "@/lib/api";
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
  daily_budget_cny: number | null;
  month_spend_cny: number;
  budget_used_pct: number | null;
  keys_active: number;
};

type Draft = { rpm_limit: string; monthly_budget_cny: string; daily_budget_cny: string };

type Team = {
  id: string;
  slug: string;
  name: string;
  rpm_limit: number;
  monthly_budget_cny: number | null;
  month_spend_cny: number;
  budget_used_pct: number | null;
};

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
  const [draft, setDraft] = useState<Draft>({
    rpm_limit: "",
    monthly_budget_cny: "",
    daily_budget_cny: "",
  });
  const [teams, setTeams] = useState<Team[]>([]);
  const [teamDraft, setTeamDraft] = useState<Record<string, { rpm: string; budget: string }>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { canWrite } = useSession();
  const [newTeam, setNewTeam] = useState({ name: "", slug: "", budget: "" });
  const [newProject, setNewProject] = useState({
    teamId: "",
    name: "",
    slug: "",
    rpm: "60",
    daily: "",
    monthly: "",
  });

  const load = useCallback(() => {
    adminGet<{ items: Project[]; month: string }>("/projects/summary")
      .then((d) => {
        setItems(d.items);
        setMonth(d.month);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
    adminGet<{ items: Team[] }>("/teams")
      .then((d) => setTeams(d.items))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function startEdit(p: Project) {
    setEditing(p.id);
    setDraft({
      rpm_limit: String(p.rpm_limit),
      monthly_budget_cny: p.monthly_budget_cny === null ? "" : String(p.monthly_budget_cny),
      daily_budget_cny: p.daily_budget_cny === null ? "" : String(p.daily_budget_cny),
    });
  }

  async function save(p: Project) {
    setBusy(true);
    try {
      const body: Record<string, unknown> = {
        rpm_limit: Number(draft.rpm_limit),
        // empty input = no cap; the API treats an explicit null as "unlimited"
        monthly_budget_cny: draft.monthly_budget_cny.trim() === "" ? null : Number(draft.monthly_budget_cny),
        daily_budget_cny: draft.daily_budget_cny.trim() === "" ? null : Number(draft.daily_budget_cny),
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

  async function createTeam() {
    try {
      await adminPost("/teams", {
        name: newTeam.name,
        slug: newTeam.slug,
        monthly_budget_cny: newTeam.budget.trim() === "" ? null : Number(newTeam.budget),
      });
      toast(`团队 ${newTeam.slug} 已创建`);
      setNewTeam({ name: "", slug: "", budget: "" });
      load();
    } catch (e) {
      toast((e as Error).message, "err");
    }
  }

  async function createProject() {
    try {
      await adminPost("/projects", {
        team_id: newProject.teamId,
        name: newProject.name,
        slug: newProject.slug,
        rpm_limit: Number(newProject.rpm || 60),
        daily_budget_cny: newProject.daily.trim() === "" ? null : Number(newProject.daily),
        monthly_budget_cny: newProject.monthly.trim() === "" ? null : Number(newProject.monthly),
      });
      toast(`项目 ${newProject.slug} 已创建，下一步给它签 key`);
      setNewProject({ ...newProject, name: "", slug: "", rpm: "60", daily: "", monthly: "" });
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

      {canWrite && (
        <div className="panel">
          <h2>新建团队 / 项目</h2>
          <div className="sub" style={{ marginBottom: 10 }}>
            新人报到、新业务上线都从这里开始：先建团队（部门），再在团队下建项目，最后给项目签 key。
            slug 只允许小写字母、数字和连字符——它会出现在账单、CSV 和 URL 里。
          </div>

          <div className="row" style={{ alignItems: "flex-end" }}>
            <div>
              <label>团队名称</label>
              <input
                value={newTeam.name}
                onChange={(e) => setNewTeam({ ...newTeam, name: e.target.value })}
                placeholder="增长部"
              />
            </div>
            <div>
              <label>团队 slug</label>
              <input
                value={newTeam.slug}
                onChange={(e) => setNewTeam({ ...newTeam, slug: e.target.value })}
                placeholder="growth"
              />
            </div>
            <div>
              <label>月预算（留空=不限）</label>
              <input
                value={newTeam.budget}
                onChange={(e) => setNewTeam({ ...newTeam, budget: e.target.value })}
                placeholder="50"
              />
            </div>
            <div style={{ flex: "0 0 auto", minWidth: 0 }}>
              <button
                className="ghost"
                onClick={createTeam}
                disabled={!newTeam.name.trim() || !newTeam.slug.trim()}
              >
                建团队
              </button>
            </div>
          </div>

          <div className="row" style={{ alignItems: "flex-end", marginTop: 12 }}>
            <div>
              <label>所属团队</label>
              <select
                value={newProject.teamId || teams[0]?.id || ""}
                onChange={(e) => setNewProject({ ...newProject, teamId: e.target.value })}
              >
                {teams.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.slug}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label>项目名称</label>
              <input
                value={newProject.name}
                onChange={(e) => setNewProject({ ...newProject, name: e.target.value })}
                placeholder="订单助手"
              />
            </div>
            <div>
              <label>项目 slug</label>
              <input
                value={newProject.slug}
                onChange={(e) => setNewProject({ ...newProject, slug: e.target.value })}
                placeholder="order-bot"
              />
            </div>
            <div>
              <label>RPM</label>
              <input
                style={{ width: 80 }}
                value={newProject.rpm}
                onChange={(e) => setNewProject({ ...newProject, rpm: e.target.value })}
              />
            </div>
            <div>
              <label>日预算</label>
              <input
                style={{ width: 80 }}
                value={newProject.daily}
                onChange={(e) => setNewProject({ ...newProject, daily: e.target.value })}
                placeholder="留空"
              />
            </div>
            <div style={{ flex: "0 0 auto", minWidth: 0 }}>
              <button
                onClick={createProject}
                disabled={
                  !newProject.name.trim() ||
                  !newProject.slug.trim() ||
                  teams.length === 0
                }
              >
                建项目
              </button>
            </div>
          </div>
          {teams.length === 0 && (
            <div className="sub" style={{ marginTop: 8 }}>
              先把团队建出来，项目才有归属。
            </div>
          )}
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
                <th>日预算</th>
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
                          style={{ width: 90 }}
                          placeholder="留空=不限"
                          value={draft.daily_budget_cny}
                          onChange={(e) => setDraft({ ...draft, daily_budget_cny: e.target.value })}
                        />
                      ) : p.daily_budget_cny === null ? (
                        <span className="sub">不限</span>
                      ) : (
                        `¥${p.daily_budget_cny}`
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
        <h2>团队级上限（跨项目共享）</h2>
        <div className="sub" style={{ marginBottom: 10 }}>
          一个项目的噪声不该吃掉整个部门的额度。团队额度在这里设，项目额度在上面设。
        </div>
        {teams.length === 0 ? (
          <div className="empty">没有团队</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>团队</th>
                <th>RPM（0=不限）</th>
                <th>月预算</th>
                <th>本月已用</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {teams.map((t) => {
                const d = teamDraft[t.id] ?? {
                  rpm: String(t.rpm_limit),
                  budget: t.monthly_budget_cny === null ? "" : String(t.monthly_budget_cny),
                };
                return (
                  <tr key={t.id}>
                    <td className="mono">{t.slug}</td>
                    <td>
                      {canWrite ? (
                        <input
                          style={{ width: 80 }}
                          value={d.rpm}
                          onChange={(e) =>
                            setTeamDraft({ ...teamDraft, [t.id]: { ...d, rpm: e.target.value } })
                          }
                        />
                      ) : (
                        <span className="mono">{t.rpm_limit === 0 ? "不限" : t.rpm_limit}</span>
                      )}
                    </td>
                    <td>
                      {canWrite ? (
                        <input
                          style={{ width: 100 }}
                          placeholder="留空=不限"
                          value={d.budget}
                          onChange={(e) =>
                            setTeamDraft({ ...teamDraft, [t.id]: { ...d, budget: e.target.value } })
                          }
                        />
                      ) : t.monthly_budget_cny === null ? (
                        <span className="sub">不限</span>
                      ) : (
                        <span className="mono">¥{t.monthly_budget_cny}</span>
                      )}
                    </td>
                    <td className="mono">
                      {money(t.month_spend_cny)}
                      {t.budget_used_pct !== null && <span className="sub"> · {pct(t.budget_used_pct)}</span>}
                    </td>
                    <td>
                      {canWrite && (
                      <button
                        className="ghost tiny"
                        onClick={async () => {
                          try {
                            await adminPut(`/teams/${t.id}`, {
                              rpm_limit: Number(d.rpm || 0),
                              monthly_budget_cny: d.budget.trim() === "" ? null : Number(d.budget),
                            });
                            toast(`${t.slug} 已保存`);
                            load();
                          } catch (e) {
                            toast((e as Error).message, "err");
                          }
                        }}
                      >
                        保存
                      </button>
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
        <h2>这三个数字的含义</h2>
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
              <td className="mono">日预算</td>
              <td>自然日累计成本的硬上限。它防的不是超支，是「凌晨跑飞的循环」——月预算要到月底才发现。</td>
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