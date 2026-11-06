"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useSession } from "@/lib/session";

/**
 * 404。控制台里最常见的"找不到"不是打错字，而是**权限**：
 * 开发者手动输了一个自己看不到的页面。所以这里不说"页面不存在"，
 * 而是给出角色、可能的入口和回退路径。
 */
export default function ShellNotFound() {
  const { me } = useSession();
  const [path, setPath] = useState("");

  useEffect(() => {
    setPath(window.location.pathname);
  }, []);

  return (
    <div className="panel">
      <h2>没有这个页面</h2>
      <div className="sub" style={{ marginBottom: 12 }}>
        路径：<span className="mono">{path || "—"}</span>
        {me?.role && (
          <>
            {" "}
            · 当前角色：<span className="badge">{me.role}</span>
          </>
        )}
      </div>
      <div className="sub" style={{ marginBottom: 12, lineHeight: 1.9 }}>
        两种可能：地址打错了，或者这个页面不在你的角色范围内。
        用 <span className="mono">⌘K</span> 可以只列出你有权访问的页面。
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <Link
          href={me?.role === "viewer" ? "/playground" : "/"}
          style={{ padding: "6px 12px", border: "1px solid var(--border)", borderRadius: 6 }}
        >
          回到{me?.role === "viewer" ? "工作台" : "概览"}
        </Link>
        <Link
          href="/keys"
          className="ghost"
          style={{ padding: "6px 12px", border: "1px solid var(--border)", borderRadius: 6 }}
        >
          Key 管理
        </Link>
      </div>
    </div>
  );
}