"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV = [
  { href: "/", label: "概览" },
  { href: "/playground", label: "工作台" },
  { href: "/usage", label: "调用明细" },
  { href: "/keys", label: "Key 管理" },
];

export function NavLinks() {
  const pathname = usePathname();
  return (
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
  );
}