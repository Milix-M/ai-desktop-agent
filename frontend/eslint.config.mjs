import { FlatCompat } from "@eslint/eslintrc";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const compat = new FlatCompat({ baseDirectory: dirname(fileURLToPath(import.meta.url)) });
const config = [
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts", "coverage/**"] },
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  // noVNC has no bundled TypeScript declarations; keep the existing JS boundary explicit.
  {
    files: ["src/components/VncViewer.tsx", "src/lib/novnc.d.ts"],
    rules: { "@typescript-eslint/no-explicit-any": "off" },
  },
];

export default config;
