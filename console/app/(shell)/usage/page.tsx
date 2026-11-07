"use client";

import Link from "next/link";
import { useCallback, useState } from "react";
import { adminGet, money, ms, shortTime } from "@/lib/api";
import { ErrorPanel, LoadingBlock, StaleBanner } from "@/components/async-state";
import { useResource } from "@/lib/use-resource";
import { useSession } from "@/lib/session";

type Item = {
  request_id: string;
  project_id: string;
  provider: string;
  model_requested: string;
  model_used: string;
  input_tokens: number;
  output_tokens: number;
  cost_cny: number;
  latency_ms: number;
  ttft_ms: number | null;
  status: string;
  error_code: string | null;
  cache_hit: string | null;
  degraded: boolean;
  created_at: string;
};

type Project = { id: string; slug: string; team: string };

const STATUSES = ["", "ok", "cached", "fallback", "error"];
const WINDOWS = [
  { label: "近 1 小时", hours: 1 },
  { label: "近 24 小时", hours: 24 },
  { label: "近 7 天", hours: 24 * 7 },
  { label: "近 30 天", hours: 24 * 30 },
  { label: "全部", hours: 0 },
];

const PAGE = 50;

/**
 * 状态徽章：白名单映射。
 *
 * 以前是 `fallback` 到绿色 ok 的三元链：status 是任何没枚举过的值（或未来新增的状态）
 * 都会渲染成"绿 ok"。未知必须是灰色"未知"——它与 ok 的区别就是值班要不要起床。
 */
function statusBadge(item: Item) {
  switch (item.status) {
    case "ok":
      return <span className="badge ok">ok</span>;
    case "cached":
      return <span className="badge blue">cached {item.cache_hit ?? ""}</span>;
    case "error":
      return <span className="badge err">{item.error_code ?? "error"}</span>;
    case "fallback":
      return <span className="badge warn">fallback</span>;
    default:
      // 空字符串/未知枚举：灰 badge + 原值，绝不冒充 ok
      return <span className="badge">未知{item.status ? `（${item.status}）` : ""}</span>;
  }
}

/**
 * Call log. Every row links to its trace: the question a billing number raises is
 * always "why", and the answer lives one click away instead of in another tab.
 */
export default function UsagePage() {
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState("");
  const [model, setModel] = useState("");
  const [projectId, setProjectId] = useState("");
  const [hours, setHours] = useState(24 * 7);
  const { can } = useSession();

  // 项目/模型下拉：只是筛选项的来源，读不到就退化成"全部"，
  // 但不拿空数组冒充"没有项目"——null 交给下面的 select 显示加载中。
  const projectResource = useResource<{ items: Project[] }>(() => adminGet<{ items: Project[] }>("/projects"), []);
  const modelResource = useResource<{ policies: { match_model: string }[] }>(
    () => adminGet<{ policies: { match_model: string }[] }>("/models"),
    [],
  );
  const projects = projectResource.data?.items ?? null;
  const models =
    modelResource.data?.policies.map((p) => p.match_model).filter((m) => m !== "__default__") ?? null;

  const params = useCallback(() => {
    const p = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
    if (status) p.set("status", status);
    if (model) p.set("model", model);
    if (projectId) p.set("project_id", projectId);
    if (hours) p.set("hours", String(hours));
    return p;
  }, [offset, status, model, projectId, hours]);

  // 同一个 hook 管三态：data === null 时是"还没回来"，不是"共 0 条"。
  // 翻页/换筛选会重新取；失败时保留上一页的数据并标陈旧（不把表清空）。
  const resource = useResource<{ items: Item[]; total: number }>(
    () => adminGet<{ items: Item[]; total: number }>(`/usage?${params().toString()}`),
    [offset, status, model, projectId, hours],
  );
  const items = resource.data?.items ?? null;
  const total = resource.data?.total ?? null;

  // Any filter change invalidates the offset: page 3 of a new filter usually does not exist.
  function change<T>(setter: (v: T) => void) {
    return (value: T) => {
      setOffset(0);
      setter(value);
    };
  }

  const projectLabel = (id: string) => {
    const p = (projects ?? []).find((x) => x.id === id);
    return p ? `${p.team}/${p.slug}` : id.slice(0, 8);
  };

  const pageCost = (items ?? []).reduce((sum, i) => sum + i.cost_cny, 0);
  const pageErrors = (items ?? []).filter((i) => i.status === "error").length;
  const exportHref = `/api/admin/usage/export.csv?${params().toString().replace(/limit=\d+&?/, "").replace(/offset=\d+&?/, "")}`;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>调用明细</h1>
          <div className="sub">
            {items === null || total === null ? (
              "正在读取调用记录…"
            ) : (
              <>
                共 {total} 条 · 本页 {items.length} 条
                {items.length > 0 ? ` · 本页成本 ${money(pageCost)}` : " · 本页无数据"}
                {pageErrors > 0 ? ` · 本页 ${pageErrors} 条错误` : ""}
              </>
            )}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {can("export") && (
            <a className="button-link" href={exportHref} download>
              导出 CSV
            </a>
          )}
          <button className="ghost" onClick={resource.reload}>
            刷新
          </button>
        </div>
      </div>

      <div className="panel">
        <div className="row">
          <div>
            <label>时间窗口</label>
            <select value={hours} onChange={(e) => change(setHours)(Number(e.target.value))}>
              {WINDOWS.map((w) => (
                <option key={w.hours} value={w.hours}>
                  {w.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label>状态</label>
            <select value={status} onChange={(e) => change(setStatus)(e.target.value)}>
              {STATUSES.map((s) => (
                <option key={s || "all"} value={s}>
                  {s || "全部"}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label>虚拟模型</label>
            <select value={model} onChange={(e) => change(setModel)(e.target.value)}>
              <option value="">全部</option>
              {(models ?? []).map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label>项目</label>
            <select value={projectId} onChange={(e) => change(setProjectId)(e.target.value)}>
              <option value="">全部</option>
              {(projects ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.team}/{p.slug}
                </option>
              ))}
            </select>
          </div>
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
      {items === null && resource.error && (
        <ErrorPanel error={resource.error} onRetry={resource.reload} />
      )}

      <div className="panel">
        {items === null ? (
          <LoadingBlock label="正在读取调用记录…" />
        ) : items.length === 0 ? (
          <div className="empty">这个筛选条件下没有调用记录</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>request_id</th>
                <th>项目</th>
                <th>模型</th>
                <th>provider</th>
                <th>tokens</th>
                <th>成本</th>
                <th>延迟</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {items.map((i) => (
                <tr key={i.request_id}>
                  <td className="mono">{shortTime(i.created_at)}</td>
                  <td className="mono">
                    <Link href={`/traces/${i.request_id}`} className="link">
                      {i.request_id.slice(0, 12)}…
                    </Link>
                  </td>
                  <td className="mono">{projectLabel(i.project_id)}</td>
                  <td className="mono" title={i.model_used}>
                    {i.model_requested}
                  </td>
                  <td className="mono">{i.provider}</td>
                  <td className="mono">
                    {i.input_tokens}+{i.output_tokens}
                  </td>
                  <td className="mono">{i.cost_cny === 0 ? <span className="badge blue">¥0</span> : money(i.cost_cny)}</td>
                  <td className="mono">{ms(i.latency_ms)}</td>
                  <td>{statusBadge(i)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        <div className="pager">
          <button
            className="ghost tiny"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE))}
          >
            上一页
          </button>
          <span className="sub">
            {total === null ? "—" : total === 0 ? "0" : `${offset + 1}–${Math.min(offset + PAGE, total)}`} /{" "}
            {total === null ? "—" : total}
          </span>
          <button
            className="ghost tiny"
            disabled={total === null || offset + PAGE >= total}
            onClick={() => setOffset(offset + PAGE)}
          >
            下一页
          </button>
        </div>
      </div>
    </>
  );
}