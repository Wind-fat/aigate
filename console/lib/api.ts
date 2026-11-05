/**
 * Client-side API helpers.
 *
 * Everything goes through the console's own `/api/*` routes: the admin token lives in
 * the server process, never in this bundle. Same-origin also means no CORS setup.
 */

async function unwrap(res: Response) {
  if (!res.ok) {
    const text = await res.text();
    let code = `${res.status}`;
    try {
      code = JSON.parse(text)?.error?.code ?? code;
    } catch {
      /* not json */
    }
    throw new Error(code);
  }
  return res.json();
}

export function adminGet<T>(path: string): Promise<T> {
  return fetch(`/api/admin${path}`, { cache: "no-store" }).then(unwrap) as Promise<T>;
}

export function adminPost<T>(path: string): Promise<T> {
  return fetch(`/api/admin${path}`, { method: "POST", cache: "no-store" }).then(unwrap) as Promise<T>;
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