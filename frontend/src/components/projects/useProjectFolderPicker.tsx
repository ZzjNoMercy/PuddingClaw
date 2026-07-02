"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { createPortal } from "react-dom";
import { AlertCircle, Clipboard, FolderPlus, Monitor, X } from "lucide-react";

interface UseProjectFolderPickerOptions {
  onPathSelected: (path: string) => Promise<boolean | void>;
}

export function useProjectFolderPicker({ onPathSelected }: UseProjectFolderPickerOptions) {
  const [isOpen, setIsOpen] = useState(false);
  const [pathValue, setPathValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isMounted, setIsMounted] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setIsMounted(true);
  }, []);

  const closeDialog = useCallback(() => {
    if (isSubmitting) return;
    setIsOpen(false);
    setPathValue("");
    setError(null);
  }, [isSubmitting]);

  const submitPath = useCallback(
    async (rawPath: string) => {
      const nextPath = rawPath.trim();
      if (!nextPath) {
        setError("请粘贴一个本地项目目录路径。");
        return false;
      }

      setIsSubmitting(true);
      setError(null);
      try {
        const result = await onPathSelected(nextPath);
        if (result === false) {
          setError("项目目录登记失败，请确认路径存在且是文件夹。");
          return false;
        }
        setIsOpen(false);
        setPathValue("");
        return true;
      } finally {
        setIsSubmitting(false);
      }
    },
    [onPathSelected]
  );

  const openProjectFolderPicker = useCallback(async () => {
    if (window.electron?.selectProjectFolder) {
      try {
        const selectedPath = await window.electron.selectProjectFolder();
        if (selectedPath?.trim()) {
          await submitPath(selectedPath);
        }
        return;
      } catch {
        setPathValue("");
        setError("系统目录选择器打开失败，请直接粘贴本地项目目录路径。");
        setIsOpen(true);
        return;
      }
    }

    setPathValue("");
    setError("当前窗口无法读取系统目录选择器，请粘贴本地项目目录路径。");
    setIsOpen(true);
  }, [submitPath]);

  useEffect(() => {
    if (!isOpen) return;
    const frame = window.requestAnimationFrame(() => inputRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeDialog();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [closeDialog, isOpen]);

  const handleSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      await submitPath(pathValue);
    },
    [pathValue, submitPath]
  );

  const dialogContent = isOpen ? (
    <div className="fixed inset-0 z-[80] flex items-center justify-center px-4 py-6">
      <button
        type="button"
        aria-label="关闭添加项目弹窗"
        className="absolute inset-0 bg-slate-950/20 backdrop-blur-sm"
        onClick={closeDialog}
      />

      <form
        onSubmit={handleSubmit}
        className="relative w-full max-w-[520px] overflow-hidden rounded-3xl border border-black/[0.06] bg-white/95 p-5 shadow-2xl shadow-slate-900/18 backdrop-blur-xl animate-fade-in-scale"
      >
        <div className="flex items-start justify-between gap-4">
          <div className="flex min-w-0 items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-[#002fa7]/[0.08] text-[#002fa7]">
              <FolderPlus className="h-5 w-5" />
            </div>
            <div className="min-w-0">
              <h2 className="text-[15px] font-semibold text-gray-950">添加本地项目</h2>
              <p className="mt-1 text-[12px] leading-relaxed text-gray-500">
                粘贴项目文件夹路径，PuddingClaw 会把它登记为 Agent 工作区。
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={closeDialog}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-gray-400 transition-colors hover:bg-black/[0.05] hover:text-gray-700"
            title="关闭"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <label className="mt-5 block">
          <span className="mb-2 flex items-center gap-1.5 text-[11px] font-medium text-gray-500">
            <Clipboard className="h-3.5 w-3.5" />
            本地目录路径
          </span>
          <input
            ref={inputRef}
            value={pathValue}
            onChange={(event) => {
              setPathValue(event.target.value);
              if (error) setError(null);
            }}
            placeholder="/Users/pet/Code/AI/Agent/PuddingClaw"
            className="h-11 w-full rounded-2xl border border-black/[0.08] bg-white/70 px-3.5 text-[13px] text-gray-900 outline-none transition-all placeholder:text-gray-300 focus:border-[#002fa7]/35 focus:bg-white focus:ring-4 focus:ring-[#002fa7]/[0.08]"
          />
        </label>

        {error && (
          <div className="mt-3 flex items-start gap-2 rounded-2xl border border-red-500/10 bg-red-50 px-3 py-2 text-[12px] leading-relaxed text-red-600">
            <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        <div className="mt-4 flex items-center gap-2 rounded-2xl bg-black/[0.025] px-3 py-2 text-[11px] leading-relaxed text-gray-500">
          <Monitor className="h-3.5 w-3.5 shrink-0 text-gray-400" />
          <span>桌面版会优先打开系统目录选择器；浏览器环境不能把所选文件夹路径交给后端，需要粘贴目录路径。</span>
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={closeDialog}
            disabled={isSubmitting}
            className="h-9 rounded-full border border-black/[0.08] bg-white/60 px-4 text-[13px] font-medium text-gray-600 transition-colors hover:bg-white hover:text-gray-900 disabled:opacity-50"
          >
            取消
          </button>
          <button
            type="submit"
            disabled={isSubmitting}
            className="h-9 rounded-full bg-[#002fa7] px-4 text-[13px] font-medium text-white shadow-sm transition-all hover:bg-[#001f7a] active:scale-[0.98] disabled:cursor-wait disabled:opacity-60"
          >
            {isSubmitting ? "登记中..." : "添加项目"}
          </button>
        </div>
      </form>
    </div>
  ) : null;

  const projectFolderDialog =
    isMounted && dialogContent ? createPortal(dialogContent, document.body) : null;

  return {
    openProjectFolderPicker,
    projectFolderDialog,
  };
}
