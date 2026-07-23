"use client";

import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, Loader2 } from "lucide-react";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description: string;
  confirmLabel: string;
  cancelLabel?: string;
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}

export default function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  cancelLabel = "取消",
  busy = false,
  onConfirm,
  onClose,
}: ConfirmDialogProps) {
  const [mounted, setMounted] = useState(false);
  const titleId = useId();
  const descriptionId = useId();
  const cancelButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) {
        event.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    window.requestAnimationFrame(() => cancelButtonRef.current?.focus());
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [busy, onClose, open]);

  if (!mounted || !open) return null;

  return createPortal(
    <div
      className="fixed inset-0 z-[160] flex items-center justify-center bg-slate-950/25 px-4 py-6 backdrop-blur-[3px]"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        className="w-full max-w-[440px] rounded-[28px] border border-black/[0.08] bg-white p-5 shadow-2xl shadow-slate-950/20"
      >
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-rose-50 text-rose-600">
            <AlertTriangle className="h-5 w-5" />
          </div>
          <div className="min-w-0 flex-1 pt-0.5">
            <h2 id={titleId} className="text-[16px] font-bold text-slate-950">
              {title}
            </h2>
            <p id={descriptionId} className="mt-1.5 text-[13px] leading-6 text-slate-500">
              {description}
            </p>
          </div>
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button
            ref={cancelButtonRef}
            type="button"
            disabled={busy}
            onClick={onClose}
            className="h-10 rounded-xl border border-black/[0.08] bg-white px-4 text-[13px] font-semibold text-slate-600 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-45"
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onConfirm}
            className="inline-flex h-10 min-w-24 items-center justify-center gap-2 rounded-xl bg-rose-600 px-4 text-[13px] font-semibold text-white shadow-sm transition hover:bg-rose-700 disabled:cursor-wait disabled:opacity-65"
          >
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            {busy ? "处理中…" : confirmLabel}
          </button>
        </div>
      </section>
    </div>,
    document.body,
  );
}
