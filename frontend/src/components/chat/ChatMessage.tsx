"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { User, AlertTriangle, Key } from "lucide-react";
import type { ChatMessage as ChatMessageType } from "@/lib/store";
import ThoughtChain from "./ThoughtChain";
import RetrievalCard from "./RetrievalCard";

interface Props {
  message: ChatMessageType;
}

function formatTime(ts: number): string {
  const d = new Date(ts);
  return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

/** Detect 401 / API key errors */
function isAuthError(content: string): boolean {
  return /401|authentication.?fail|invalid.*api.?key|api.?key.*invalid/i.test(content);
}

export default function ChatMessage({ message }: Props) {
  const isUser = message.role === "user";
  const hasAuthError = !isUser && isAuthError(message.content);

  return (
    <div className="animate-fade-in px-4 py-1.5">
      <div className="max-w-2xl mx-auto">
        {/* User message — right-aligned bubble */}
        {isUser ? (
          <div className="flex justify-end gap-2">
            <div>
              <div className="bg-[#002fa7] text-white px-4 py-2.5 rounded-2xl rounded-tr-md text-[14px] leading-relaxed shadow-sm">
                {message.content}
              </div>
              <div className="text-[10px] text-gray-400 mt-1 text-right pr-1">
                {formatTime(message.timestamp)}
              </div>
            </div>
            <div className="shrink-0 w-7 h-7 rounded-full bg-gray-200 flex items-center justify-center mt-0.5">
              <User className="w-3.5 h-3.5 text-gray-500" />
            </div>
          </div>
        ) : (
          /* Assistant message — left-aligned */
          <div className="flex gap-2.5">
            <div className="shrink-0 w-7 h-7 rounded-full bg-gradient-to-br from-[#002fa7] to-[#4070ff] flex items-center justify-center mt-0.5 shadow-sm">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
                <path d="M12 2L2 7L12 12L22 7L12 2Z" fill="white" fillOpacity="0.9" />
                <path d="M2 17L12 22L22 17" stroke="white" strokeOpacity="0.7" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                <path d="M2 12L12 17L22 12" stroke="white" strokeOpacity="0.85" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </div>
            <div className="flex-1 min-w-0">
              {/* Tool calls */}
              {message.toolCalls && message.toolCalls.length > 0 && (
                <ThoughtChain toolCalls={message.toolCalls} />
              )}

              {/* Auth error alert */}
              {hasAuthError ? (
                <AuthErrorAlert content={message.content} />
              ) : message.content ? (
                <div>
                  <div className="bg-white/60 px-4 py-2.5 rounded-2xl rounded-tl-md text-[14px] leading-relaxed shadow-[0_1px_3px_rgba(0,0,0,0.04)] border border-black/[0.04]">
                    <div className="markdown-content">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {message.content}
                      </ReactMarkdown>
                    </div>
                  </div>
                  {message.retrievals && message.retrievals.length > 0 && (
                    <RetrievalCard retrievals={message.retrievals} />
                  )}
                  <div className="text-[10px] text-gray-400 mt-1 pl-1">
                    {formatTime(message.timestamp)}
                  </div>
                </div>
              ) : (
                /* Typing indicator */
                <div className="bg-white/60 px-4 py-3 rounded-2xl rounded-tl-md inline-flex items-center gap-1.5 shadow-[0_1px_3px_rgba(0,0,0,0.04)] border border-black/[0.04]">
                  <span className="typing-dot w-1.5 h-1.5 bg-[#002fa7] rounded-full" />
                  <span className="typing-dot w-1.5 h-1.5 bg-[#002fa7] rounded-full" />
                  <span className="typing-dot w-1.5 h-1.5 bg-[#002fa7] rounded-full" />
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** Prominent auth error alert with setup guidance */
function AuthErrorAlert({ content }: { content: string }) {
  return (
    <div className="animate-fade-in-scale rounded-xl border border-red-200 bg-red-50/80 px-4 py-3 space-y-2">
      <div className="flex items-center gap-2">
        <AlertTriangle className="w-4 h-4 text-red-500 shrink-0" />
        <span className="text-[13px] font-semibold text-red-700">
          API Key 认证失败
        </span>
      </div>
      <p className="text-[12px] text-red-600/80 leading-relaxed">
        你的 API Key 无效或未配置。请检查 <code className="bg-red-100 px-1 rounded text-red-700">backend/.env</code> 文件中的配置。
      </p>
      <div className="flex items-center gap-3 pt-1">
        <a
          href="http://localhost:8002/"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-[11px] font-medium text-red-600 hover:text-red-800 transition-colors"
        >
          <Key className="w-3 h-3" />
          检查后端状态
        </a>
        <span className="text-[10px] text-red-400">|</span>
        <span className="text-[10px] text-red-500 font-mono">{content.slice(0, 120)}...</span>
      </div>
    </div>
  );
}
