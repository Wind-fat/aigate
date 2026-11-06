import Link from "next/link";

/**
 * 根级 404 —— 用户手输了一个连路由组都不匹配的地址。
 *
 * 这里拿不到登录态（服务端组件、且在 (shell) 之外），所以不猜角色，
 * 只给两条永远成立的路：回首页、看接入指南。样式沿用控制台的深色语言，
 * 不要让 404 变成"另一个产品的页面"。
 */
export default function RootNotFound() {
  return (
    <main
      style={{
        minHeight: "70vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
      }}
    >
      <div
        style={{
          maxWidth: 520,
          border: "1px solid var(--border)",
          borderRadius: 12,
          padding: "28px 30px",
          background: "var(--card)",
        }}
      >
        <div style={{ fontSize: 12, letterSpacing: 2, color: "var(--muted-foreground)" }}>
          AIGATE CONSOLE
        </div>
        <h1 style={{ margin: "10px 0 8px", fontSize: 22 }}>没有这个页面</h1>
        <p style={{ color: "var(--muted-foreground)", fontSize: 13, lineHeight: 1.8 }}>
          地址可能打错了，或者这个页面不在你的角色范围内。
          登录后用 <code>⌘K</code> / <code>Ctrl+K</code> 可以只列出你有权访问的页面。
        </p>
        <div style={{ display: "flex", gap: 10, marginTop: 16 }}>
          <Link
            href="/"
            style={{
              padding: "7px 14px",
              borderRadius: 8,
              background: "var(--accent)",
              color: "#0b0f14",
              fontSize: 13,
              fontWeight: 600,
            }}
          >
            回控制台
          </Link>
          <Link
            href="/guide"
            style={{
              padding: "7px 14px",
              borderRadius: 8,
              border: "1px solid var(--border)",
              fontSize: 13,
            }}
          >
            接入指南
          </Link>
        </div>
      </div>
    </main>
  );
}