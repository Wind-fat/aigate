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
 *
 * The token fallback is opt-in (`CONSOLE_ALLOW_TOKEN_FALLBACK=1`). Left always-on it is a
 * hole big enough to drive a truck through: anyone who can reach port 3000 without a
 * cookie gets the whole control plane, because this proxy would happily attach the token
 * for them. Local dev and the smoke script set the flag; production does not.
 */

const BASE = process.env.AIGATE_API_BASE ?? "http://127.0.0.1:8080";
const TOKEN = process.env.AIGATE_ADMIN_TOKEN ?? "";
const ALLOW_TOKEN_FALLBACK = process.env.CONSOLE_ALLOW_TOKEN_FALLBACK === "1";

async function forward(req: Request, path: string[]): Promise<Response> {
  const url = new URL(req.url);
  const target = `${BASE}/admin/v1/${path.join("/")}${url.search}`;

  const headers: Record<string, string> = {};
  const contentType = req.headers.get("content-type");
  if (contentType) headers["content-type"] = contentType;

  const cookie = req.headers.get("cookie");
  if (cookie) {
    headers["cookie"] = cookie;
  } else if (TOKEN && ALLOW_TOKEN_FALLBACK) {
    headers["X-Admin-Token"] = TOKEN;
  } else {
    // No session and no permission to borrow the machine token: fail here rather than
    // letting the request reach the gateway with no credentials and a confusing 401.
    return Response.json(
      {
        error: {
          code: "console.login_required",
          message: "no session cookie; sign in (or enable CONSOLE_ALLOW_TOKEN_FALLBACK for scripts)",
        },
      },
      { status: 401 },
    );
  }

  const init: RequestInit = { method: req.method, headers, cache: "no-store" };
  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = await req.text();
  }

  try {
    const upstream = await fetch(target, init);
    // Download headers matter: without content-disposition the browser names the file
    // after the URL, and without x-export-rows the UI cannot say how many rows it got.
    const pass = ["content-type", "content-disposition", "x-export-rows"];
    const headers = new Headers({ "cache-control": "no-store" });
    for (const name of pass) {
      const value = upstream.headers.get(name);
      if (value) headers.set(name, value);
    }
    if (!headers.has("content-type")) headers.set("content-type", "application/json");
    return new Response(upstream.body, { status: upstream.status, headers });
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
export async function PUT(req: Request, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  return forward(req, path);
}

export async function DELETE(req: Request, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  return forward(req, path);
}
