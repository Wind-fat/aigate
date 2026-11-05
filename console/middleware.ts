import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

/**
 * Route guard.
 *
 * Presence of the cookie is enough here — the gateway verifies the signature on every
 * call, so a forged cookie gets you a login page's worth of nothing. Keeping the check
 * cheap means no signing secret in the Next.js edge runtime.
 */

const PUBLIC_PATHS = ["/login"];

export function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl;

  if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) {
    return NextResponse.next();
  }

  if (!req.cookies.get("aigate_session")) {
    const url = req.nextUrl.clone();
    url.pathname = "/login";
    url.searchParams.set("next", pathname);
    return NextResponse.redirect(url);
  }

  return NextResponse.next();
}

export const config = {
  // skip static assets, the API proxies (they check credentials themselves) and icons
  matcher: ["/((?!api|_next/static|_next/image|favicon.ico).*)"],
};