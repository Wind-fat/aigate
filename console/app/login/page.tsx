"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

type Me = { name: string; role: string; kind: string };

/**
 * Login. Deliberately plain: this is an internal tool, and a password field plus a
 * button is the whole feature. Errors are shown verbatim from the gateway so a wrong
 * password and a disabled account are distinguishable to the person, not to an attacker
 * (the gateway already collapses those two into one message).
 */
function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  // Where the middleware wanted to go before it bounced us here. Only same-site paths are
  // honoured: an open redirect on the login page is how phishing links get a real domain.
  const wanted = params.get("next");
  const next = wanted && wanted.startsWith("/") && !wanted.startsWith("//") ? wanted : null;
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
      const who = await fetch("/api/console/me", { cache: "no-store" })
        .then((r) => (r.ok ? r.json() : null))
        .catch(() => null);
      // A developer lands on the playground, not on a cost dashboard they cannot read —
      // unless they were sent here from a deeper page, in which case take them back there.
      const fallback = who?.role === "viewer" ? "/playground" : "/";
      router.replace(next ?? fallback);
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

/**
 * `useSearchParams` opts the route into client-side rendering at request time, which Next
 * requires to sit behind a Suspense boundary. Without it `next build` fails, not `next dev`.
 */
export default function LoginPage() {
  return (
    <Suspense fallback={<div className="login-wrap" />}>
      <LoginForm />
    </Suspense>
  );
}
