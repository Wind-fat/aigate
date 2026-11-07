"use client";

import { useEffect, useState } from "react";
import { agoText, clockText } from "@/lib/use-resource";

/**
 * 三态里的两个"没有数据"分支 + 一个"有旧数据但刷新失败"的横条。
 *
 * 这些判断以前散在 8 个页面里，写法各不相同：有的 `useState([])` 直接渲染空态，
 * 有的 `if (error) return <整页错误>` 把已加载的数据丢掉。集中在这里之后，
 * 页面只需要问一句 `data === null ?`。
 */

/** 首次加载：还不知道有没有数据。绝不渲染"共 0 条"这种结论。 */
export function LoadingBlock({ label = "加载中…" }: { label?: string }) {
  return (
    <div className="empty" aria-busy="true" aria-live="polite">
      {label}
    </div>
  );
}

/** 首次加载就失败：没有旧数据可显示，只能给错误 + 重试。 */
export function ErrorPanel({
  error,
  title = "加载失败",
  onRetry,
}: {
  error: string;
  title?: string;
  onRetry?: () => void;
}) {
  return (
    <div className="panel">
      <h2>{title}</h2>
      <div className="err mono" style={{ marginBottom: 10 }}>
        {error}
      </div>
      <div className="sub" style={{ marginBottom: 12 }}>
        数据一条都没取到——这屏现在什么都说明不了。先重试；若提示登录过期，去登录页重新登录。
      </div>
      {onRetry && (
        <button className="ghost" onClick={onRetry}>
          重试
        </button>
      )}
    </div>
  );
}

/**
 * 有数据、但最近一次刷新失败。
 *
 * 轮询（概览 10s / 运行状态 5s）抖动一次就把整页换成"连不上网关"，等于把上一秒
 * 还正确的数据也一起扔了。这里保留旧数据，只把"这些数字是什么时候的"写清楚。
 */
export function StaleBanner({
  loadedAt,
  failedAt,
  error,
  onRetry,
}: {
  loadedAt: number | null;
  failedAt: number | null;
  error: string;
  onRetry?: () => void;
}) {
  // "12 秒前"要自己走，否则屏上的陈旧时钟会假装数据是新的。
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 5_000);
    return () => clearInterval(timer);
  }, []);

  return (
    <div
      className="panel"
      role="status"
      aria-live="polite"
      style={{ borderColor: "#5a3a1f", display: "flex", alignItems: "center", gap: 12 }}
    >
      <span className="badge warn">刷新失败</span>
      <div className="sub" style={{ flex: 1 }}>
        {loadedAt
          ? `数据截止 ${clockText(loadedAt)}（${agoText(loadedAt, now)}）`
          : "显示的是上一次取到的数据"}
        {failedAt ? ` · 上次刷新失败于 ${clockText(failedAt)}` : ""} · {error}
      </div>
      {onRetry && (
        <button className="ghost tiny" onClick={onRetry}>
          重试
        </button>
      )}
    </div>
  );
}