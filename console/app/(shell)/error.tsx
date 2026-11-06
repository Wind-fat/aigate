"use client";

/**
 * 页面级错误边界。
 *
 * 没有它，一个渲染异常会变成 Next 的默认错误页或白屏——用户只会说"系统坏了"。
 * 这里给三样东西：发生了什么（message）、能做什么（重试 / 回概览）、以及 request_id 的思路
 * （控制台的错通常来自某个 API，页面上直接给出排查方向）。
 */
export default function ShellError({ error, reset }: { error: Error; reset: () => void }) {
  return (
    <div className="panel">
      <h2>这个页面出错了</h2>
      <div className="err mono" style={{ marginBottom: 10 }}>
        {error.message}
      </div>
      <div className="sub" style={{ marginBottom: 12, lineHeight: 1.9 }}>
        <div>常见原因：网关没起（8080）、控制台的 token 与服务端不一致、或数据库刚被迁移过。</div>
        <div>
          先看「运行状态」页；如果那一页也打不开，问题在网关而不是这个页面。
        </div>
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <button onClick={reset}>重试</button>
        <a
          className="ghost"
          href="/ops"
          style={{ padding: "6px 12px", border: "1px solid var(--border)", borderRadius: 6 }}
        >
          去运行状态
        </a>
      </div>
    </div>
  );
}