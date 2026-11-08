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
  // 配置类：缺密钥 / 被停用 / 路由指向不存在的 provider。warn 级别，要有人去改配置。
  provider_config: "配置问题",
};

function resolvedNote(a: Alert): string | null {
  if (!a.resolved_at) return null;
  const seconds = typeof a.detail?.duration_s === "number" ? a.detail.duration_s : null;
  const lasted = seconds === null ? "" : `，持续 ${seconds}s`;
  return `已恢复 ${shortTime(a.resolved_at)}${lasted}`;
}

/**
 * 处置链接指向**该动手的那一页**。
 *
 * `unknown_provider`（路由里写了一个不存在的 provider）要改的是**路由**，不是供应商：
 * 以前一律链到 `/providers`，值班到了那儿只看到一堆供应商，找不到该改的东西。带上
 * `?model=` 直接打开那条路由的候选链编辑器。
 */
function fixAction(a: Alert): { href: string; label: string; title: string } {
  const detail = a.detail ?? {};
  const reason = typeof detail.reason === "string" ? detail.reason : "";
  const model = typeof detail.model === "string" ? detail.model : "";
  if (a.metric === "provider_config") {
    if (reason === "unknown_provider") {
      return {
        href: model ? `/routes?model=${encodeURIComponent(model)}` : "/routes",
        label: "去改路由",
        title: model
          ? `路由 ${model} 指向了不存在的 provider：去模型路由页修候选链`
          : "路由指向了不存在的 provider：去模型路由页修候选链",
      };
    }
    return {
      href: "/providers",
      label: "去修配置",
      title: "去供应商页改配置：补密钥 / 重新启用",
    };
  }
  if (a.scope_type === "provider") {
    return { href: "/providers", label: "去处置", title: "去供应商页看配置/密钥/连通性测试" };
  }
  return { href: "/projects", label: "去处置", title: "去预算配额页提额或暂停项目" };
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
  const warn = open.filter((a) => a.level !== "critical");

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
                {critical.length > 0 && ` · critical ${critical.length}`}
                {warn.length > 0 && ` · warn ${warn.length}`}
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
          <div className="empty">没有告警。预算到 80% 先来一条 warn，100% 升级成 critical；上游连续失败会来一条「上游故障」，恢复后自动标成已恢复；配置问题（缺密钥 / 被停用 / 路由指向不存在的 provider）来一条 warn。</div>
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
              {items.map((a) => {
                const fix = fixAction(a);
                return (
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
                      href={fix.href}
                      title={fix.title}
                    >
                      {fix.label}
                    </a>
                  </td>
                </tr>
                );
              })}
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
            <b>配置问题</b>（<span className="mono">provider_config</span>，warn）是另一种事故：
            供应商缺密钥、被手动停用、路由指向一个不存在的 provider。它不会自己好，所以要有人去改，
            但没有 critical 那么急 —— critical 留给「上游在挂、流量正在丢」。同一家供应商同一时刻只有
            一条，`detail.fix` 就是该执行的那一步；配置生效后下一次调用会自动标成已恢复。
          </div>
          <div>
            <b>残留告警会被对账收口</b>：聚合窗口在进程内存里，开告警的副本先退出 / 重启后
            进程对旧行没有记忆 —— 所以网关启动时和每 5 分钟做一次对账，只关「现在能证明已经正常」
            的行（配置已好；上游故障还要求上游此刻真的在应答），并写
            <span className="mono"> detail.closed_by=&quot;reconcile&quot;</span> 留痕。
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