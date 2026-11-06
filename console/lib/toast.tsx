"use client";

import { useEffect, useState } from "react";

/**
 * Toast host. Deliberately ~40 lines and no dependency: the console needs one channel
 * for "saved / failed", and a library for that would be the only reason to add a
 * provider tree to every page.
 */

type Toast = { id: number; msg: string; kind: "ok" | "err" };
type Listener = (t: Toast) => void;

let listeners: Listener[] = [];
let nextId = 1;

export function toast(msg: string, kind: "ok" | "err" = "ok") {
  const t = { id: nextId++, msg, kind };
  listeners.forEach((fn) => fn(t));
}

export function ToastHost() {
  const [items, setItems] = useState<Toast[]>([]);

  useEffect(() => {
    const fn: Listener = (t) => {
      setItems((prev) => [...prev, t]);
      window.setTimeout(() => setItems((prev) => prev.filter((x) => x.id !== t.id)), 3800);
    };
    listeners = [...listeners, fn];
    return () => {
      listeners = listeners.filter((x) => x !== fn);
    };
  }, []);

  if (items.length === 0) return null;
  return (
    <div className="toasts">
      {items.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`} role="status">
          {t.msg}
        </div>
      ))}
    </div>
  );
}