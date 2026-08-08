"use client";

import Link from "next/link";
import {
  Activity,
  ArrowLeft,
  Bot,
  Brain,
  Database,
  FileText,
  FolderOpen,
  Globe2,
  KeyRound,
  Network,
  Sliders,
} from "lucide-react";

export type SettingsCategory =
  | "ai"
  | "project"
  | "databaseQa"
  | "rag"
  | "knowledge"
  | "memory"
  | "harness"
  | "worker"
  | "advanced"
  | "system";

export const SETTINGS_CATEGORIES: Array<{
  key: SettingsCategory;
  label: string;
  icon: React.ElementType;
  color: string;
}> = [
  { key: "ai", label: "模型服务", icon: Network, color: "#002fa7" },
  { key: "project", label: "项目上下文", icon: FileText, color: "#002fa7" },
  { key: "databaseQa", label: "智能问数设置", icon: Database, color: "#002fa7" },
  { key: "rag", label: "RAG 设置", icon: Database, color: "#002fa7" },
  { key: "knowledge", label: "知识库", icon: FolderOpen, color: "#002fa7" },
  { key: "memory", label: "记忆管理", icon: Brain, color: "#002fa7" },
  { key: "harness", label: "Harness 配置", icon: Bot, color: "#002fa7" },
  { key: "worker", label: "Worker 接入", icon: KeyRound, color: "#002fa7" },
  { key: "advanced", label: "高级设置", icon: Sliders, color: "#6b7280" },
  { key: "system", label: "系统状态", icon: Activity, color: "#002fa7" },
];

export default function SettingsNavigation({
  active,
  onSelectCategory,
  onReturnToApp,
}: {
  active: SettingsCategory | "webSearch";
  onSelectCategory?: (category: SettingsCategory) => void;
  onReturnToApp?: () => void;
}) {
  const rowClass = (selected: boolean) =>
    `flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-[12px] transition-all ${
      selected
        ? "bg-white/80 font-medium text-gray-900 shadow-sm"
        : "text-gray-500 hover:bg-white/55 hover:text-gray-800"
    }`;

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto p-3">
      <Link
        href="/"
        onClick={onReturnToApp}
        className="group mb-3 flex items-center gap-2.5 rounded-xl bg-white/55 px-3 py-2.5 text-[13px] font-medium text-gray-700 transition-all hover:bg-white/80"
      >
        <ArrowLeft className="h-4 w-4 text-gray-500 transition-colors group-hover:text-gray-700" />
        返回应用
      </Link>

      <div className="space-y-0.5">
        {SETTINGS_CATEGORIES.map((item, index) => {
          const Icon = item.icon;
          const selected = active === item.key;
          const content = (
            <>
              <Icon className="h-3.5 w-3.5" style={selected ? { color: item.color } : undefined} />
              {item.label}
            </>
          );
          const categoryRow = onSelectCategory ? (
            <button key={item.key} type="button" onClick={() => onSelectCategory(item.key)} className={rowClass(selected)}>
              {content}
            </button>
          ) : (
            <Link key={item.key} href={`/settings?category=${item.key}`} className={rowClass(selected)}>
              {content}
            </Link>
          );
          return (
            <div key={item.key}>
              {categoryRow}
              {index === 0 ? (
                <Link href="/settings/web-search" className={`${rowClass(active === "webSearch")} mt-0.5`}>
                  <Globe2 className="h-3.5 w-3.5" style={active === "webSearch" ? { color: "#002fa7" } : undefined} />
                  联网搜索
                </Link>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}
