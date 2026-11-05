/** Proxy for `/console/v1/*` (whoami, users, password). Cookie forwarded, nothing else. */

const BASE = process.env.AIGATE_API_BASE ?? "http://127.0.0.1:8080";

async function forward(req: Request, path: string[]): Promise<Response> {
  const url = new URL(req.url);
  const headers: Record<string, string> = {};
  const contentType = req.headers.get("content-type");
  if (contentType) headers["content-type"] = contentType;
  const cookie = req.headers.get("cookie");
  if (cookie) headers["cookie"] = cookie;

  const init: RequestInit = { method: req.method, headers, cache: "no-store" };
  if (req.method !== "GET" && req.method !== "HEAD") init.body = await req.text();

  try {
    const upstream = await fetch(`${BASE}/console/v1/${path.join("/")}${url.search}`, init);
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
  return forward(req, (await ctx.params).path);
}

export async function POST(req: Request, ctx: { params: Promise<{ path: string[] }> }) {
  return forward(req, (await ctx.params).path);
}