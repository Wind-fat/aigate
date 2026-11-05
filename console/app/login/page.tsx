"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

type Me = { name: string; role: string; kind: string };

/**
 * Login. Deliberately plain: this is an internal tool, and a password field plus a
 * button is the whole feature. Errors are shown verbatim from the gateway so a wrong
 * password and a disabled account are distinguishable to the person, not to an attacker
 * (the gateway already collapses those two into one message).
 */
export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [me, setMe] = useState<Me | null>(null);

  useEffect(() => {
    fetch("/api/admin/overview", { cache: "no-store" })
      .then((r) => (r.ok ? fetch("/api/console/me", { cache: "no-store" }) : null))
      .then((r) => (r && r.ok ? r.json() : null))
      .then((data) => data && setMe(data))
      .catch(() => undefined);
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.error?.message ?? body?.error?.code ?? `${res.status}`);
      }
      router.replace("/");
      router.refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand">
          AIGate<span>console</span>
        </div>
        <p className="sub" style={{ marginTop: 0 }}>
          用团队账号登录
        </p>

        <label>邮箱</label>
        <input
          type="email"
          value={email}
          autoComplete="username"
          onChange={(e) => setEmail(e.target.value)}
          required
        />

        <label style={{ marginTop: 12 }}>密码</label>
        <input
          type="password"
          value={password}
          autoComplete="current-password"
          onChange={(e) => setPassword(e.target.value)}
          required
        />

        {error && (
          <div className="err mono" style={{ marginTop: 12 }}>
            {error}
          </div>
        )}

        <button type="submit" disabled={busy} style={{ marginTop: 16, width: "100%" }}>
          {busy ? "登录中…" : "登录"}
        </button>

        {me && (
          <div className="sub" style={{ marginTop: 14 }}>
            当前已登录：{me.name}
          </div>
        )}

        <div className="sub" style={{ marginTop: 12, fontSize: 12 }}>
          还没有账号？让管理员在控制台「成员」里加你，或用
          <code> uv run python scripts/setup.py --add-user </code>
          创建。改密码在「成员 → 我的密码」。
        </div>
      </form>
    </div>
  );
}