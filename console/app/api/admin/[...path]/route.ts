/**
 * Server-side proxy for the control plane.
 *
 * Why this exists: the console used to call the gateway straight from the browser with
 * the admin token inlined at build time (`NEXT_PUBLIC_*`), which meant anyone who opened
 * the page had full admin rights. Now the token lives only in this server process and
 * never reaches the browser bundle.
 */

const BASE = process.env.AIGATE_API_BASE ?? "http://127.0.0.1:8080";
const TOKEN = process.env.AIGATE_ADMIN_TOKEN ?? "dev-admin-token";

async function forward(req: Request, path: string[]): Promise<Response> {
  const url = new URL(req.url);
  const target = `${BASE}/admin/v1/${path.join("/")}${url.search}`;

  const headers: Record<string, string> = { "X-Admin-Token": TOKEN };
  const contentType = req.headers.get("content-type");
  if (contentType) headers["content-type"] = contentType;

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