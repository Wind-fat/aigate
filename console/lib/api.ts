const API_BASE = process.env.NEXT_PUBLIC_AIGATE_API ?? "http://127.0.0.1:8080";
const ADMIN_TOKEN = process.env.NEXT_PUBLIC_AIGATE_ADMIN_TOKEN ?? "dev-admin-token";

export { API_BASE };

function adminHeaders(): HeadersInit {
  return { "X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json" };
}

async function unwrap(res: Response) {
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${text.slice(0, 300)}`);
  }
  return res.json();
}

export function adminGet<T>(path: string): Promise<T> {
  return fetch(`${API_BASE}/admin/v1${path}`, {
    headers: adminHeaders(),
    cache: "no-store",
  }).then(unwrap) as Promise<T>;
}

export function adminPost<T>(path: string): Promise<T> {
  return fetch(`${API_BASE}/admin/v1${path}`, {
    method: "POST",
    headers: adminHeaders(),
    cache: "no-store",
  }).then(unwrap) as Promise<T>;
}

export type ChatBody = {
  model: string;
  messages: { role: string; content: string }[];
  temperature?: number;
  stream?: boolean;
};

export function chatFetch(body: ChatBody, apiKey: string): Promise<Response> {
  return fetch(`${API_BASE}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify(body),
  });
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