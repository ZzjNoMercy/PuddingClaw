"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { BookOpenCheck, Files, LayoutDashboard, ListChecks, Sparkles } from "lucide-react";

const items = [
  { href: "/knowledge", label: "概览", icon: LayoutDashboard, exact: true },
  { href: "/knowledge/library", label: "资料库", icon: Files },
  { href: "/knowledge/read-later", label: "稍后读", icon: BookOpenCheck },
  { href: "/knowledge/schema", label: "LLM Wiki", icon: Sparkles },
  { href: "/knowledge/imports", label: "任务中心", icon: ListChecks },
];

export default function KnowledgeWorkspaceNav({ className = "" }: { className?: string }) {
  const pathname = usePathname();

  return (
    <nav
      aria-label="知识库工作区"
      className={`flex min-w-0 items-center gap-1 overflow-x-auto rounded-2xl border border-black/[0.06] bg-white p-1.5 shadow-sm ${className}`}
    >
      {items.map((item) => {
        const active = item.exact ? pathname === item.href : pathname.startsWith(item.href);
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={`inline-flex h-9 shrink-0 items-center gap-2 rounded-xl px-3.5 text-xs font-semibold transition ${
              active
                ? "bg-[#002fa7] text-white shadow-sm shadow-[#002fa7]/20"
                : "text-gray-500 hover:bg-[#002fa7]/[0.05] hover:text-[#002fa7]"
            }`}
          >
            <Icon className="h-3.5 w-3.5" />
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
