import type { Metadata } from "next";
import { installBootstrapRecovery } from "@/lib/bootstrap-recovery";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Desktop Agent",
  description: "AIがVMデスクトップを操作するリモートコントロールアプリ",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="ja">
      <head><script id="bootstrap-recovery" dangerouslySetInnerHTML={{ __html: `(${installBootstrapRecovery.toString()})(window, () => window.location.reload());` }} /></head>
      <body>{children}</body>
    </html>
  );
}
