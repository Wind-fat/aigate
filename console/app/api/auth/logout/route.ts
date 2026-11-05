/** Logout: ask the gateway to expire the cookie, then clear it in the browser too. */

const BASE = process.env.AIGATE_API_BASE ?? "http://127.0.0.1:8080";

export async function POST(req: Request): Promise<Response> {
  const cookie = req.headers.get("cookie") ?? "";

  try {
    const upstream = await fetch(`${BASE}/console/v1/logout`, {
      method: "POST",
      headers: { cookie },
    });
    const payload = await upstream.text();
    const headers = new Headers({ "content-type": "application/json", "cache-control": "no-store" });
    for (const c of upstream.headers.getSetCookie?.() ?? []) headers.append("set-cookie", c);
    headers.append("set-cookie", "aigate_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax");
    return new Response(payload, { status: upstream.status, headers });
  } catch {
    // even if the gateway is down, drop the cookie locally
    return Response.json(
      { ok: true },
      { headers: { "set-cookie": "aigate_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" } },
    );
  }
}