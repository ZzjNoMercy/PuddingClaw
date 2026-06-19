"use client";

import { useEffect, useRef } from "react";
import { useApp } from "@/lib/store";
import ChatMessage from "./ChatMessage";
import ChatInput from "./ChatInput";
import { Loader2, Sparkles } from "lucide-react";

export default function ChatPanel() {
  const { messages, maintenanceStatus } = useApp();
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, maintenanceStatus]);

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 overflow-y-auto scroll-shadow">
        {messages.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full">
            <div className="w-14 h-14 rounded-2xl bg-gradient-to-br from-[#002fa7] to-[#4070ff] flex items-center justify-center mb-4 shadow-lg shadow-[#002fa7]/15">
              <Sparkles className="w-7 h-7 text-white" />
            </div>
            <h2 className="text-lg font-semibold text-gray-800 mb-1">
              Hi, how can I help?
            </h2>
            <p className="text-[13px] text-gray-400 max-w-xs text-center leading-relaxed">
              Ask me anything, or try{" "}
              <span className="text-[#ff6723] font-medium">&quot;查询北京天气&quot;</span>
            </p>
            <div className="flex flex-wrap gap-2 mt-5 max-w-md justify-center">
              {["你好，介绍一下自己", "查询北京天气", "帮我写一段Python代码"].map((hint) => (
                <QuickHint key={hint} text={hint} />
              ))}
            </div>
          </div>
        ) : (
          <div className="py-4">
            {messages.map((msg) => (
              <ChatMessage key={msg.id} message={msg} />
            ))}
            {maintenanceStatus && (
              <div className="animate-fade-in px-4 py-1.5">
                <div className="max-w-2xl mx-auto">
                  <div className="inline-flex items-center gap-2 rounded-full border border-[#002fa7]/10 bg-white/70 px-3 py-1.5 text-[12px] text-gray-500 shadow-[0_1px_3px_rgba(0,0,0,0.04)]">
                    <Loader2 className="h-3.5 w-3.5 animate-spin text-[#002fa7]" />
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
      className="px-3 py-1.5 rounded-full text-[12px] text-gray-500 bg-white/60 border border-black/[0.04] hover:bg-white hover:shadow-sm hover:text-gray-700 transition-all"
    >
      {text}
    </button>
  );
}
