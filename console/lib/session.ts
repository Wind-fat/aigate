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
 * 上次"取不到 me"的原因。
 *
 * `me === null` 有两种含义：还在加载（ready=false），或者**失败了**（ready=true）。
 * 以前两者都渲染"跳转中…"，于是一个 401 的会话会永远停在那一行字上 —— 没有错误、
 * 没有重试、没有登录入口。把原因留下来，ShellGate 才能说清"发生了什么"。
 */
let lastFailure: string | null = null;

/** 供 ShellGate 读：null = 不知道（未加载或成功），字符串 = 失败原因。 */
export function sessionFailure(): string | null {
  return lastFailure;
}

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
      .then(async (r) => {
        if (!r.ok) {
          // 401 = 会话过期：直接回登录页（同 lib/api.ts 的处理口径）。
          // 其它失败（502/网关没起）留在这里，由 ShellGate 给"重试"而不是无限等待。
          lastFailure =
            r.status === 401
              ? "登录已过期"
              : r.status === 502
                ? "网关不可达（控制台连不上网关进程）"
                : `读取会话失败（HTTP ${r.status}）`;
          if (r.status === 401 && typeof window !== "undefined") {
            const { pathname, search } = window.location;
            if (!pathname.startsWith("/login")) {
              window.location.replace(`/login?next=${encodeURIComponent(`${pathname}${search}`)}`);
            }
          }
          return null;
        }
        lastFailure = null;
        return (await r.json()) as Me;
      })
      .catch(() => {
        lastFailure = "网关不可达（网络请求失败）";
        return null;
      })
      .then((d: Me | null) => {
        cached = d;
        return d;
      })
      .finally(() => {
        inflight = null;
      });
  }
  return inflight;
}

export function useSession() {
  const [me, setMe] = useState<Me | null>(cached);
  const [ready, setReady] = useState(cached !== null);
  const [failure, setFailure] = useState<string | null>(lastFailure);

  useEffect(() => {
    let alive = true;
    load().then((d) => {
      if (!alive) return;
      setMe(d);
      setFailure(d ? null : sessionFailure());
      setReady(true);
    });
    return () => {
      alive = false;
    };
  }, []);

  /** 会话读取失败后的手动重试：丢掉模块缓存再问一次。 */
  const retry = () => {
    clearSession();
    setReady(false);
    load().then((d) => {
      setMe(d);
      setFailure(d ? null : sessionFailure());
      setReady(true);
    });
  };

  const can = (capability: string | string[]) => {
    if (!me) return false;
    const list = Array.isArray(capability) ? capability : [capability];
    // any-of: "usage:own" or "usage:all" both unlock the usage page
    return list.some((c) => me.capabilities.includes(c));
  };

  return {
    me,
    ready,
    failure,
    retry,
    role: me?.role ?? null,
    can,
    canWrite: ready && me !== null && me.role !== "viewer" && me.role !== "finance",
  };
}

/** Where a role lands right after login — a developer does not want a cost dashboard. */
export function homeFor(role: string | null): string {
  return role === "viewer" ? "/playground" : "/";
}