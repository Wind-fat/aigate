/**
 * Server-side proxy for the control plane.
 *
 * Two credentials can arrive here:
 *   - a browser session cookie (normal case) — forwarded as-is, so the gateway can name
 *     the human in the audit log;
 *   - the machine token, used only when there is no cookie (scripts, curl against the
 *     console, first-run smoke tests).
 *
 * Either way the token never reaches the browser bundle.
 */

const BASE = process.env.AIGATE_API_BASE ?? "http://127.0.0.1:8080";
const TOKEN = process.env.AIGATE_ADMIN_TOKEN ?? "";

async function forward(req: Request, path: string[]): Promise<Response> {
  const url = new URL(req.url);
  const target = `${BASE}/admin/v1/${path.join("/")}${url.search}`;

  const headers: Record<string, string> = {};
  const contentType = req.headers.get("content-type");
  if (contentType) headers["content-type"] = contentType;

  const cookie = req.headers.get("cookie");
  if (cookie) {
    headers["cookie"] = cookie;
  } else if (TOKEN) {
    headers["X-Admin-Token"] = TOKEN;
  }

  const init: RequestInit = { method: req.method, headers, cache: "no-store" };
  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = await req.text();
  }

  try {
    const upstream = await fetch(target, init);
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "content-type": upstream.headers.get("content-type") ?? "application/json",
        "cache-control": "no-store",
      },
    });
  } catch {
    return Response.json(
      { error: { code: "console.upstream_unreachable", message: `gateway not reachable at ${BASE}` } },
      { status: 502 },
    );
  }
}

export async function GET(req: Request, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  return forward(req, path);
}

export async function POST(req: Request, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  return forward(req, path);
}