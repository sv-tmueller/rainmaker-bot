"use client";

import { useState, type ReactNode } from "react";

/**
 * Collapsible wrapper for technical-detail sections. Defaults collapsed so the
 * operator view stays clean; expands on click to reveal the internals.
 */
export function DisclosureToggle({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="text-[10px] uppercase tracking-[0.1em] text-muted transition-colors hover:text-faint"
        aria-expanded={open}
      >
        {label} {open ? "▾" : "▸"}
      </button>
      {open && <div className="mt-3.5">{children}</div>}
    </div>
  );
}
