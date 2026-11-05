import type { ReactNode } from "react";
import "./globals.css";

export const metadata = {
  title: "AIGate Console",
  description: "Multi-tenant LLM gateway control plane",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
