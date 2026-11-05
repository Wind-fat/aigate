/**
 * Chat proxy for the playground.
 *
 * Forwards the caller's Bearer key to the gateway and passes the body straight through,
 * including SSE. Keeping it same-origin means the playground no longer needs CORS or a
 * gateway URL baked into the bundle.
 */

const BASE = process.env.AIGATE_API_BASE ?? "http://127.0.0.1:8080";

export async function POST(req: Request): Promise<Response> {
  const body = await req.text();
  const authorization = req.headers.get("authorization") ?? "";
  if (!authorization) {
    return Response.json(
      { error: { code: "console.missing_key", message: "Authorization: Bearer sk-ag-... required" } },
      { status: 401 },
    );
  }

  try {
    const upstream = await fetch(`${BASE}/v1/chat/completions`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization },
      body,
    });

    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "content-type": upstream.headers.get("content-type") ?? "application/json",
        "cache-control": "no-cache",
        // keep SSE unbuffered through the proxy hop
        "x-accel-buffering": "no",
      },
    });
  } catch {
    return Response.json(
      { error: { code: "console.upstream_unreachable", message: `gateway not reachable at ${BASE}` } },
      { status: 502 },
    );
  }
}