"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

export default function NavLink({
  href,
  label,
  badge,
}: {
  href: string;
  label: string;
  /** Count shown alongside the label — used for actions awaiting approval. */
  badge?: number;
}) {
  const pathname = usePathname();
  const active = pathname === href || pathname.startsWith(`${href}/`);

  return (
    <Link
      href={href}
      aria-current={active ? "page" : undefined}
      className={`flex items-center justify-between gap-2 rounded-md px-3 py-2 text-sm transition ${
        active
          ? "bg-surface-raised font-medium text-text"
          : "text-text-muted hover:bg-surface-raised/60 hover:text-text"
      }`}
    >
      <span>{label}</span>
      {badge !== undefined && badge > 0 && (
        <span
          className="numeric rounded-full bg-accent px-1.5 py-0.5 text-[10px] font-medium text-canvas"
          aria-label={`${badge} awaiting your approval`}
        >
          {badge}
        </span>
      )}
    </Link>
  );
}
