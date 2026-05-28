import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // 静的エクスポート不要 — サーバーとして起動
  output: "standalone",
};

export default nextConfig;
