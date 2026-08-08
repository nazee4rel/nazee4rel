import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // The browser never calls FastAPI directly. Every request goes through this
  // app's own /api routes, which run server-side, so the session cookie stays
  // same-origin and no backend URL, X token or Anthropic key is ever present in
  // a client bundle.
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        ],
      },
    ];
  },
};

export default config;
