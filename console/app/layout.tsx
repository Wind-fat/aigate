import type { ReactNode } from "react";
import "./globals.css";
import { NavLinks } from "./nav";

export const metadata = {
  title: "AIGate Console",
  description: "Multi-tenant LLM gateway control plane",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <aside className="sidebar">
          <div className="brand">
            AIGate
            <span>console</span>
          </div>
          <NavLinks />
          <div className="sidebar-foot">
            v0.2.0 · dev
            <br />
            admin/control-plane
          </div>
        </aside>
        <main className="main">{children}</main>
      </body>
    </html>
  );
}