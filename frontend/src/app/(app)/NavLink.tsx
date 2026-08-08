"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

export default function NavLink({ href, label }: { href: string; label: string }) {
  const pathname = usePathname();
  const active = pathname === href || pathname.startsWith(`${href}/`);

  return (
    <Link
      href={href}
      aria-current={active ? "page" : undefined}
      className={`block rounded-md px-3 py-2 text-sm transition ${
        active
          ? "bg-surface-raised font-medium text-text"
          : "text-text-muted hover:bg-surface-raised/60 hover:text-text"
      }`}
    >
      {label}
    </Link>
  );
}
