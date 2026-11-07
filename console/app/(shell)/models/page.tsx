"use client";

import { useState } from "react";
import { adminGet, pct } from "@/lib/api";
import { ErrorPanel, LoadingBlock, StaleBanner } from "@/components/async-state";
import { useResource } from "@/lib/use-resource";

/**
 * 模型对比：把「换更便宜的模型值不值」变成一个能看的表。
 *
 * 两个诚实的地方：
 * 1. 只有跑过影子流量的模型才有 `similarity`；没有的显示「—」，不拿行业平均值凑数。
 * 2. 成本按「每千次调用」排，不按单价排——单价不同、token 长度不同，比单价没有意义。
 */

type Row = {
  provider: string;
  model: string;
  calls: number;
  /** 没有调用时后端返回 null（未知），不是 0.0。 */
  success_rate: number | null;
  p95_ms: number | null;
  avg_ms: number;
  cost_per_call: number;
  cost_per_1k: number;
  avg_tokens: number;
  similarity: number | null;
  shadow_runs: number;
};

/**
 * 影子汇总行（`/admin/v1/shadow/summary`）。和上面那张表的口径**不同**：
 * 分母是「影子跑了几次」，成功率来自 `usage_stats(strict=False)`；
 * `shadow_cost_cny` 是影子链路花的钱（不进账单）。两个数放在一起就是为了让人看出
 * 「相似度高但影子一直在失败」这种组合——那是最不该切过去的时机。
 */
type ShadowRow = {
  provider: string;
  model: string;
  runs: number;
  success_rate: number | null;
  avg_similarity: number | null;
  avg_latency_ms: number;
  shadow_cost_cny: number;
};

function money(v: number): string {
  return v < 0.01 ? `¥${v.toFixed(6)}` : `¥${v.toFixed(3)}`;
}

export default function ModelsPage() {
  const [days, setDays] = useState(7);
  const resource = useResource<{ items: Row[] }>(
    () => adminGet<{ items: Row[] }>(`/model-compare?days=${days}`),
    [days],
  );
  // 影子链自己的汇总：一次请求、一张表。谁跑了影子、跑了多少次、成功率多少、花了多少钱。
  const shadows = useResource<{ items: ShadowRow[] }>(
    () => adminGet<{ items: ShadowRow[] }>(`/shadow/summary?days=${days}`),
    [days],
  );
  // data === null 才是"还没回来"；[] 是真的没有调用。两者以前被渲染成同一句话。
  const rows = resource.data?.items ?? null;
  // 同上：null = 还没回来，[] = 真的没有影子流量。
  const shadowRows = shadows.data?.items ?? null;

  const cheapest = rows ? Math.min(...rows.map((r) => r.cost_per_1k), 1) : null;
  const maxCost = Math.max(...(rows ?? []).map((r) => r.cost_per_1k), 0.0001);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>模型对比</h1>
          <div className="sub">
            近 {days} 天的真实调用：成本、成功率、P95、与主回答的相似度。换模型之前先看这张表。
          </div>
        </div>
        <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
          <option value={1}>近 1 天</option>
          <option value={7}>近 7 天</option>
          <option value={30}>近 30 天</option>
        </select>
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
          <LoadingBlock label="正在读取窗口内的调用…" />
        ) : rows.length === 0 ? (
          <div className="empty">这段时间没有调用记录</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>provider / model</th>
                <th>调用</th>
                <th>成功率</th>
                <th>P95</th>
                <th>每次成本</th>
                <th>每千次成本</th>
                <th>均 tokens</th>
                <th>相似度</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={`${r.provider}/${r.model}`}>
                  <td>
                    <div className="mono">
                      {r.provider}/{r.model}
                    </div>
                    {r.cost_per_1k === cheapest && <span className="sub">最便宜</span>}
                  </td>
                  <td className="mono">{r.calls}</td>
                  <td>
                    {r.success_rate === null ? (
                      <span className="badge" title="窗口内没有调用">—</span>
                    ) : (
                      <span
                        className={`badge ${r.success_rate >= 99 ? "ok" : r.success_rate >= 90 ? "warn" : "err"}`}
                      >
                        {pct(r.success_rate)}
                      </span>
                    )}
                  </td>
                  <td className="mono">{r.p95_ms === null ? "—" : `${r.p95_ms} ms`}</td>
                  <td className="mono">{money(r.cost_per_call)}</td>
                  <td className="mono">
                    ¥{r.cost_per_1k.toFixed(2)}
                    <div className="meter" style={{ marginTop: 4 }}>
                      <div
                        className="meter-fill"
                        style={{ width: `${Math.max(3, (r.cost_per_1k / maxCost) * 100)}%` }}
                      />
                    </div>
                  </td>
                  <td className="mono">{r.avg_tokens}</td>
                  <td>
                    {r.similarity === null ? (
                      <span className="sub" title="没有跑过影子流量">
                        —
                      </span>
                    ) : (
                      <span className="mono">
                        {r.similarity.toFixed(2)}
                        <span className="sub"> · {r.shadow_runs} 次</span>
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="panel">
        <h2>影子链：跑了多少、成不成功、花了多少</h2>
        <div className="sub" style={{ marginBottom: 10 }}>
          分母是「影子跑了几次」，不是网关调用数；影子花费记账但不进账单。
        </div>
        {shadowRows === null && shadows.error && (
          <ErrorPanel error={shadows.error} onRetry={shadows.reload} />
        )}
        {shadowRows === null && !shadows.error ? (
          <LoadingBlock label="正在读取影子汇总…" />
        ) : shadowRows !== null && shadowRows.length === 0 ? (
          <div className="empty">这段时间没有影子流量</div>
        ) : (
          shadowRows !== null && (
            <table>
              <thead>
                <tr>
                  <th>provider / model</th>
                  <th>影子次数</th>
                  <th>影子成功率</th>
                  <th>平均相似度</th>
                  <th>平均延迟</th>
                  <th>影子花费</th>
                </tr>
              </thead>
              <tbody>
                {shadowRows.map((r) => (
                  <tr key={`shadow-${r.provider}/${r.model}`}>
                    <td className="mono">
                      {r.provider}/{r.model}
                    </td>
                    <td className="mono">{r.runs}</td>
                    <td>
                      {r.success_rate === null ? (
                        <span className="badge" title="窗口内没有影子行">—</span>
                      ) : (
                        <span
                          className={`badge ${r.success_rate >= 99 ? "ok" : r.success_rate >= 90 ? "warn" : "err"}`}
                        >
                          {pct(r.success_rate)}
                        </span>
                      )}
                    </td>
                    <td className="mono">
                      {r.avg_similarity === null ? "—" : r.avg_similarity.toFixed(3)}
                    </td>
                    <td className="mono">{Math.round(r.avg_latency_ms)} ms</td>
                    <td className="mono">{money(r.shadow_cost_cny)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        )}
      </div>

      <div className="panel">
        <h2>怎么用这张表</h2>
        <div className="sub" style={{ lineHeight: 1.9 }}>
          <div>
            <b>成本</b>按每千次调用看：单价低不等于便宜，输出长、重试多的模型总价更高。
          </div>
          <div>
            <b>相似度</b>是影子流量里「候选答案与主答案」的字符级相似度，只是烟雾信号，不是质量评分。
            想给某个模型补这一列：在「模型路由」里把它加进影子链，跑几天再回来看。
          </div>
          <div>
            <b>P95 高于均值很多</b>说明有长尾（通常是输出很长或上游抖动），成本优先策略下先看这一列。
          </div>
        </div>
      </div>
    </>
  );
}