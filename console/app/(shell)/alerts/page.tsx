"use client";

import { useState } from "react";
import { adminGet, adminPost, shortTime } from "@/lib/api";
import { ErrorPanel, LoadingBlock, StaleBanner } from "@/components/async-state";
import { useResource } from "@/lib/use-resource";

/**
 * 告警中心。
 *
 * 预算告警在概览页只是一条横条，处理它需要三件事：看清全部（含已确认）、
 * 知道该找谁、以及"确认"这个动作。放一页，是因为横条一多就变成背景噪音。
 *
 * 关键语义写在页面上：确认 ≠ 解决。确认只是把横条收起来；
 * 真要止血得提额或暂停项目。
 */

type Alert = {
  id: string;
  level: string;
  scope_type: string;
  scope_slug: string;
  metric: string;
  period_key: string;
  value: number;
  limit_value: number;
  message: string;
  created_at: string;
  acknowledged_at: string | null;
};

const METRIC_LABEL: Record<string, string> = {
  budget_daily: "日预算",
  budget_monthly: "月预算",
};

export default function AlertsPage() {
  const [includeAcked, setIncludeAcked] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // 未确认数是个结论：数据还没回来时写「0」，等于告诉值班「今天没事」。
  // null = 还没回来，[] = 真的没有告警（同 keys/ops 的三态约定）。
  const resource = useResource<{ items: Alert[] }>(
    () =>
      adminGet<{ items: Alert[] }>(`/alerts?limit=200&include_acknowledged=${includeAcked}`),
    [includeAcked],
  );
  const items = resource.data?.items ?? null;

  async function ack(id: string) {
    setBusy(id);
    try {
      await adminPost(`/alerts/${id}/ack`);
      resource.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const open = (items ?? []).filter((a) => !a.acknowledged_at);
  const critical = open.filter((a) => a.level === "critical");

  return (
    <>
      <div className="page-head">
        <div>
          <h1>告警</h1>
          <div className="sub">
            {items === null ? (
              "正在读取告警…"
            ) : (
              <>
                未确认 {open.length}
                {critical.length > 0 && ` · 其中 critical ${critical.length}`}
                {includeAcked ? " · 含已确认" : ""}
              </>
            )}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="ghost" onClick={() => setIncludeAcked((v) => !v)}>
            {includeAcked ? "只看未确认" : "显示已确认"}
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
      {items === null && resource.error && !error && (
        <ErrorPanel error={resource.error} onRetry={resource.reload} />
      )}

      {error && (
        <div className="panel" style={{ borderColor: "#5a1f1f" }}>
          <div className="err mono">{error}</div>
          <button className="ghost tiny" style={{ marginTop: 8 }} onClick={() => setError(null)}>
            知道了
          </button>
        </div>
      )}

      <div className="panel">
        {items === null ? (
          <LoadingBlock label="正在读取告警列表…" />
        ) : items.length === 0 ? (
          <div className="empty">没有告警。80% 会先来一条 warn，100% 会升级成 critical。</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>级别</th>
                <th>范围</th>
                <th>指标</th>
                <th>周期</th>
                <th>数值</th>
                <th>时间</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((a) => (
                <tr key={a.id}>
                  <td>
                    <span className={`badge ${a.level === "critical" ? "err" : "warn"}`}>{a.level}</span>
                  </td>
                  <td className="mono">
                    {a.scope_type} / {a.scope_slug}
                  </td>
                  <td>{METRIC_LABEL[a.metric] ?? a.metric}</td>
                  <td className="mono">{a.period_key}</td>
                  <td className="sub">{a.message}</td>
                  <td className="mono">{shortTime(a.created_at)}</td>
                  <td>
                    {a.acknowledged_at ? (
                      <span className="sub">已确认 {shortTime(a.acknowledged_at)}</span>
                    ) : (
                      <span className="badge warn">待处理</span>
                    )}
                  </td>
                  <td>
                    {!a.acknowledged_at && (
                      <button className="ghost tiny" disabled={busy === a.id} onClick={() => ack(a.id)}>
                        {busy === a.id ? "…" : "确认"}
                      </button>
                    )}
                    <a
                      className="ghost tiny"
                      style={{ marginLeft: 6 }}
                      href={`/projects`}
                      title="去预算配额页提额或暂停项目"
                    >
                      去处置
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="panel">
        <h2>这一页的语义</h2>
        <div className="sub" style={{ lineHeight: 1.9 }}>
          <div>
            <b>确认 ≠ 解决</b>。确认只是把概览页的横条收起来；业务还在花，超了还是会 429。
            真要止血：提额（预算配额页）或暂停项目（同一页）。
          </div>
          <div>
            <b>升级规则</b>：同一周期里 warn 升到 critical 会复用同一条并清空"已确认"——
            值班确认过的 80%，不能把 100% 也一起吞掉。
          </div>
          <div>
            <b>100% 之后</b>：调用方拿到 <span className="mono">gateway.budget_exhausted</span>，
            <span className="mono">scope</span> 字段说明是 key / project / team 哪一级。
          </div>
          <div>
            <b>日预算</b>防的是"凌晨跑飞的循环"，<b>月预算</b>防的是"月底才发现"。
            两个都配才完整。
          </div>
        </div>
      </div>
    </>
  );
}