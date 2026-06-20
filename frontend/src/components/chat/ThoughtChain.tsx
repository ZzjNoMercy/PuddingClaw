"use client";

import { useState } from "react";
import {
  ChevronDown, ChevronRight, Terminal, Code, Globe,
  FileText, Search, Loader2, CheckCircle2,
  XCircle,
} from "lucide-react";
import type { ToolCall } from "@/lib/store";

const TOOL_META: Record<string, { icon: React.ElementType; color: string; bg: string }> = {
  terminal:              { icon: Terminal,  color: "#6b7280", bg: "#f3f4f6" },
  python_repl:           { icon: Code,     color: "#2563eb", bg: "#eff6ff" },
  fetch_url:             { icon: Globe,    color: "#059669", bg: "#ecfdf5" },
  read_file:             { icon: FileText, color: "#d97706", bg: "#fffbeb" },
  search_knowledge_base: { icon: Search,   color: "#7c3aed", bg: "#f5f3ff" },
};

interface Props { toolCalls: ToolCall[] }

export default function ThoughtChain({ toolCalls }: Props) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  if (toolCalls.length === 0) return null;

  return (
    <div className="mb-2 space-y-1">
      {toolCalls.map((tc, idx) => {
        const meta = TOOL_META[tc.tool] || TOOL_META.terminal;
        const Icon = meta.icon;
        const key = tc.id || `${idx}`;
        const isOpen = expanded[key] ?? false;

        return (
          <div
            key={key}
            className={`animate-fade-in-scale overflow-hidden rounded-lg border bg-white ${
              tc.is_error ? "border-red-200" : "border-slate-200"
            }`}
          >
            <button
              onClick={() => setExpanded((p) => ({ ...p, [key]: !p[key] }))}
              className={`w-full flex items-center gap-2 px-3 py-1.5 text-[12px] transition-colors ${
                tc.is_error ? "hover:bg-red-50/70" : "hover:bg-slate-50"
              }`}
            >
              {isOpen
                ? <ChevronDown className="w-3 h-3 text-gray-400" />
                : <ChevronRight className="w-3 h-3 text-gray-400" />
              }
              <div
                className="w-5 h-5 rounded flex items-center justify-center"
                style={{ background: meta.bg }}
              >
                <Icon className="w-3 h-3" style={{ color: meta.color }} />
              </div>
              <span className="font-medium text-gray-700">{tc.tool}</span>
              {tc.summary_source && (
                <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-gray-500">
                  {tc.summary_source}
                </span>
              )}
              <span className="ml-auto">
                {tc.status === "running"
                  ? <Loader2 className="w-3.5 h-3.5 text-amber-500 animate-spin" />
                  : tc.is_error
                  ? <XCircle className="w-3.5 h-3.5 text-red-500" />
                  : <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500" />
                }
              </span>
            </button>
            {isOpen && (
              <div className="space-y-1.5 border-t border-slate-100 px-3 pb-2 pt-1.5 text-[11px]">
                {tc.input && (
                  <div>
                    <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">Input</span>
                    <pre className="mt-0.5 overflow-x-auto whitespace-pre-wrap rounded-lg bg-slate-50 p-2 font-mono leading-relaxed text-gray-600">
                      {tc.input}
                    </pre>
                  </div>
                )}
                {tc.output && (
                  <div>
                    <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">Output</span>
                    <pre className={`mt-0.5 max-h-36 overflow-y-auto overflow-x-auto whitespace-pre-wrap rounded-lg p-2 font-mono leading-relaxed ${
                      tc.is_error ? "bg-red-50 text-red-700" : "bg-slate-50 text-gray-600"
                    }`}>
                      {tc.output}
                    </pre>
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
