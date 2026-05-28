import type { Metadata } from "next";
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
      <body>{children}</body>
    </html>
  );
}
