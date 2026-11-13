/**
 * Client-side API helpers.
 *
 * Everything goes through the console's own `/api/*` routes: the admin token lives in
 * the server process, never in this bundle. Same-origin also means no CORS setup.
 */

/**
 * 一个带 code 的错误。
 *
 * 以前所有失败都变成 `new Error(message)`：页面拿到一段字符串，只能靠文案猜是
 * "会话过期"还是"网关没起"。`code` 让错误态按原因分支
 * （`gateway.login_required` → 重新登录；`console.upstream_unreachable` → 网关不可达），
 * 而不是给一句"检查 8080 是否在运行"把值班推去错方向。
 */
export class ApiError extends Error {
  readonly code: string | null;
  readonly status: number;

  constructor(message: string, code: string | null, status: number) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

/** 会话过期时把整页送回登录页，并带上"登录后回哪"。 */
function redirectToLogin(): void {
  if (typeof window === "undefined") return;
  const { pathname, search } = window.location;
  // 已经在登录页就别再跳，否则 `next` 会被一路覆盖成 /login。
  if (pathname.startsWith("/login")) return;
  window.location.replace(`/login?next=${encodeURIComponent(`${pathname}${search}`)}`);
}

async function unwrap(res: Response) {
  if (!res.ok) {
    const text = await res.text();
    let message = `${res.status}`;
    let code: string | null = null;
    try {
      const err = JSON.parse(text)?.error ?? {};
      code = typeof err.code === "string" ? err.code : null;
      // 只抛 code（`gateway.key_name_taken`）等于让用户去猜；把 message 一起带上，
      // toast 里才是一句能照做的话。
      message = err.message ? `${err.message}（${err.code ?? res.status}）` : (err.code ?? message);
    } catch {
      /* not json */
    }

    // 401 = 会话过期或被吊销。middleware 只看 cookie 是否存在，所以页面本身能渲染；
    // 但每个请求都 401 时留在原地只会看到一屏错误 —— 唯一正确的动作是重新登录。
    // 全局副作用只对 401 生效，别的错误仍然交给页面自己展示。
    if (res.status === 401) {
      redirectToLogin();
      throw new ApiError("登录已过期，请重新登录", code ?? "gateway.login_required", 401);
    }

    // 代理层连不上网关时，别把内部地址和英文原文扔给值班。
    if (code === "console.upstream_unreachable") {
      throw new ApiError("网关不可达：控制台连不上网关进程（它可能已停止或正在重启）", code, res.status);
    }

    throw new ApiError(message, code, res.status);
  }
  return res.json();
}

export function adminGet<T>(path: string): Promise<T> {
  return fetch(`/api/admin${path}`, { cache: "no-store" }).then(unwrap) as Promise<T>;
}

/**
 * `/api/console/*`（会话 cookie 认证，不走 admin 令牌）。
 *
 * 成员页读的是 `/console/v1/users`：它按"我是谁"决定返回范围，走 admin 代理
 * 反而会绕过这套语义。错误解析与 adminGet 共用同一个 unwrap，页面拿到的仍是 ApiError。
 */
export function consoleGet<T>(path: string): Promise<T> {
  return fetch(`/api/console${path}`, { cache: "no-store" }).then(unwrap) as Promise<T>;
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

export function adminDelete<T>(path: string): Promise<T> {
  return fetch(`/api/admin${path}`, { method: "DELETE", cache: "no-store" }).then(
    unwrap,
  ) as Promise<T>;
}

/** Error text a human can act on: the gateway's code plus its message when present. */
export async function describeError(res: Response): Promise<string> {
  const text = await res.text().catch(() => "");
  try {
    const body = JSON.parse(text);
    const err = body?.error ?? {};
    if (err.message || err.code) return err.message || err.code;

    // FastAPI 的校验错误长这样：{"detail":[{"loc":["body","email"],"msg":"Value error, …"}]}
    // 以前这里只返回 `422`，用户看到的就是一个数字 —— 实测有人因为「邮箱写成 demo@local」
    // 反复以为自己登不进去，却看不到半句原因。校验错误必须把字段名和网关的原话带出来。
    const detail = body?.detail;
    if (Array.isArray(detail) && detail.length) {
      const first = detail[0] as { loc?: unknown[]; msg?: string };
      const field = Array.isArray(first?.loc) ? String(first.loc[first.loc.length - 1]) : "";
      const msg = (first?.msg ?? "").replace(/^Value error,\s*/, "");
      if (msg) return field ? `${field}：${msg}` : msg;
    }
    if (typeof detail === "string" && detail) return detail;
    return `${res.status}`;
  } catch {
    return text.slice(0, 120) || `${res.status}`;
  }
}

/** 未知值的统一渲染。`null`/`undefined` 是"没有这个数"，不是 0。 */
export const UNKNOWN = "—";

/**
 * 金额。null → 「—」。
 *
 * 以前 `(v ?? 0)` 把"没有账单"写成 ¥0：一条只有 trace、没有 usage 的失败请求会显示
 * "成本 ¥0.0"，而它其实根本没被记过账。零和未知必须能分辨。
 */
export const money = (v: number | null | undefined) =>
  v === null || v === undefined
    ? UNKNOWN
    : `¥${v.toFixed(6).replace(/0+$/, "").replace(/\.$/, ".0")}`;

/** 百分比。null → 「—」，不再兜底成 0.0%（更不会兜底成 100%）。 */
export const pct = (v: number | null | undefined) =>
  v === null || v === undefined ? UNKNOWN : `${v.toFixed(1)}%`;

/** 毫秒。null → 「—」，不再兜底成 "0 ms"。 */
export const ms = (v: number | null | undefined) =>
  v === null || v === undefined ? UNKNOWN : `${Math.round(v)} ms`;

export const shortTime = (iso: string | null | undefined) => {
  if (!iso) return UNKNOWN;
  const d = new Date(iso);
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(
    d.getHours(),
  ).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};