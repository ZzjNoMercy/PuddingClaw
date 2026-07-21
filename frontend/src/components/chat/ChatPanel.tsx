"use client";

import { useEffect, useMemo, useRef } from "react";
import { useApp, type SourceRecord } from "@/lib/store";
import ChatMessage from "./ChatMessage";
import ChatInput from "./ChatInput";
import { Loader2, Sparkles } from "lucide-react";

export default function ChatPanel() {
  const {
    messages,
    maintenanceStatus,
    runActivityStatus,
    isStreaming,
    hasActiveRun,
  } = useApp();
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, maintenanceStatus]);

  const lastAssistantId = [...messages].reverse().find((m) => m.role === "assistant")?.id;
  const lastMessageId = messages[messages.length - 1]?.id;
  const sessionSources = useMemo(() => {
    const catalog = new Map<string, SourceRecord>();
    for (const message of messages) {
      for (const source of message.sources || []) {
        catalog.set(source.source_id, { ...catalog.get(source.source_id), ...source });
      }
    }
    return Array.from(catalog.values());
  }, [messages]);

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 overflow-y-auto">
        {messages.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center px-6 pb-16">
            <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-xl bg-gradient-to-br from-[#002fa7] to-[#4070ff] shadow-lg shadow-blue-900/10">
              <Sparkles className="h-6 w-6 text-white" />
            </div>
            <h2 className="mb-1 text-lg font-semibold text-gray-900">
              准备开始这个工作台
            </h2>
            <p className="max-w-sm text-center text-[13px] leading-relaxed text-gray-500">
              保留当前对话能力，同时把会话、扩展和上下文状态集中到一个更安静的工作区。
            </p>
            <div className="mt-5 flex max-w-md flex-wrap justify-center gap-2">
              {["你好，介绍一下自己", "查询北京天气", "帮我写一段Python代码"].map((hint) => (
                <QuickHint key={hint} text={hint} />
              ))}
            </div>
          </div>
        ) : (
          <div className="py-5 pb-3">
            {messages.map((msg) => (
              <ChatMessage
                key={msg.id}
                message={msg}
                sessionSources={sessionSources}
                isStreaming={isStreaming && msg.id === lastAssistantId}
                showInterruptionNotice={msg.id === lastMessageId}
              />
            ))}
            <div ref={bottomRef} />
          </div>
        )}
      </div>
      {(runActivityStatus || maintenanceStatus || hasActiveRun) ? (
        <div
          className="shrink-0 px-7 pb-2 pt-1 animate-fade-in"
          role="status"
          aria-live="polite"
          data-testid="run-activity-status"
        >
          <div className="mx-auto flex w-full max-w-[900px]">
            <div className={`inline-flex max-w-full items-center gap-2 rounded-full border px-3 py-1.5 text-[12px] shadow-sm backdrop-blur ${
              runActivityStatus?.phase === "permission"
                ? "border-amber-200 bg-amber-50/95 text-amber-800"
                : "border-blue-100 bg-white/90 text-slate-600"
            }`}>
              {maintenanceStatus?.phase.endsWith("_done") ? (
                <Sparkles className="h-3.5 w-3.5 shrink-0 text-emerald-500" />
              ) : (
                <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-[#002fa7]" />
              )}
              <span className="shrink-0 font-medium text-slate-800">
                {maintenanceStatus
                  ? maintenanceStatus.phase === "global_summarization"
                    ? "正在进行全局上下文压缩"
                    : maintenanceStatus.message
                  : runActivityStatus?.label || "Agent 正在处理"}
              </span>
              {(runActivityStatus?.detail || maintenanceStatus?.phase === "global_summarization") ? (
                <span className="truncate text-slate-400">
                  {runActivityStatus?.detail || (
                    maintenanceStatus?.usedTokensBefore != null && maintenanceStatus?.triggerTokens != null
                      ? `${Math.round(maintenanceStatus.usedTokensBefore / 1000)}k / ${Math.round(maintenanceStatus.triggerTokens / 1000)}k`
                      : maintenanceStatus?.message
                  )}
                </span>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}
      <ChatInput />
    </div>
  );
}

function QuickHint({ text }: { text: string }) {
  const { sendMessage, isStreaming } = useApp();
  return (
    <button
      onClick={() => !isStreaming && sendMessage(text)}
      className="rounded-full border border-black/[0.06] bg-white/58 px-3 py-1.5 text-[12px] text-gray-500 transition-all hover:bg-white/80 hover:text-gray-800 hover:shadow-sm"
    >
      {text}
    </button>
  );
}
