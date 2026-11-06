"use client";

import { usePathname } from "next/navigation";
import { useSession } from "@/lib/session";

/**
 * One gate for the whole shell.
 *
 * The previous approach was per-page: every page had to remember to check, and the two
 * pages that forgot shipped owner-only controls to a read-only account. Route → capability
 * in one table means a new page is covered by editing this file, not by remembering.
 *
 * This is still a UX layer: the same capability is enforced by the API. It exists so a
 * person who mistypes a URL gets an explanation instead of a page of failed requests.
 */
const NEED: { prefix: string; need: string | string[]; why?: string }[] = [
  { prefix: "/playground", need: "dev" },
  { prefix: "/usage", need: ["usage:all", "usage:own"] },
  { prefix: "/traces", need: ["trace:all", "trace:own"] },
  { prefix: "/keys", need: ["keys:read", "keys:own"] },
  { prefix: "/routes", need: "routes:read", why: "路由是平台决策，改错会影响所有人的调用" },
  { prefix: "/providers", need: "providers:read", why: "供应商配置包含各家 base_url 与密钥变量名" },
  { prefix: "/projects", need: "projects:read", why: "预算是公司级财务信息" },
  { prefix: "/members", need: "members:read", why: "成员列表包含全公司的账号" },
  { prefix: "/audit", need: "audit:read", why: "审计包含所有人的操作记录" },
];

function needFor(pathname: string) {
  return NEED.find((item) => pathname.startsWith(item.prefix));
}

export function ShellGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { ready, can, role } = useSession();

  if (!ready) return <div className="empty">加载中…</div>;

  // "/" is the cost dashboard; a developer gets redirected by the nav component instead
  // of seeing this panel, so no special case is needed here beyond the check below.
  if (pathname === "/" && !can("costs")) return <div className="empty">跳转中…</div>;

  const rule = needFor(pathname);
  if (rule && !can(rule.need)) {
    return (
      <div className="panel">
        <h2>这个页面不属于你的角色</h2>
        <div className="sub" style={{ marginBottom: 10 }}>
          当前角色：<span className="badge">{role ?? "unknown"}</span>
        </div>
        <div className="sub">
          {rule.why ?? `需要权限：${Array.isArray(rule.need) ? rule.need.join(" 或 ") : rule.need}`}。
          需要开通找 owner。
        </div>
      </div>
    );
  }

  return <>{children}</>;
}