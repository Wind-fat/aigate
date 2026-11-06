import type { NextConfig } from "next";

/**
 * `standalone` produces a self-contained server bundle (`.next/standalone`), which is
 * what the console image runs. Without it the image has to ship node_modules (~400MB)
 * and the entrypoint becomes "please don't break the workspace layout".
 */
const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  // The gateway lives on another host inside compose; its URL is read at request time
  // from AIGATE_API_BASE, so nothing about it is baked into the bundle.
};

export default nextConfig;