"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useSession } from "@/lib/session";

/**
 * 命令面板（⌘K / Ctrl+K）。
 *
 * 为什么值得做：控制台有 12 个页面，运维记不住哪个功能在哪一页——
 * 他记得的是"我要测一下供应商"、"我要签一把 key"。这个面板按意图索引，不按页面索引。
 *
 * 三个细节：
 *   - 只在有权限的项上出现（借用 useSession().can，和后端同一套能力名）
 *   - 上下键 + 回车，鼠标不用离开键盘
 *   - Esc 关闭；点击遮罩也关闭（随手打开的人不会去按 Esc）
 */

type Item = {
  id: string;
  label: string;
  hint?: string;
  href: string;
  need?: string | string[];
};

const ITEMS: Item[] = [
  { id: "overview", label: "概览", hint: "成本 / 成功率 / P95", href: "/", need: "costs" },
  { id: "ops", label: "运行状态", hint: "熔断 / 记账队列 / 数据库", href: "/ops", need: "providers:read" },
  { id: "play", label: "工作台", hint: "发一个请求，A/B 对比", href: "/playground", need: "dev" },
  { id: "usage", label: "调用明细", hint: "逐条账单，可导出 CSV", href: "/usage", need: ["usage:all", "usage:own"] },
  { id: "keys", label: "Key 管理", hint: "签发 / 吊销 / 看过期", href: "/keys", need: ["keys:read", "keys:own"] },
  { id: "guide", label: "接入指南", hint: "curl / SDK / 错误码", href: "/guide", need: "dev" },
  { id: "routes", label: "模型路由", hint: "虚拟模型 → 候选链", href: "/routes", need: "routes:read" },
  { id: "providers", label: "供应商", hint: "录入密钥 / 测试连通", href: "/providers", need: "providers:read" },
  { id: "models", label: "模型对比", hint: "每千次成本 / 相似度", href: "/models", need: "usage:all" },
  { id: "prompts", label: "Prompt 版本", hint: "版本库 / 切换 / 回滚", href: "/prompts", need: "routes:read" },
  { id: "alerts", label: "告警", hint: "未确认 / 已确认 / 确认", href: "/alerts", need: "costs" },
  { id: "projects", label: "预算配额", hint: "日/月预算、团队上限", href: "/projects", need: "projects:read" },
  { id: "members", label: "成员", hint: "角色 / 可见项目 / 重置密码", href: "/members", need: "members:read" },
  { id: "audit", label: "审计", hint: "谁在什么时候改了什么", href: "/audit", need: "audit:read" },
];

export function CommandPalette() {
  const router = useRouter();
  const { can } = useSession();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const available = useMemo(() => ITEMS.filter((i) => !i.need || can(i.need)), [can]);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return available;
    return available.filter((i) =>
      `${i.label} ${i.hint ?? ""} ${i.href}`.toLowerCase().includes(q),
    );
  }, [available, query]);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen((prev) => !prev);
        setQuery("");
        setCursor(0);
      }
      if (event.key === "Escape") setOpen(false);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 30);
  }, [open]);

  if (!open) return null;

  function go(item: Item | undefined) {
    if (!item) return;
    setOpen(false);
    router.push(item.href);
  }

  return (
    <div className="cmdk-overlay" onClick={() => setOpen(false)}>
      <div className="cmdk" onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          value={query}
          placeholder="跳到哪一页？输入「key」「供应商」「预算」…"
          onChange={(e) => {
            setQuery(e.target.value);
            setCursor(0);
          }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setCursor((c) => Math.min(c + 1, matches.length - 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setCursor((c) => Math.max(c - 1, 0));
            } else if (e.key === "Enter") {
              e.preventDefault();
              go(matches[cursor]);
            }
          }}
        />
        <div className="cmdk-list">
          {matches.length === 0 ? (
            <div className="empty">没有匹配的页面</div>
          ) : (
            matches.map((item, index) => (
              <button
                key={item.id}
                className={`cmdk-item ${index === cursor ? "on" : ""}`}
                onMouseEnter={() => setCursor(index)}
                onClick={() => go(item)}
              >
                <span>{item.label}</span>
                <span className="sub">{item.hint}</span>
              </button>
            ))
          )}
        </div>
        <div className="cmdk-foot">
          ↑↓ 选择 · Enter 打开 · Esc 关闭 · ⌘K / Ctrl+K 唤起
        </div>
      </div>
    </div>
  );
}