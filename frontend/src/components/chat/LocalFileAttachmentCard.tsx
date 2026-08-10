"use client";

import {
  File,
  FileArchive,
  FileCode2,
  FileImage,
  FileSpreadsheet,
  FileText,
  Globe2,
  Presentation,
} from "lucide-react";
import { openLocalFile } from "@/lib/api";

interface LocalFileAttachmentCardProps {
  href: string;
  filePath: string;
  sessionId: string;
}

function iconForFile(fileName: string) {
  const extension = fileName.split(".").pop()?.toLowerCase() || "";

  if (["html", "htm", "js", "mjs", "cjs", "ts", "tsx", "jsx", "css", "json", "yaml", "yml", "xml", "sql", "py", "sh"].includes(extension)) {
    return FileCode2;
  }
  if (["xls", "xlsx", "csv", "tsv"].includes(extension)) return FileSpreadsheet;
  if (["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"].includes(extension)) return FileImage;
  if (["ppt", "pptx", "key"].includes(extension)) return Presentation;
  if (["zip", "tar", "gz", "tgz", "7z", "rar"].includes(extension)) return FileArchive;
  if (["md", "markdown", "txt", "pdf", "doc", "docx", "rtf"].includes(extension)) return FileText;
  return File;
}

export default function LocalFileAttachmentCard({
  href,
  filePath,
  sessionId,
}: LocalFileAttachmentCardProps) {
  const fileName = filePath.split("/").filter(Boolean).pop() || "本地文件";
  const FileIcon = iconForFile(fileName);

  const openFile = async (event: React.MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    try {
      await openLocalFile(filePath, sessionId);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "打开本地文件失败");
    }
  };

  return (
    <a
      href={href}
      className="local-file-attachment-card not-prose my-2 flex w-full items-center gap-3 rounded-2xl bg-slate-100 px-4 py-3.5 text-slate-950 shadow-sm ring-1 ring-black/[0.035] transition hover:bg-slate-200/75 focus:outline-none focus-visible:ring-2 focus-visible:ring-violet-500/35"
      onClick={openFile}
      aria-label={`使用系统默认应用打开：${fileName}`}
      title={filePath}
    >
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-violet-600 text-white shadow-sm shadow-violet-600/20">
        <FileIcon className="h-[18px] w-[18px]" />
      </span>
      <span className="min-w-0 flex-1 truncate text-[14px] font-semibold">{fileName}</span>
      <Globe2 className="h-[18px] w-[18px] shrink-0 text-slate-600" aria-hidden="true" />
    </a>
  );
}
