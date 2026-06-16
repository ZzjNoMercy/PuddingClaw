"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight, Database } from "lucide-react";
import type { RetrievalResult } from "@/lib/store";

interface Props {
  retrievals: RetrievalResult[];
}

export default function RetrievalCard({ retrievals }: Props) {
  const [expanded, setExpanded] = useState(false);

  if (retrievals.length === 0) return null;

  return (
    <div className="mt-1.5 mb-2 rounded-xl border border-violet-200/60 bg-violet-50/40 overflow-hidden animate-fade-in-scale">
      <button
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-[12px] hover:bg-violet-50/60 transition-colors"
      >
        {expanded ? (
          <ChevronDown className="w-3 h-3 text-violet-400" />
        ) : (
          <ChevronRight className="w-3 h-3 text-violet-400" />
        )}
        <div className="w-5 h-5 rounded flex items-center justify-center bg-violet-100">
          <Database className="w-3 h-3 text-violet-600" />
        </div>
        <span className="font-medium text-violet-700">Memory Retrieval</span>
        <span className="text-[10px] text-violet-400 ml-1">
          {retrievals.length} snippet{retrievals.length > 1 ? "s" : ""}
        </span>
      </button>
      {expanded && (
        <div className="px-3 pb-2.5 space-y-2 border-t border-violet-100/60 pt-2">
          {retrievals.map((r, idx) => (
            <div key={idx} className="space-y-0.5">
              <div className="flex items-center gap-2 text-[10px]">
                <span className="font-semibold text-violet-500 uppercase tracking-wider">
                  {r.source}
                </span>
                <span className="text-violet-400">score: {r.score}</span>
              </div>
              <pre className="p-2 bg-white/70 rounded-lg text-[11px] text-gray-600 font-mono whitespace-pre-wrap leading-relaxed max-h-32 overflow-y-auto border border-violet-100/40">
                {r.text}
              </pre>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
