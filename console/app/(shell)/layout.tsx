import type { ReactNode } from "react";
import { CommandPalette } from "@/components/command-palette";
import { ShellGate } from "@/components/shell-gate";
import { ToastHost } from "@/lib/toast";
import { NavLinks } from "./nav";

export default function ShellLayout({ children }: { children: ReactNode }) {
  return (
    <>
      <aside className="sidebar">
        <div className="brand">
          AIGate
          <span>console</span>
        </div>
        <NavLinks />
        <div className="sidebar-foot">
          v0.3.0 · team
          <br />
          admin/control-plane
        </div>
      </aside>
      <main className="main">
        <ShellGate>{children}</ShellGate>
      </main>
      <ToastHost />
      <CommandPalette />
    </>
  );
}
