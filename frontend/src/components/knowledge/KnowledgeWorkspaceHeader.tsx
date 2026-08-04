import type { ReactNode } from "react";
import { ChevronRight } from "lucide-react";

import WorkspacePageHeader from "@/components/layout/WorkspacePageHeader";

type Section = "overview" | "library" | "readLater" | "wiki" | "tasks";

const sectionCopy: Record<Section, { eyebrow: string; label?: string; description: string }> = {
  overview: {
    eyebrow: "KNOWLEDGE WORKSPACE",
    description: "从资料与链接开始，按需要沉淀为 Wiki，再交给检索和 Agent 使用。",
  },
  library: {
    eyebrow: "LIBRARY WORKSPACE",
    label: "资料库",
    description: "统一管理 PDF、Markdown 与表格等资料，并同步到本地目录和多模态索引。",
  },
  readLater: {
    eyebrow: "READING WORKSPACE",
    label: "稍后读",
    description: "先收藏链接并由后台整理正文，需要时再编译为 Wiki。",
  },
  wiki: {
    eyebrow: "WIKI WORKSPACE",
    label: "LLM Wiki",
    description: "维护知识 Schema，将 Raw 编译为互联 Wiki，并按需导入 GBrain。",
  },
  tasks: {
    eyebrow: "TASK WORKSPACE",
    label: "任务中心",
    description: "统一查看 Wiki 编译、文件解析、向量与实体导入；离开页面后后台仍会继续处理。",
  },
};

export default function KnowledgeWorkspaceHeader({ section, actions }: { section: Section; actions?: ReactNode }) {
  const copy = sectionCopy[section];
  const title = copy.label ? (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <span>知识库</span>
      <ChevronRight className="h-5 w-5 text-gray-300" aria-hidden="true" />
      <span className="text-2xl text-gray-700">{copy.label}</span>
    </span>
  ) : "知识库";

  return (
    <WorkspacePageHeader
      eyebrow={copy.eyebrow}
      title={title}
      description={copy.description}
      actions={actions}
    />
  );
}
