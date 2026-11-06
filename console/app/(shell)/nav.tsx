"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { clearSession, useSession } from "@/lib/session";

/**
 * Navigation is derived from capabilities, not from a hardcoded role list. A developer
 * sees three entries (工作台 / 我的调用 / 模型), finance sees three (概览 / 明细 / 预算),
 * owner sees all eleven. Nobody sees a link that would answer 403.
 */
const NAV: { href: string; label: string; need: string | string[] }[] = [
  { href: "/", label: "概览", need: "costs" },
  { href: "/playground", label: "工作台", need: "dev" },
  { href: "/usage", label: "调用明细", need: ["usage:all", "usage:own"] },
  { href: "/keys", label: "Key 管理", need: ["keys:read", "keys:own"] },
  { href: "/routes", label: "模型路由", need: "routes:read" },
  { href: "/providers", label: "供应商", need: "providers:read" },
  { href: "/models", label: "模型对比", need: "models:read" },
  { href: "/prompts", label: "Prompt 版本", need: "routes:read" },
  { href: "/projects", label: "预算配额", need: "projects:read" },
  { href: "/members", label: "成员", need: "members:read" },
  { href: "/audit", label: "审计", need: "audit:read" },
];

const ROLE_LABEL: Record<string, string> = {
  owner: "owner",
  admin: "管理员",
  finance: "财务",
  viewer: "开发者",
};

export function NavLinks() {
  const pathname = usePathname();
  const router = useRouter();
  const { me, role, can, ready } = useSession();

  async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    clearSession(); // 否则下一个登录的人会短暂看到上一个身份的侧栏
    router.replace("/login");
    router.refresh();
  }

  // One session-wide redirect: a developer who types /projects should land somewhere
  // useful instead of reading a 403 page.
  useEffect(() => {
    if (!ready || !role) return;
    if (pathname === "/" && !can("costs")) {
      router.replace("/playground");
    }
  }, [ready, role, pathname, can, router]);

  return (
    <>
      <nav>
        {NAV.filter((item) => !ready || can(item.need)).map((item) => {
          const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
          return (
            <Link key={item.href} href={item.href} className={`navlink${active ? " active" : ""}`}>
              {item.label}
            </Link>
          );
        })}
      </nav>
      <div className="nav-user">
        {me ? (
          <>
            <div className="nav-user-name" title={`${me.name} · ${role}`}>
              {me.name}
            </div>
            <div className="nav-user-role">
              <span className={`badge ${role === "owner" ? "blue" : ""}`}>
                {ROLE_LABEL[role ?? ""] ?? role}
              </span>
              <button className="ghost tiny" onClick={logout}>
                退出
              </button>
            </div>
          </>
        ) : (
          <div className="nav-user-name">—</div>
        )}
      </div>
    </>
  );
}