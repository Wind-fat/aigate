/**
 * Login proxy.
 *
 * The gateway sets an HttpOnly session cookie; this route passes it through to the
 * browser unchanged. The browser never sees the signing secret or the admin token.
 */

const BASE = process.env.AIGATE_API_BASE ?? "http://127.0.0.1:8080";

export async function POST(req: Request): Promise<Response> {
  const body = await req.text();

  try {
    const upstream = await fetch(`${BASE}/console/v1/login`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body,
    });

    const payload = await upstream.text();
    const headers = new Headers({ "content-type": "application/json", "cache-control": "no-store" });

    // pass Set-Cookie through (there can be more than one)
    const setCookie = upstream.headers.getSetCookie?.() ?? [];
    for (const cookie of setCookie) headers.append("set-cookie", cookie);

    return new Response(payload, { status: upstream.status, headers });
  } catch {
    return Response.json(
      { error: { code: "console.upstream_unreachable", message: `gateway not reachable at ${BASE}` } },
      { status: 502 },
    );
  }
}