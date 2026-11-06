/**
 * Client-side API helpers.
 *
 * Everything goes through the console's own `/api/*` routes: the admin token lives in
 * the server process, never in this bundle. Same-origin also means no CORS setup.
 */

async function unwrap(res: Response) {
  if (!res.ok) {
    const text = await res.text();
    let message = `${res.status}`;
    try {
      const err = JSON.parse(text)?.error ?? {};
      // 只抛 code（`gateway.key_name_taken`）等于让用户去猜；把 message 一起带上，
      // toast 里才是一句能照做的话。
      message = err.message ? `${err.message}（${err.code ?? res.status}）` : (err.code ?? message);
    } catch {
      /* not json */
    }
    throw new Error(message);
  }
  return res.json();
}

export function adminGet<T>(path: string): Promise<T> {
  return fetch(`/api/admin${path}`, { cache: "no-store" }).then(unwrap) as Promise<T>;
}

export function adminPost<T>(path: string, body?: unknown): Promise<T> {
  return fetch(`/api/admin${path}`, {
    method: "POST",
    cache: "no-store",
    ...(body === undefined
      ? {}
      : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  }).then(unwrap) as Promise<T>;
}

export function adminPut<T>(path: string, body: unknown): Promise<T> {
  return fetch(`/api/admin${path}`, {
    method: "PUT",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then(unwrap) as Promise<T>;
}

/** Error text a human can act on: the gateway's code plus its message when present. */
export async function describeError(res: Response): Promise<string> {
  const text = await res.text().catch(() => "");
  try {
    const body = JSON.parse(text);
    const err = body?.error ?? {};
    return err.message || err.code || `${res.status}`;
  } catch {
    return text.slice(0, 120) || `${res.status}`;
  }
}

export const money = (v: number | null | undefined) =>
  `¥${(v ?? 0).toFixed(6).replace(/0+$/, "").replace(/\.$/, ".0")}`;

export const pct = (v: number | null | undefined) => `${(v ?? 0).toFixed(1)}%`;

export const ms = (v: number | null | undefined) => `${Math.round(v ?? 0)} ms`;

export const shortTime = (iso: string | null | undefined) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(
    d.getHours(),
  ).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};