"use client";

import { useSession } from "@/lib/session";
import { Fragment, useEffect, useMemo, useState } from "react";
import { adminGet, adminPost, shortTime } from "@/lib/api";
import { ErrorPanel, LoadingBlock, StaleBanner } from "@/components/async-state";
import { useResource } from "@/lib/use-resource";

/**
 * Key 管理。
 *
 * 页面按「谁在用 → 这把 key 是什么 → 它现在还能不能用 → 出事怎么停」排，不是按接口字段排：
 *
 *   1. 签发面板：环境、过期、用途。prod 必须给过期天数——没有过期时间的 prod key
 *      没有人会去轮换，它会在某个 .env 里活两年。
 *   2. 明文只出现一次，并且带复制按钮。少了复制按钮，用户就会截屏发到群里。
 *   3. 表格默认按「最该处理的排前面」：过期的、久未使用的先出现。
 *   4. 吊销要填原因。理由不是为了审计好看——三个月后有人问「这把为什么停了」，答案是空的
 *      就只能靠猜。
 *
 * 开发者（viewer）也能进这页：只能给自己项目签发、只能吊销自己签发的。规则在服务端，
 * 这里只是把做不到的按钮藏起来。
 */

type Key = {
  id: string;
  name: string;
  prefix: string;
  team: string;
  project: string;
  project_id: string;
  status: string;
  allowed_models: string[];
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  created_by_email: string;
  description: string;
  environment: string;
  expires_at: string | null;
  expired: boolean;
  revoked_reason: string;
  idle_days: number | null;
};

type Project = {
  id: string;
  slug: string;
  team: string;
  monthly_budget_cny: number | null;
  rpm_limit: number;
};

const ENV_LABEL: Record<string, string> = { dev: "开发", staging: "预发", prod: "生产" };
const ENV_CLASS: Record<string, string> = { dev: "", staging: "blue", prod: "warn" };

function stateOf(k: Key): { label: string; cls: string } {
  if (k.status === "revoked") return { label: "已吊销", cls: "err" };
  if (k.expired) return { label: "已过期", cls: "err" };
  if (k.idle_days !== null && k.idle_days >= 60) return { label: "久未使用", cls: "warn" };
  return { label: "生效中", cls: "ok" };
}

export default function KeysPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  // 项目列表读完之前不判断"你没有项目"——那是个结论，不是加载态。
  const [projectsReady, setProjectsReady] = useState(false);
  const [projectId, setProjectId] = useState("");
  const [name, setName] = useState("console-key");
  const [issued, setIssued] = useState<string | null>(null);
  const [models, setModels] = useState("");
  const [environment, setEnvironment] = useState("dev");
  const [expiresInDays, setExpiresInDays] = useState("90");
  const [why, setWhy] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [revoking, setRevoking] = useState<string | null>(null);
  const [revokeReason, setRevokeReason] = useState("");
  const [copied, setCopied] = useState(false);

  const { can, canWrite, me } = useSession();
  const mayIssue = canWrite || can("keys:own");
  // 吊销按钮只给"真的能吊销"的人看：管理员，或这把 key 的签发人。
  // 一个永远 403 的按钮不是权限策略，是坏产品。
  const mayRevoke = (k: Key) => canWrite || (mayIssue && !!k.created_by_email && k.created_by_email === me?.name);

  const resource = useResource<{ items: Key[] }>(() => adminGet<{ items: Key[] }>("/keys"), []);
  // null = 还没回来；[] = 真的一把 key 都没有。以前两者都显示"共 0 把 · 生效 0"。
  const keys = resource.data?.items ?? null;

  useEffect(() => {
    adminGet<{ items: Project[] }>("/projects")
      .then((d) => {
        setProjects(d.items);
        setProjectId((prev) => prev || d.items[0]?.id || "");
      })
      .catch(() => undefined)
      .finally(() => setProjectsReady(true));
  }, []);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (keys ?? [])
      .filter((k) => (statusFilter ? (statusFilter === "expired" ? k.expired : k.status === statusFilter) : true))
      .filter((k) =>
        needle
          ? [k.name, k.created_by_email, k.description, k.prefix, k.project]
              .join(" ")
              .toLowerCase()
              .includes(needle)
          : true,
      )
      .sort((a, b) => {
        // 该处理的排前面：已吊销/过期沉底，久未使用的靠上
        const rank = (k: Key) => (k.status === "revoked" ? 3 : k.expired ? 2 : 0);
        if (rank(a) !== rank(b)) return rank(a) - rank(b);
        return (b.idle_days ?? 0) - (a.idle_days ?? 0);
      });
  }, [keys, query, statusFilter]);

  async function create() {
    if (environment === "prod" && !expiresInDays.trim()) {
      setError("生产环境必须填过期天数——不带过期的 prod key 没人会去换。");
      return;
    }
    try {
      const params = new URLSearchParams({
        project_id: projectId,
        name,
        allowed_models: models.trim(),
        environment,
        description: why.trim(),
      });
      if (expiresInDays.trim()) params.set("expires_in_days", expiresInDays.trim());
      const res = await adminPost<{ api_key: string }>(`/keys?${params.toString()}`);
      setIssued(res.api_key);
      setError(null);
      setCopied(false);
      resource.reload();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function revoke(id: string) {
    try {
      const params = new URLSearchParams({ reason: revokeReason.trim() });
      await adminPost(`/keys/${id}/revoke?${params.toString()}`);
      setRevoking(null);
      setRevokeReason("");
      setNotice("已吊销，下一次请求立即 401");
      resource.reload();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("浏览器不允许写剪贴板，请手动选中复制");
    }
  }

  const counts = {
    active: (keys ?? []).filter((k) => k.status === "active" && !k.expired).length,
    expiring: (keys ?? []).filter((k) => {
      if (!k.expires_at || k.expired) return false;
      const days = (new Date(k.expires_at).getTime() - Date.now()) / 86_400_000;
      return days <= 14;
    }).length,
    idle: (keys ?? []).filter((k) => k.status === "active" && (k.idle_days ?? 0) >= 60).length,
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Key 管理</h1>
          <div className="sub">
            共 {keys === null ? "—" : keys.length} 把 · 生效 {keys === null ? "—" : counts.active}
            {counts.expiring > 0 && ` · 14 天内过期 ${counts.expiring}`}
            {counts.idle > 0 && ` · 60 天未用 ${counts.idle}`}
          </div>
        </div>
      </div>

      {issued && (
        <div className="panel" style={{ borderColor: "#1f4d2b" }}>
          <h2>新 Key（只显示这一次）</h2>
          <div className="row" style={{ alignItems: "center", gap: 10 }}>
            <div className="mono" style={{ wordBreak: "break-all", color: "var(--ok)", flex: 1 }}>
              {issued}
            </div>
            <button className="tiny" onClick={() => copy(issued)} style={{ flex: "0 0 auto" }}>
              {copied ? "已复制" : "复制"}
            </button>
          </div>
          <div className="sub" style={{ marginTop: 8 }}>
            库里只存 SHA-256。请直接粘进密钥管理（KMS / CI secret），不要发在聊天里。
          </div>
          <button className="ghost tiny" style={{ marginTop: 10 }} onClick={() => setIssued(null)}>
            我已保存，关闭
          </button>
        </div>
      )}

      {mayIssue && projectsReady && projects.length === 0 ? (
        <div className="panel" style={{ borderColor: "#5a3a1f" }}>
          <h2>签发新 Key</h2>
          <div style={{ color: "var(--warn)" }}>
            你的账号还没有被分配任何项目，所以这里没有可选的项目——这也是「签不出来」的常见原因。
          </div>
          <div className="sub" style={{ marginTop: 8 }}>
            找 owner / 管理员在「成员」页把你的账号和项目关联起来；关联后刷新这一页立刻生效，不用重新登录。
          </div>
        </div>
      ) : mayIssue ? (
        <div className="panel">
          <h2>签发新 Key</h2>
          {!canWrite && (
            <div className="sub" style={{ marginBottom: 10 }}>
              你的账号只能给自己负责的项目签发；吊销也只能吊销自己签的。
            </div>
          )}
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
              <label>名称（同项目内唯一）</label>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="order-svc" />
            </div>
            <div>
              <label>环境</label>
              <select value={environment} onChange={(e) => setEnvironment(e.target.value)}>
                <option value="dev">开发 dev</option>
                <option value="staging">预发 staging</option>
                <option value="prod">生产 prod（必须设过期）</option>
              </select>
            </div>
            <div>
              <label>过期天数（留空=永久）</label>
              <input
                value={expiresInDays}
                onChange={(e) => setExpiresInDays(e.target.value)}
                placeholder="90"
              />
            </div>
          </div>
          <div className="row" style={{ marginTop: 10 }}>
            <div style={{ flex: 2 }}>
              <label>允许的模型（逗号分隔，留空=跟随项目路由）</label>
              <input
                value={models}
                onChange={(e) => setModels(e.target.value)}
                placeholder="chat-default,chat-cheap"
              />
            </div>
            <div style={{ flex: 2 }}>
              <label>用途（给三个月后的自己看）</label>
              <input
                value={why}
                onChange={(e) => setWhy(e.target.value)}
                placeholder="订单服务线上 + CI 冒烟"
              />
            </div>
            <div style={{ flex: "0 0 auto", minWidth: 0, alignSelf: "flex-end" }}>
              <button onClick={create} disabled={!projectId || !name.trim()}>
                签发
              </button>
            </div>
          </div>
        </div>
      ) : (
        <div className="panel">
          <h2>签发</h2>
          <div className="sub">
            你的角色（财务）不能签发 key。需要新 key 时找项目负责人或管理员，他们能在同一页签发并转发。
          </div>
        </div>
      )}

      {keys !== null && resource.error && (
        <StaleBanner
          loadedAt={resource.loadedAt}
          failedAt={resource.failedAt}
          error={resource.error}
          onRetry={resource.reload}
        />
      )}
      {keys === null && resource.error && !error && (
        <ErrorPanel error={resource.error} onRetry={resource.reload} />
      )}

      {error && (
        <div className="panel" style={{ borderColor: "#5a1f1f" }}>
          <div style={{ color: "var(--err)" }}>{error}</div>
          <button className="ghost tiny" style={{ marginTop: 8 }} onClick={() => setError(null)}>
            知道了
          </button>
        </div>
      )}
      {notice && (
        <div className="panel" style={{ borderColor: "#1f4d2b" }}>
          <div style={{ color: "var(--ok)" }}>{notice}</div>
        </div>
      )}

      <div className="panel">
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div style={{ flex: 2 }}>
            <label>搜索（名称 / 归属人 / 用途 / 前缀）</label>
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="order-svc" />
          </div>
          <div style={{ flex: 1 }}>
            <label>状态</label>
            <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
              <option value="">全部</option>
              <option value="active">生效中</option>
              <option value="expired">已过期</option>
              <option value="revoked">已吊销</option>
            </select>
          </div>
        </div>

        {keys === null ? (
          <LoadingBlock label="正在读取 key 列表…" />
        ) : visible.length === 0 ? (
          <div className="empty">{keys.length === 0 ? "还没有签发过 key" : "没有匹配的 key"}</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>名称</th>
                <th>项目</th>
                <th>环境</th>
                <th>归属</th>
                <th>状态</th>
                <th>最后使用</th>
                <th>过期</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((k) => {
                const st = stateOf(k);
                return (
                  <Fragment key={k.id}>
                    <tr>
                      <td>
                        <div className="mono">{k.name}</div>
                        <div className="sub">
                          {k.description || k.allowed_models.join(", ") || "全部模型"}
                        </div>
                      </td>
                      <td className="mono">
                        {k.team}/{k.project}
                        <div className="sub">{k.prefix}…</div>
                      </td>
                      <td>
                        <span className={`badge ${ENV_CLASS[k.environment] ?? ""}`}>
                          {ENV_LABEL[k.environment] ?? k.environment}
                        </span>
                      </td>
                      <td className="sub">{k.created_by_email || "—"}</td>
                      <td>
                        <span className={`badge ${st.cls}`}>{st.label}</span>
                        {k.status === "revoked" && k.revoked_reason && (
                          <div className="sub">原因：{k.revoked_reason}</div>
                        )}
                      </td>
                      <td className="sub">
                        {k.last_used_at ? shortTime(k.last_used_at) : "从未使用"}
                        {k.idle_days !== null && k.idle_days >= 60 && k.status === "active" && (
                          <div style={{ color: "var(--warn)" }}>已闲置 {k.idle_days} 天</div>
                        )}
                      </td>
                      <td className="sub">{k.expires_at ? shortTime(k.expires_at) : "永久"}</td>
                      <td>
                        {k.status === "active" && !mayRevoke(k) && (
                          <span className="sub">仅签发人可吊销</span>
                        )}
                        {k.status === "active" && mayRevoke(k) && (
                          <button
                            className="ghost tiny"
                            onClick={() => {
                              setRevoking(revoking === k.id ? null : k.id);
                              setRevokeReason("");
                            }}
                          >
                            吊销
                          </button>
                        )}
                      </td>
                    </tr>
                    {revoking === k.id && (
                      <tr>
                        <td colSpan={8}>
                          <div className="row" style={{ alignItems: "flex-end" }}>
                            <div style={{ flex: 3 }}>
                              <label>吊销原因（会写进审计，也会显示在列表上）</label>
                              <input
                                value={revokeReason}
                                onChange={(e) => setRevokeReason(e.target.value)}
                                placeholder="已迁移到 KMS / 怀疑泄露 / 服务下线"
                              />
                            </div>
                            <div style={{ flex: "0 0 auto", minWidth: 0, alignSelf: "flex-end" }}>
                              <button onClick={() => revoke(k.id)}>确认吊销</button>
                              <button className="ghost" onClick={() => setRevoking(null)}>
                                取消
                              </button>
                            </div>
                          </div>
                          <div className="sub" style={{ marginTop: 6 }}>
                            吊销立即生效（下一次请求即 401），不可恢复。要重新启用请签一把新的。
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="panel">
        <h2>团队怎么用这一页</h2>
        <div className="sub" style={{ lineHeight: 1.9 }}>
          <div>
            <b>开发者</b>：给自己的项目签发、填用途、设 90 天过期；发现泄露自己先吊销，再签新的。
            不需要找管理员。
          </div>
          <div>
            <b>管理员</b>：能看全公司所有 key。每周扫一眼「60 天未用」和「14 天内过期」两个数字，
            前者是泄漏风险，后者是即将发生的线上故障。
          </div>
          <div>
            <b>财务</b>：看不到 key（只有成本）。这是刻意分开的——能看账单的人不需要能签发凭证。
          </div>
          <div>
            <b>离职处理</b>：在「成员」页停用账号后，用他的邮箱搜这页，吊销他签的 key。
          </div>
        </div>
      </div>
    </>
  );
}