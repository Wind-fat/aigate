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
  scope_id: string;
  scope_slug: string;
  metric: string;
  period_key: string;
  value: number;
  limit_value: number;
  message: string;
  created_at: string;
  acknowledged_at: string | null;
  resolved_at: string | null;
  // 上游故障告警的结构化明细（provider / 连续失败数 / 首次失败时间 / 最近错误码 /
  // 持续秒数）。预算告警这个字段是空对象。
  detail: Record<string, unknown>;
};

const METRIC_LABEL: Record<string, string> = {
  budget_daily: "日预算",
  budget_monthly: "月预算",
  upstream_failure: "上游故障",
};

function resolvedNote(a: Alert): string | null {
  if (!a.resolved_at) return null;
  const seconds = typeof a.detail?.duration_s === "number" ? a.detail.duration_s : null;
  const lasted = seconds === null ? "" : `，持续 ${seconds}s`;
  return `已恢复 ${shortTime(a.resolved_at)}${lasted}`;
}

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
          <div className="empty">没有告警。预算到 80% 先来一条 warn，100% 升级成 critical；上游连续失败会来一条「上游故障」，恢复后自动标成已恢复。</div>
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
                    {resolvedNote(a) ? (
                      <span className="sub">{resolvedNote(a)}</span>
                    ) : a.acknowledged_at ? (
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
                      href={a.scope_type === "provider" ? "/providers" : "/projects"}
                      title={
                        a.scope_type === "provider"
                          ? "去供应商页看配置/密钥/连通性测试"
                          : "去预算配额页提额或暂停项目"
                      }
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
          <div>
            <b>上游故障</b>（<span className="mono">upstream_failure</span>）由网关自己写：
            同一家供应商连续失败到阈值（默认 3 次）就落一条 critical，带供应商名、连续失败数、
            首次失败时间和最近错误码；继续失败只更新这一条，不会刷屏。
            <b>已恢复</b>是供应商真的又能答了（不是有人点了确认）——自动恢复的故障仍然需要人看一眼。
          </div>
        </div>
      </div>
    </>
  );
}