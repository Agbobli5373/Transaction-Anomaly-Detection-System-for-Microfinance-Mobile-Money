import type { NextConfig } from "next";

// The Content-Security-Policy (with its per-request nonce) and the other security headers are set
// in src/proxy.ts, which runs on every page request.
const config: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
};

export default config;
