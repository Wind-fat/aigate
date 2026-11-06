"use client";

import { useEffect, useState } from "react";
import { adminGet } from "@/lib/api";

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
  success_rate: number;
  p95_ms: number;
  avg_ms: number;
  cost_per_call: number;
  cost_per_1k: number;
  avg_tokens: number;
  similarity: number | null;
  shadow_runs: number;
};

function money(v: number): string {
  return v < 0.01 ? `¥${v.toFixed(6)}` : `¥${v.toFixed(3)}`;
}

export default function ModelsPage() {
  const [rows, setRows] = useState<Row[]>([]);
  const [days, setDays] = useState(7);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    adminGet<{ items: Row[] }>(`/model-compare?days=${days}`)
      .then((d) => {
        setRows(d.items);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, [days]);

  const cheapest = Math.min(...rows.map((r) => r.cost_per_1k), 1);
  const maxCost = Math.max(...rows.map((r) => r.cost_per_1k), 0.0001);

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

      {error && (
        <div className="panel">
          <div className="err mono">{error}</div>
        </div>
      )}

      <div className="panel">
        {rows.length === 0 ? (
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
                    <span className={`badge ${r.success_rate >= 99 ? "ok" : r.success_rate >= 90 ? "warn" : "err"}`}>
                      {r.success_rate}%
                    </span>
                  </td>
                  <td className="mono">{r.p95_ms} ms</td>
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