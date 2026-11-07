"use client";

import { useEffect, useMemo, useState } from "react";
import { adminGet, adminPost } from "@/lib/api";
import { ErrorPanel, LoadingBlock, StaleBanner } from "@/components/async-state";
import { useResource } from "@/lib/use-resource";
import { toast } from "@/lib/toast";

/**
 * prompt 版本库。
 *
 * 这一页只做三件事，刻意不做第四件：
 *   1. 看：同一个名字有哪些版本、现在用哪个、谁在什么时候建的；
 *   2. 建：新建一个版本（不会自动生效）；
 *   3. 切：把某个版本设为当前版本（"回滚"就是切回旧版本）。
 *
 * 不做：变量渲染、A/B 分流、审批。它们是上层应用的事，做进来这一层就变成半个 prompt 平台。
 *
 * 上线顺序写在这里，因为这是最容易搞反的地方：
 *   激活 → 把 `X-Prompt-Version` 改成新版本号 → 缓存自动失效 → 账单按新版本归集。
 * 只激活、不改请求头，等于什么都没发生。
 */

type Prompt = {
  id: string;
  name: string;
  version: string;
  active: boolean;
  notes: string;
  created_by: string;
  created_at: string | null;
  content: string;
  chars: number;
};

export default function PromptsPage() {
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Prompt | null>(null);
  const [form, setForm] = useState({ name: "", version: "", content: "", notes: "" });
  const [showForm, setShowForm] = useState(false);
  const resource = useResource<{ items: Prompt[] }>(() => adminGet<{ items: Prompt[] }>("/prompts"), []);
  // null = 首屏还在读；[] = 真的一个版本都没有。
  const items = resource.data?.items ?? null;

  // 默认选中第一个 prompt。以前这个副作用写在 load() 里，和"加载态"混在一起。
  useEffect(() => {
    if (items && items.length > 0 && !selected) setSelected(items[0].name);
  }, [items, selected]);

  const names = useMemo(() => Array.from(new Set((items ?? []).map((i) => i.name))), [items]);
  const versions = useMemo(
    () => (items ?? []).filter((i) => i.name === selected).sort((a, b) => a.version.localeCompare(b.version)),
    [items, selected],
  );

  async function openDetail(id: string) {
    try {
      const d = await adminGet<{ item: Prompt }>(`/prompts/${id}`);
      setDetail(d.item);
    } catch (e) {
      toast((e as Error).message, "err");
    }
  }

  async function activate(id: string) {
    try {
      await adminPost(`/prompts/${id}/activate`);
      toast("已切换当前版本：记得把请求头 X-Prompt-Version 一起改");
      resource.reload();
    } catch (e) {
      toast((e as Error).message, "err");
    }
  }

  async function create() {
    try {
      const payload = {
        name: form.name.trim() || selected || "",
        version: form.version.trim(),
        content: form.content,
        notes: form.notes,
      };
      await adminPost("/prompts", payload);
      toast(`${payload.name}@${payload.version} 已创建（未生效）`);
      setForm({ name: "", version: "", content: "", notes: "" });
      setShowForm(false);
      resource.reload();
    } catch (e) {
      toast((e as Error).message, "err");
    }
  }

  const active = versions.find((v) => v.active);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Prompt 版本</h1>
          <div className="sub">
            {items === null ? "—" : names.length} 个 prompt · {items === null ? "—" : items.length} 个版本 · 当前：
            {active ? `${active.name}@${active.version}` : items === null ? "读取中" : "无"}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="ghost" onClick={() => setShowForm((v) => !v)}>
            {showForm ? "取消" : "新建版本"}
          </button>
          <button className="ghost" onClick={resource.reload}>
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
      {items === null && resource.error && (
        <ErrorPanel error={resource.error} onRetry={resource.reload} />
      )}

      {showForm && (
        <div className="panel">
          <h2>新建版本</h2>
          <div className="row" style={{ alignItems: "flex-end" }}>
            <div>
              <label>名字</label>
              <input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder={selected ?? "support-reply"}
              />
            </div>
            <div>
              <label>版本号（字母数字 . _ -）</label>
              <input
                value={form.version}
                onChange={(e) => setForm({ ...form, version: e.target.value })}
                placeholder="v3"
              />
            </div>
            <div style={{ flex: 2 }}>
              <label>备注（写清这次改了什么，三个月后只有它能解释账单）</label>
              <input
                value={form.notes}
                onChange={(e) => setForm({ ...form, notes: e.target.value })}
                placeholder="收紧长度限制，禁止承诺时效"
              />
            </div>
          </div>
          <div style={{ marginTop: 10 }}>
            <label>内容（变量原样保留，例如 {"{{q}}"}）</label>
            <textarea
              rows={6}
              style={{ width: "100%", fontFamily: "var(--font-mono, monospace)", fontSize: 12 }}
              value={form.content}
              onChange={(e) => setForm({ ...form, content: e.target.value })}
              placeholder="你是客服，回答要短……"
            />
          </div>
          <button
            style={{ marginTop: 10 }}
            onClick={create}
            disabled={!form.version.trim() || !form.content.trim() || !(form.name.trim() || selected)}
          >
            创建（不会自动生效）
          </button>
        </div>
      )}

      <div className="split">
        <div className="panel">
          <h2>Prompt</h2>
          {items === null ? (
            <LoadingBlock label="正在读取 prompt 版本…" />
          ) : names.length === 0 ? (
            <div className="empty">还没有任何 prompt 版本</div>
          ) : (
            <div className="barlist">
              {names.map((n) => {
                const cur = items.find((i) => i.name === n && i.active);
                return (
                  <div
                    key={n}
                    className={`barline ${selected === n ? "sel" : ""}`}
                    onClick={() => setSelected(n)}
                    style={{ cursor: "pointer" }}
                  >
                    <div className="barline-label mono">{n}</div>
                    <div className="sub">
                      {cur ? `当前 ${cur.version}` : "没有生效版本"}
                      {" · "}
                      {items.filter((i) => i.name === n).length} 个版本
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="panel">
          <h2 style={{ textTransform: "none" }}>{selected ?? "版本"}</h2>
          {items === null ? (
            <LoadingBlock label="正在读取 prompt 版本…" />
          ) : versions.length === 0 ? (
            <div className="empty">选左边一个 prompt</div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>版本</th>
                  <th>状态</th>
                  <th>字符</th>
                  <th>说明</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {versions.map((v) => (
                  <tr key={v.id}>
                    <td className="mono">{v.version}</td>
                    <td>{v.active ? <span className="badge ok">当前</span> : <span className="sub">历史</span>}</td>
                    <td className="mono">{v.chars}</td>
                    <td className="sub">{v.notes || "—"}</td>
                    <td>
                      <button className="ghost tiny" onClick={() => openDetail(v.id)}>
                        查看
                      </button>
                      {!v.active && (
                        <button className="ghost tiny" onClick={() => activate(v.id)}>
                          设为当前
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {detail && (
        <div className="panel">
          <h2>
            <span className="mono">
              {detail.name}@{detail.version}
            </span>
          </h2>
          <div className="sub" style={{ marginBottom: 8 }}>
            {detail.created_by} · {detail.created_at?.slice(0, 19).replace("T", " ")} · {detail.chars} 字符
          </div>
          <pre className="prompt-body">{detail.content}</pre>
          <button className="ghost tiny" style={{ marginTop: 10 }} onClick={() => setDetail(null)}>
            关闭
          </button>
        </div>
      )}

      <div className="panel">
        <h2>它和工作台、账单怎么联动</h2>
        <div className="sub" style={{ lineHeight: 1.9 }}>
          <div>
            <b>缓存</b>：缓存 key 里带 prompt 版本，所以换版本天然不会命中旧答案，不用清缓存。
          </div>
          <div>
            <b>账单</b>：每个请求的版本号落进 <span className="mono">usage_records.prompt_version</span>，
            「调用明细」可按版本过滤——某周变贵是换了版本还是业务量涨了，一查就知道。
          </div>
          <div>
            <b>生效方式</b>：这里「设为当前」只是改了网关认定的当前版本；
            业务方需要把请求头 <span className="mono">X-Prompt-Version</span> 改成新版本号才会用上。
            两步都没做就以为生效了，是这套机制唯一的坑。
          </div>
        </div>
      </div>
    </>
  );
}