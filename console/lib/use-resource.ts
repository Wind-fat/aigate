"use client";

import { type DependencyList, useCallback, useEffect, useRef, useState } from "react";

/**
 * 一个资源的三种状态，外加"上次成功"的时间戳。
 *
 * 为什么不是 `{ data: T, loading: boolean }`：那套写法把"没有数据"和"空数据"混成
 * 同一个 `[]`，于是首屏 0–1 秒永远是"共 0 把 · 生效 0"、"还没有审计记录"——
 * 用户读到的是一句结论，而事实只是"还没回来"。
 *
 * 这里的约定（页面只准照这个写）：
 *   - `data === null`  → 还没有过任何一次成功返回：渲染加载态（或首次失败态）
 *   - `data !== null`  → 有数据：`[]`/空对象是真的空，可以渲染空态
 *   - `error && data`  → 轮询/刷新失败但旧数据还在：保留旧数据，标"数据截止 X"，
 *                        绝不把整页替换成错误面板（P1-4）
 */
export type Resource<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** 最近一次成功的时间戳（ms）；从没成功过是 null。 */
  loadedAt: number | null;
  /** 最近一次失败的时间戳（ms）；从没失败过是 null。 */
  failedAt: number | null;
  reload: () => void;
};

/**
 * @param loader    每次要取数据时调用；可以内联写，hook 用 ref 保存，不会因为它换引用而重跑
 * @param deps      真正决定"要不要重新取"的东西（筛选条件、天数…），语义同 useEffect
 * @param intervalMs 有值时轮询；轮询失败不清空 data
 */
export function useResource<T>(
  loader: () => Promise<T>,
  deps: DependencyList,
  { intervalMs }: { intervalMs?: number } = {},
): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadedAt, setLoadedAt] = useState<number | null>(null);
  const [failedAt, setFailedAt] = useState<number | null>(null);
  const [nonce, setNonce] = useState(0);

  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  // 只认最后一次发出的请求：切筛选/切天数时，慢的旧请求晚到不能覆盖新结果。
  const seq = useRef(0);

  const run = useCallback(() => {
    const mine = ++seq.current;
    setLoading(true);
    loaderRef.current().then(
      (next) => {
        if (mine !== seq.current) return;
        setData(next);
        setLoadedAt(Date.now());
        setError(null);
        setFailedAt(null);
        setLoading(false);
      },
      (err: Error) => {
        if (mine !== seq.current) return;
        // 关键：这里**不动 data**。有旧数据就继续显示旧数据，由页面加"截止时间"横条。
        setError(err.message);
        setFailedAt(Date.now());
        setLoading(false);
      },
    );
  }, []);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    run();
    // deps 由调用方显式给出（同 useEffect 语义）。run 是稳定引用。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, nonce, ...deps]);

  useEffect(() => {
    if (!intervalMs) return;
    const timer = setInterval(run, intervalMs);
    return () => clearInterval(timer);
  }, [run, intervalMs]);

  return { data, error, loading, loadedAt, failedAt, reload };
}

/** "12 秒前" / "3 分钟前"，只用于陈旧数据的提示，不追求日历精度。 */
export function agoText(from: number, now: number): string {
  const s = Math.max(0, Math.round((now - from) / 1000));
  if (s < 60) return `${s} 秒前`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分钟前`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} 小时前`;
  return `${Math.floor(h / 24)} 天前`;
}

export function clockText(at: number): string {
  const d = new Date(at);
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, "0"))
    .join(":");
}