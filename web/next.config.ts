import path from "node:path";

import type { NextConfig } from "next";

const apiInternalUrl = (process.env.API_INTERNAL_URL || "http://localhost:8000").replace(
  /\/+$/,
  "",
);

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
  // Pin the workspace root so a lockfile in a parent directory is never
  // mistaken for this project's root.
  turbopack: {
    root: path.resolve("."),
  },
  async rewrites() {
    return [
      {
        source: "/api/v1/:path*",
        destination: `${apiInternalUrl}/api/v1/:path*`,
      },
    ];
  },
};

export default nextConfig;
