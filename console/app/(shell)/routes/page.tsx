"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { adminGet, adminPost, adminPut } from "@/lib/api";
import { ErrorPanel, LoadingBlock, StaleBanner } from "@/components/async-state";
import { useResource } from "@/lib/use-resource";
import { useSession } from "@/lib/session";
import { toast } from "@/lib/toast";

type Price = {
  provider: string;
  model: string;
  input_price_per_1k: number;
  output_price_per_1k: number;
  enabled: boolean;
};
type Candidate = { provider: string; model: string };
type Policy = {
  match_model: string;
  strategy: string;
  candidates: Candidate[];
  fallback: Candidate[];
  shadow: Candidate[];
  timeout_ms?: number;
  max_retries?: number;
  enabled: boolean;
};

const STRATEGIES = ["static", "cost_first", "latency_first"];

const label = (c: Candidate) => `${c.provider}/${c.model}`;
const key = (c: Candidate) => `${c.provider}::${c.model}`;

/**
 * Model routing: the shape of every virtual model.
 *
 * A candidate that is not in `model_prices` is refused by the API, so the editor only
 * offers models that can actually be called. That single rule removes the most common
 * outage in gateways like this one — a typo saved at 23:50.
 */
type ChainProps = {
  which: "candidates" | "fallback" | "shadow";
  list: Candidate[];
  options: ReactNode;
  onChange: (which: "candidates" | "fallback" | "shadow", index: number, value: string) => void;
  onMove: (which: "candidates" | "fallback" | "shadow", index: number, delta: number) => void;
  onRemove: (which: "candidates" | "fallback" | "shadow", index: number) => void;
  onAdd: (which: "candidates" | "fallback" | "shadow") => void;
  canAdd: boolean;
};

/**
 * Defined at module scope on purpose: a component declared inside the page is a new
 * type on every render, so React unmounts and remounts it — the <select> loses focus
 * after each keystroke and the editor becomes unusable.
 */
function ChainEditor({ which, list, options, onChange, onMove, onRemove, onAdd, canAdd }: ChainProps) {
  return (
    <div className="stack">
      {list.length === 0 && <div className="sub">（空）</div>}
      {list.map((c, i) => (
        <div key={`${which}-${i}`} className="chain-row">
          <span className="idx">{i + 1}</span>
          <select value={`${c.provider}::${c.model}`} onChange={(e) => onChange(which, i, e.target.value)}>
            {options}
          </select>
          <button className="ghost tiny" onClick={() => onMove(which, i, -1)} disabled={i === 0}>
            ↑
          </button>
          <button
            className="ghost tiny"
            onClick={() => onMove(which, i, 1)}
            disabled={i === list.length - 1}
          >
            ↓
          </button>
          <button className="ghost tiny" onClick={() => onRemove(which, i)}>
            删除
          </button>
        </div>
      ))}
      <button className="ghost tiny" onClick={() => onAdd(which)} disabled={!canAdd}>
        + 添加
      </button>
    </div>
  );
}

function RoutesBoard() {
  const [editing, setEditing] = useState<string | null>(null);
  const [focusNote, setFocusNote] = useState<string | null>(null);
  const [draft, setDraft] = useState<Policy | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  // 要滚到哪条路由。由「自动打开编辑器」写，由「编辑器真的渲染出来」的 effect 消费。
  const [scrollTarget, setScrollTarget] = useState<string | null>(null);
  const scrolledFor = useRef<string | null>(null);
  const { canWrite, role } = useSession();

  const resource = useResource<{ prices: Price[]; policies: Policy[] }>(
    () => adminGet<{ prices: Price[]; policies: Policy[] }>("/models"),
    [],
  );
  // null = 还没回来；[] = 真的没有路由。以前首屏表格直接是空的，看起来像"没配过"。
  const policies = resource.data
    ? resource.data.policies.filter((p) => p.match_model !== "__default__")
    : null;
  const prices = resource.data ? resource.data.prices.filter((p) => p.enabled) : [];

  // 从告警页跳过来时带着 `?model=<虚拟模型>`（`unknown_provider` 的处置链接）：直接打开
  // 那条路由的候选链编辑器。只自动打开一次 —— 用户取消之后不能又被弹开。
  const params = useSearchParams();
  const focusModel = params.get("model");
  const focusedOnce = useRef<string | null>(null);
  const router = useRouter();

  useEffect(() => {
    if (!focusModel || !policies || focusedOnce.current === focusModel) return;
    focusedOnce.current = focusModel;
    // 参数用完就从地址栏拿掉：刷新 / 后退不会再弹开编辑器（以前 `focusedOnce` 是 ref，
    // 整页刷新即重置，于是「只自动打开一次」跨刷新并不成立）。`scroll: false` 是因为
    // Next 的默认行为会把页面滚回顶部，正好抵消下面那次定位。
    router.replace("/routes", { scroll: false });
    const target = policies.find((p) => p.match_model === focusModel);
    if (!target) {
      setFocusNote(`没找到路由 ${focusModel}：可能已被改名或删除，下面的路由列表供你对照。`);
      return;
    }
    // 只读角色（没有 routes:write）不该拿到一个「保存」能点的编辑器：说清找谁，而不是
    // 让他改一通再吃 403。
    if (!canWrite) {
      setFocusNote(
        `你的角色（${role ?? "只读"}）只能查看路由，不能改候选链：把这条告警（${focusModel}）转给 owner 或 admin 处理。`,
      );
      return;
    }
    setFocusNote(`从告警跳转过来：已打开 ${focusModel} 的候选链编辑器。`);
    startEdit(target);
    // 编辑器在页面下方，跳过来时可能不在视口里：滚过去，别让人上下找。
    // 这里只登记目标；真正滚动放到「编辑器已经渲染出来」之后（见下面两个 effect）。
    setScrollTarget(focusModel);
  }, [focusModel, policies, canWrite, role, router]);

  /**
   * 等编辑器真的渲染出来再滚。
   *
   * 以前是 `requestAnimationFrame(...)` 里滚一次：隐藏的标签页 / 后台窗口里 rAF 回调根本
   * 不执行（被冻结），于是「打开编辑器并滚过去」只剩前半句 —— `scrollIntoView` 一次都没
   * 调用，用户落地在页面顶部。以 `editing === scrollTarget` 为触发点就不依赖某一次的帧。
   *
   * 另一半点：隐藏文档里 `behavior: "smooth"` 的动画也不推进（同样靠帧），所以后台标签页
   * 改走瞬时定位，前台仍然平滑滚动。
   */
  useEffect(() => {
    if (!scrollTarget || editing !== scrollTarget) return;
    if (scrolledFor.current === scrollTarget) return;
    const el = document.getElementById(`route-${scrollTarget}`);
    if (!el) return;
    el.scrollIntoView({
      block: "center",
      behavior: document.visibilityState === "visible" ? "smooth" : "auto",
    });
    scrolledFor.current = scrollTarget;
  }, [scrollTarget, editing]);

  // 兜底：万一首次滚动的时机文档还是隐藏且瞬时定位没生效，等它第一次可见时再补一次。
  useEffect(() => {
    if (!scrollTarget) return;
    const onVisible = () => {
      if (document.visibilityState !== "visible" || scrolledFor.current === scrollTarget) return;
      const el = document.getElementById(`route-${scrollTarget}`);
      if (!el) return;
      el.scrollIntoView({ block: "center", behavior: "smooth" });
      scrolledFor.current = scrollTarget;
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [scrollTarget]);

  function load() {
    resource.reload();
  }

  function startEdit(p: Policy) {
    // 只读角色点不到「编辑」，自动打开那条路径也在上游挡了；这里是最后一道：状态对了才可能
    // 渲染出「保存」。
    if (!canWrite) return;
    setEditing(p.match_model);
    setDraft({
      ...p,
      candidates: [...p.candidates],
      fallback: [...p.fallback],
      shadow: [...(p.shadow ?? [])],
      timeout_ms: p.timeout_ms ?? 20000,
      max_retries: p.max_retries ?? 1,
    });
  }

  function setList(which: "candidates" | "fallback" | "shadow", list: Candidate[]) {
    if (!draft) return;
    setDraft({ ...draft, [which]: list });
  }

  function changeCandidate(which: "candidates" | "fallback" | "shadow", index: number, value: string) {
    if (!draft) return;
    const [provider, model] = value.split("::");
    const list = [...draft[which]];
    list[index] = { provider, model };
    setList(which, list);
  }

  function move(which: "candidates" | "fallback" | "shadow", index: number, delta: number) {
    if (!draft) return;
    const list = [...draft[which]];
    const target = index + delta;
    if (target < 0 || target >= list.length) return;
    [list[index], list[target]] = [list[target], list[index]];
    setList(which, list);
  }

  function addCandidate(which: "candidates" | "fallback" | "shadow") {
    if (!draft || prices.length === 0) return;
    const used = new Set([...draft.candidates, ...draft.fallback, ...draft.shadow].map(key));
    const free = prices.find((p) => !used.has(key({ provider: p.provider, model: p.model })));
    const pick = free ?? prices[0];
    setList(which, [...draft[which], { provider: pick.provider, model: pick.model }]);
  }

  async function save() {
    if (!draft) return;
    if (!canWrite) {
      toast("你的角色只能查看路由，不能保存（需要 routes:write）", "err");
      return;
    }
    setBusy(true);
    try {
      const res = await adminPut<{ warnings?: string[] }>(`/routes/${draft.match_model}`, {
        candidates: draft.candidates,
        fallback: draft.fallback,
        strategy: draft.strategy,
        timeout_ms: draft.timeout_ms,
        max_retries: draft.max_retries,
        enabled: draft.enabled,
      });
      toast(`${draft.match_model} 已保存，下一条请求即生效`);
      for (const w of res.warnings ?? []) toast(w, "err");
      setEditing(null);
      setDraft(null);
      // 编辑器收起了，就别再挂着「已打开 … 编辑器」这句话（滚动的目标也一起清掉，
      // 否则用户自己点「编辑」时会再被滚一次）。
      setFocusNote(null);
      setScrollTarget(null);
      load();
    } catch (e) {
      toast((e as Error).message, "err");
    } finally {
      setBusy(false);
    }
  }

  async function create() {
    const name = newName.trim();
    if (!name || prices.length === 0) return;
    try {
      const res = await adminPost<{ warnings?: string[] }>(
        `/routes?match_model=${encodeURIComponent(name)}&candidates=${encodeURIComponent(
          `${prices[0].provider}/${prices[0].model}`,
        )}`,
      );
      toast(`已创建 ${name}（候选可再编辑）`);
      for (const w of res.warnings ?? []) toast(w, "err");
      setNewName("");
      load();
    } catch (e) {
      toast((e as Error).message, "err");
    }
  }

  /**
   * Options = priced models + whatever this route already points at.
   *
   * Demo and legacy routes can reference a model that has no price row (a mock that
   * always fails, for instance). Leaving it out of the list would make the <select>
   * fall back to the first option and silently rewrite the route on save.
   */
  function options() {
    const seen = new Set<string>();
    const rows: { value: string; text: string; unpriced?: boolean }[] = [];
    for (const p of prices) {
      const value = `${p.provider}::${p.model}`;
      seen.add(value);
      rows.push({ value, text: `${p.provider}/${p.model}` });
    }
    for (const c of [...(draft?.candidates ?? []), ...(draft?.fallback ?? [])]) {
      const value = `${c.provider}::${c.model}`;
      if (!seen.has(value)) {
        seen.add(value);
        rows.push({ value, text: `${c.provider}/${c.model}（未定价）`, unpriced: true });
      }
    }
    return rows.map((r) => (
      <option key={r.value} value={r.value}>
        {r.text}
      </option>
    ));
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>模型路由</h1>
          <div className="sub">
            业务代码只认虚拟模型名；改候选链不需要重启，也不改业务代码
          </div>
        </div>
        <button className="ghost" onClick={load}>
          刷新
        </button>
      </div>

      {focusNote && (
        <div className="panel">
          <div className="sub">{focusNote}</div>
        </div>
      )}

      {policies !== null && resource.error && (
        <StaleBanner
          loadedAt={resource.loadedAt}
          failedAt={resource.failedAt}
          error={resource.error}
          onRetry={resource.reload}
        />
      )}
      {policies === null && resource.error && !error && (
        <ErrorPanel error={resource.error} onRetry={resource.reload} />
      )}

      {error && (
        <div className="panel">
          <div className="err mono">{error}</div>
        </div>
      )}

      {canWrite ? (
      <div className="panel">
        <h2>新建虚拟模型</h2>
        <div className="row">
          <div>
            <label>模型名（业务代码里填这个）</label>
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="chat-cheap"
            />
          </div>
          <div style={{ flex: "0 0 auto", minWidth: 0 }}>
            <button onClick={create} disabled={!newName.trim() || prices.length === 0}>
              创建
            </button>
          </div>
        </div>
        <div className="sub" style={{ marginTop: 8 }}>
          创建后可在下方编辑候选链。第一个候选默认用最便宜的可用模型（{prices[0] ? label(prices[0]) : "—"}）。
        </div>
      </div>
      ) : (
        <div className="panel">
          <div className="sub">你的角色（{role ?? "viewer"}）只能看路由配置，不能编辑候选链。需要修改找 owner。</div>
        </div>
      )}

      {policies === null ? (
        <div className="panel">
          <LoadingBlock label="正在读取路由与价格…" />
        </div>
      ) : null}
      {(policies ?? []).map((p) => (
        <div className="panel" key={p.match_model} id={`route-${p.match_model}`}>
          <div className="page-head" style={{ marginBottom: 10 }}>
            <div>
              <h2 style={{ margin: 0 }}>
                <span className="mono">{p.match_model}</span>{" "}
                {!p.enabled && <span className="badge warn">disabled</span>}
              </h2>
              <div className="sub">
                策略 {p.strategy} · 候选 {p.candidates.length} 个
                {p.fallback.length > 0 ? ` · 降级 ${p.fallback.length} 个` : " · 无降级链路"}
                {(p.shadow?.length ?? 0) > 0 ? ` · 影子 ${p.shadow.length} 个` : ""}
              </div>
            </div>
            {editing === p.match_model || !canWrite ? null : (
              <button className="ghost" onClick={() => startEdit(p)}>
                编辑
              </button>
            )}
          </div>

          {editing === p.match_model && draft ? (
            <>
              <div className="row" style={{ marginBottom: 10 }}>
                <div>
                  <label>策略</label>
                  <select
                    value={draft.strategy}
                    onChange={(e) => setDraft({ ...draft, strategy: e.target.value })}
                  >
                    {STRATEGIES.map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label>超时 (ms)</label>
                  <input
                    type="number"
                    value={draft.timeout_ms ?? 20000}
                    onChange={(e) => setDraft({ ...draft, timeout_ms: Number(e.target.value) })}
                  />
                </div>
                <div>
                  <label>重试次数</label>
                  <input
                    type="number"
                    min="0"
                    max="5"
                    value={draft.max_retries ?? 1}
                    onChange={(e) => setDraft({ ...draft, max_retries: Number(e.target.value) })}
                  />
                </div>
                <div style={{ flex: "0 0 auto", minWidth: 0 }}>
                  <label>启用</label>
                  <input
                    type="checkbox"
                    checked={draft.enabled}
                    onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })}
                    style={{ width: 18, height: 18, marginTop: 6 }}
                  />
                </div>
              </div>

              <label>候选链（按顺序尝试）</label>
              <ChainEditor
                which="candidates"
                list={draft.candidates}
                options={options()}
                onChange={changeCandidate}
                onMove={move}
                onRemove={(w, i) => setList(w, draft[w].filter((_, j) => j !== i))}
                onAdd={addCandidate}
                canAdd={prices.length > 0}
              />

              <label style={{ marginTop: 14 }}>降级链（候选全部失败后按顺序尝试）</label>
              <ChainEditor
                which="fallback"
                list={draft.fallback}
                options={options()}
                onChange={changeCandidate}
                onMove={move}
                onRemove={(w, i) => setList(w, draft[w].filter((_, j) => j !== i))}
                onAdd={addCandidate}
                canAdd={prices.length > 0}
              />

              <label style={{ marginTop: 14 }}>影子流量（只记录、不返回给调用方，单独记账）</label>
              <ChainEditor
                which="shadow"
                list={draft.shadow}
                options={options()}
                onChange={changeCandidate}
                onMove={move}
                onRemove={(w, i) => setList(w, draft[w].filter((_, j) => j !== i))}
                onAdd={addCandidate}
                canAdd={prices.length > 0}
              />
              <div className="sub" style={{ marginBottom: 10 }}>
                影子调用在响应返回之后才发，失败不影响调用方；花费进 shadow_runs，不进账单。
              </div>

              <div style={{ marginTop: 14 }}>
                <button onClick={save} disabled={busy || !canWrite || draft.candidates.length === 0}>
                  {busy ? "保存中…" : "保存"}
                </button>
                <button
                  className="ghost"
                  onClick={() => {
                    setEditing(null);
                    setDraft(null);
                    // 取消之后提示条不能还写着「已打开 … 编辑器」（它已经不在屏幕上了）。
                    setFocusNote(null);
                    setScrollTarget(null);
                  }}
                  style={{ marginLeft: 8 }}
                >
                  取消
                </button>
              </div>
            </>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>序</th>
                  <th>候选</th>
                  <th>角色</th>
                </tr>
              </thead>
              <tbody>
                {p.candidates.map((c, i) => (
                  <tr key={`c-${i}`}>
                    <td className="mono">{i + 1}</td>
                    <td className="mono">{label(c)}</td>
                    <td>
                      <span className="badge blue">primary</span>
                    </td>
                  </tr>
                ))}
                {p.fallback.map((c, i) => (
                  <tr key={`f-${i}`}>
                    <td className="mono">—</td>
                    <td className="mono">{label(c)}</td>
                    <td>
                      <span className="badge warn">fallback</span>
                    </td>
                  </tr>
                ))}
                {p.candidates.length === 0 && p.fallback.length === 0 && (
                  <tr>
                    <td colSpan={3} className="sub">
                      这个路由没有候选——请求会直接 502。点右上角「编辑」补上。
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      ))}

      <div className="panel">
        <h2>可用模型（来自单价表）</h2>
        <table>
          <thead>
            <tr>
              <th>provider</th>
              <th>model</th>
              <th>输入 / 1K</th>
              <th>输出 / 1K</th>
            </tr>
          </thead>
          <tbody>
            {prices.map((p) => (
              <tr key={`${p.provider}-${p.model}`}>
                <td className="mono">{p.provider}</td>
                <td className="mono">{p.model}</td>
                <td className="mono">¥{p.input_price_per_1k}</td>
                <td className="mono">¥{p.output_price_per_1k}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="sub" style={{ marginTop: 8 }}>
          只有这张表里启用的模型才能被选为候选——写入时就校验，避免拼错模型名上线后才 502。
        </div>
      </div>
    </>
  );
}

/**
 * `useSearchParams` 要求使用它的子树处在 Suspense 里：否则 `next build` 会失败
 * （`next dev` 不报）。同 `app/login/page.tsx`。
 */
export default function RoutesPage() {
  return (
    <Suspense
      fallback={
        <div className="panel">
          <LoadingBlock label="正在读取路由与价格…" />
        </div>
      }
    >
      <RoutesBoard />
    </Suspense>
  );
}
