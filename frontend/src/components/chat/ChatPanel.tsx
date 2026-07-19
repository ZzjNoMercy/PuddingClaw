"use client";

import { useEffect, useMemo, useRef } from "react";
import { useApp, type SourceRecord } from "@/lib/store";
import ChatMessage from "./ChatMessage";
import ChatInput from "./ChatInput";
import { Loader2, Sparkles } from "lucide-react";

export default function ChatPanel() {
  const { messages, maintenanceStatus, isStreaming, currentRun, verificationReport } = useApp();
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
                verificationState={
                  msg.verificationState
                  || (msg.id !== lastAssistantId
                    ? undefined
                    : currentRun?.status === "evaluating"
                      ? "pending"
                      : verificationReport && verificationReport.run_id === currentRun?.run_id
                        ? verificationReport.status === "not_required"
                          || (
                            verificationReport.status === "satisfied"
                            && verificationReport.accepted_for_goal_revision !== false
                          )
                          ? "passed"
                          : "failed"
                        : undefined)
                }
                showInterruptionNotice={msg.id === lastMessageId}
              />
            ))}
            {maintenanceStatus && maintenanceStatus.phase === "global_summarization" && (
              <div className="sticky bottom-3 z-20 animate-fade-in px-4 py-2">
                <div className="mx-auto flex w-full max-w-[900px] items-center gap-3 rounded-2xl border border-blue-200 bg-blue-50/95 px-4 py-3 text-[13px] font-medium text-blue-900 shadow-lg shadow-blue-950/10 backdrop-blur">
                  <Loader2 className="h-4 w-4 shrink-0 animate-spin text-[#002fa7]" />
                  <div>
                    <p className="font-semibold">正在进行全局上下文压缩</p>
                    <p className="mt-0.5 text-[12px] font-normal text-blue-700">{maintenanceStatus.message}</p>
                  </div>
                </div>
              </div>
            )}
            {maintenanceStatus && maintenanceStatus.phase !== "global_summarization" && (
              <div className="animate-fade-in px-4 py-1.5">
                <div className="mx-auto w-full max-w-[900px]">
                  <div className="inline-flex items-center gap-2 rounded-full border border-black/[0.05] bg-white/70 px-3 py-1.5 text-[12px] text-gray-500 shadow-sm">
                    {maintenanceStatus.phase.endsWith("_done") ? (
                      <Sparkles className="h-3.5 w-3.5 text-emerald-500" />
                    ) : (
                      <Loader2 className="h-3.5 w-3.5 animate-spin text-[#002fa7]" />
                    )}
                    <span>{maintenanceStatus.message}</span>
                  </div>
                </div>
              </div>
            )}
            <div ref={bottomRef} />
          </div>
        )}
      </div>
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
