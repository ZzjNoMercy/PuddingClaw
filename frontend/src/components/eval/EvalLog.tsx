import { useEffect, useRef } from "react";
import { Terminal } from "lucide-react";

interface EvalLogProps {
  logLines: string[];
}

export function EvalLog({ logLines }: EvalLogProps) {
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom whenever logLines changes
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [logLines]);

  return (
    <div className="flex flex-col h-full bg-gray-900 rounded-xl overflow-hidden">
      {/* Title bar */}
      <div className="flex items-center justify-between bg-gray-800 px-3 py-1.5 flex-shrink-0">
        <div className="flex items-center gap-1.5">
          <Terminal className="w-3 h-3 text-gray-400" />
          <span className="text-[11px] font-semibold text-gray-300">
            评估日志
          </span>
        </div>
        <span className="text-[10px] text-gray-500">
          {logLines.length} 行
        </span>
      </div>

      {/* Log content */}
      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto px-3 py-2 min-h-0"
      >
        {logLines.length === 0 ? (
          <div className="flex items-center h-full">
            <span className="text-[11px] text-gray-600 font-mono animate-pulse">
              等待评估输出...
            </span>
          </div>
        ) : (
          <div className="flex flex-col gap-0.5">
            {logLines.map((line, index) => (
              <div key={index} className="flex gap-2 items-start">
                {/* Line number */}
                <span className="text-[10px] font-mono text-gray-600 w-6 text-right flex-shrink-0 select-none leading-[1.6]">
                  {index + 1}
                </span>
                {/* Line content */}
                <span className="text-[11px] font-mono text-gray-300 leading-[1.6] break-all">
                  {line}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
