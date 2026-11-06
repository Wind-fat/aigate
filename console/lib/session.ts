"use client";

import { useEffect, useState } from "react";

/**
 * Who am I, and what may I reach?
 *
 * The capability list comes from the server (`/console/v1/me`), which derives it from the
 * role. The console never re-implements the rules: it asks "may I?" instead of "is my role
 * admin?". That is why adding a role later does not mean touching five pages.
 *
 * This is a UX layer only. Every one of these capabilities is also enforced in the API —
 * a hidden button is not a permission.
 */

type Me = {
  name: string;
  role: string;
  kind: string;
  project_scope: string[];
  capabilities: string[];
};

let cached: Me | null = null;
let inflight: Promise<Me | null> | null = null;

/**
 * 模块级缓存必须在"换人"时清掉。
 *
 * 之前不清：同一浏览器里先用 owner 登录、再用开发者登录，SPA 不会重新加载模块，
 * `cached` 还是上一个身份 —— 于是侧栏写着 owner、按钮还是 owner 的按钮，
 * 而服务端其实已经把你当开发者了。这是最容易被当成"权限坏了"的一类 bug。
 */
export function clearSession(): void {
  cached = null;
  inflight = null;
}

function load(): Promise<Me | null> {
  if (cached) return Promise.resolve(cached);
  if (!inflight) {
    inflight = fetch("/api/console/me", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d: Me | null) => {
        cached = d;
        return d;
      })
      .catch(() => null)
      .finally(() => {
        inflight = null;
      });
  }
  return inflight;
}

export function useSession() {
  const [me, setMe] = useState<Me | null>(cached);
  const [ready, setReady] = useState(cached !== null);

  useEffect(() => {
    let alive = true;
    load().then((d) => {
      if (!alive) return;
      setMe(d);
      setReady(true);
    });
    return () => {
      alive = false;
    };
  }, []);

  const can = (capability: string | string[]) => {
    if (!me) return false;
    const list = Array.isArray(capability) ? capability : [capability];
    // any-of: "usage:own" or "usage:all" both unlock the usage page
    return list.some((c) => me.capabilities.includes(c));
  };

  return {
    me,
    ready,
    role: me?.role ?? null,
    can,
    canWrite: ready && me !== null && me.role !== "viewer" && me.role !== "finance",
  };
}

/** Where a role lands right after login — a developer does not want a cost dashboard. */
export function homeFor(role: string | null): string {
  return role === "viewer" ? "/playground" : "/";
}