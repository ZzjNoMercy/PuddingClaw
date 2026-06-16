"use client";

import { useState } from "react";
import {
  ChevronDown, ChevronRight, Terminal, Code, Globe,
  FileText, Search, Loader2, CheckCircle2,
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
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});
  if (toolCalls.length === 0) return null;

  return (
    <div className="mb-2 space-y-1">
      {toolCalls.map((tc, idx) => {
        const meta = TOOL_META[tc.tool] || TOOL_META.terminal;
        const Icon = meta.icon;
        const isOpen = expanded[idx] ?? false;

        return (
          <div key={idx} className="rounded-xl border border-black/[0.04] bg-white/50 overflow-hidden animate-fade-in-scale">
            <button
              onClick={() => setExpanded((p) => ({ ...p, [idx]: !p[idx] }))}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-[12px] hover:bg-black/[0.02] transition-colors"
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
              <span className="font-medium text-gray-600">{tc.tool}</span>
              <span className="ml-auto">
                {tc.status === "running"
                  ? <Loader2 className="w-3.5 h-3.5 text-amber-500 animate-spin" />
                  : <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500" />
                }
              </span>
            </button>
            {isOpen && (
              <div className="px-3 pb-2 text-[11px] space-y-1.5 border-t border-black/[0.03] pt-1.5">
                {tc.input && (
                  <div>
                    <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">Input</span>
                    <pre className="mt-0.5 p-2 bg-gray-50/80 rounded-lg overflow-x-auto whitespace-pre-wrap text-gray-600 font-mono leading-relaxed">
                      {tc.input}
                    </pre>
                  </div>
                )}
                {tc.output && (
                  <div>
                    <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">Output</span>
                    <pre className="mt-0.5 p-2 bg-gray-50/80 rounded-lg overflow-x-auto whitespace-pre-wrap text-gray-600 font-mono max-h-36 overflow-y-auto leading-relaxed">
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
