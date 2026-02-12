import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Static export for S3 + CloudFront deployment:  NEXT_OUTPUT=export pnpm build
  // When set, generates static files in `out/`. Security headers are handled by CloudFront.
  // When unset (local dev), `pnpm dev` works normally with the headers() config below.
  output: process.env.NEXT_OUTPUT === "export" ? "export" : undefined,
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          {
            key: "Strict-Transport-Security",
            value: "max-age=63072000; includeSubDomains; preload",
          },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=()",
          },
          {
            key: "Content-Security-Policy",
            value: [
              "default-src 'self'",
              "script-src 'self' 'unsafe-inline'",
              "style-src 'self' 'unsafe-inline'",
              `connect-src 'self' ${process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000"} ${process.env.NEXT_PUBLIC_WEBSOCKET_URL || "ws://localhost:8000"}`,
              "img-src 'self' data: blob:",
              "font-src 'self'",
              "frame-ancestors 'none'",
              "base-uri 'self'",
              "form-action 'self'",
            ].join("; "),
          },
        ],
      },
    ];
  },
};

export default nextConfig;
