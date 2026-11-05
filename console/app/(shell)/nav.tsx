"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

const NAV = [
  { href: "/", label: "概览" },
  { href: "/playground", label: "工作台" },
  { href: "/usage", label: "调用明细" },
  { href: "/keys", label: "Key 管理" },
  { href: "/members", label: "成员" },
  { href: "/audit", label: "审计" },
];

type Me = { name: string; role: string; kind: string };

export function NavLinks() {
  const pathname = usePathname();
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);

  useEffect(() => {
    fetch("/api/console/me", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => d && setMe(d))
      .catch(() => undefined);
  }, []);

  async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    router.replace("/login");
    router.refresh();
  }

  return (
    <>
      <nav>
        {NAV.map((item) => {
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
            <div className="nav-user-name" title={me.name}>
              {me.name}
            </div>
            <div className="nav-user-role">
              <span className="badge">{me.role}</span>
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