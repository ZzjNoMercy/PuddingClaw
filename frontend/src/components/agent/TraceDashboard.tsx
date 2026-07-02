"use client";

import type React from "react";
import { ArrowLeft, Cpu, ListTree, Network, Radio, Timer } from "lucide-react";
import TraceViewer from "@/components/agent/TraceViewer";
import type { AgentTrace } from "@/lib/api";
import { useApp } from "@/lib/store";

export default function TraceDashboard() {
  const {
    trace,
    traceHistory,
    selectedTraceQueryId,
    selectTraceQuery,
    graph,
    activeGraphNode,
    isStreaming,
    setWorkspaceView,
  } = useApp();
  const spanCount = trace?.spans?.length || 0;
  const nodeCount = graph?.nodes?.length || 0;
  const edgeCount = graph?.edges?.length || 0;
  const status = trace?.status || (isStreaming ? "running" : "idle");

  return (
    <div className="flex h-full flex-col overflow-hidden bg-transparent">
      <div className="shrink-0 border-b border-black/[0.06] bg-white/70 px-5 py-4 backdrop-blur">
        <div className="mx-auto flex w-full max-w-[1280px] items-center justify-between gap-4">
          <div className="flex min-w-0 items-center gap-3">
            <button
              type="button"
              onClick={() => setWorkspaceView("chat")}
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-black/[0.06] bg-white text-slate-500 shadow-sm transition-colors hover:text-slate-900"
              aria-label="返回对话"
              title="返回对话"
            >
              <ArrowLeft className="h-4 w-4" />
            </button>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <Cpu className="h-5 w-5 text-[#002fa7]" />
                <h1 className="truncate text-[18px] font-bold text-slate-950">Trace 看板</h1>
              </div>
              <p className="mt-0.5 text-[12px] text-slate-500">
                LangGraph 节点、middleware、skill、tools、memory 写入的运行轨迹
              </p>
            </div>
          </div>

          <div className="hidden items-center gap-2 md:flex">
            <Metric icon={<Radio className="h-3.5 w-3.5" />} label={statusLabel(status)} tone={status === "running" ? "blue" : "slate"} />
            <Metric icon={<Network className="h-3.5 w-3.5" />} label={`${nodeCount} 节点 / ${edgeCount} 边`} />
            <Metric icon={<Timer className="h-3.5 w-3.5" />} label={`${spanCount} span`} />
          </div>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        <div className="mx-auto w-full max-w-[1280px]">
          <div className="space-y-4">
            <QueryList
              traces={traceHistory}
              selectedQueryId={selectedTraceQueryId || trace?.query_id || null}
              onSelect={selectTraceQuery}
            />
            <TraceViewer
              trace={trace}
              graph={graph}
              activeGraphNode={activeGraphNode}
              maxGraphHeight={760}
              minCanvasWidth={960}
            />
          </div>
        </div>
      </div>
    </div>
  );
}

function QueryList({
  traces,
  selectedQueryId,
  onSelect,
}: {
  traces: Record<string, AgentTrace>;
  selectedQueryId: string | null;
  onSelect: (queryId: string) => void;
}) {
  const items = Object.values(traces || {}).sort((a, b) => b.started_at - a.started_at);
  return (
    <section className="rounded-xl border border-slate-100 bg-white p-3 shadow-sm">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
        <ListTree className="h-4 w-4 text-slate-500" />
        <span className="text-[13px] font-semibold text-slate-800">Query 列表</span>
        </div>
        <span className="text-[11px] text-slate-400">{items.length} 次请求</span>
      </div>
      {items.length === 0 ? (
        <p className="rounded-lg bg-slate-50 px-3 py-3 text-[12px] text-slate-400">
          运行 Agent 后，每次用户请求会在这里生成一条 Trace。
        </p>
      ) : (
        <div className="flex gap-2 overflow-x-auto pb-1">
          {items.map((item, index) => {
            const isSelected = item.query_id === selectedQueryId;
            return (
              <button
                key={item.query_id || item.trace_id}
                type="button"
                onClick={() => item.query_id && onSelect(item.query_id)}
                className={`min-w-[180px] rounded-lg border px-3 py-2 text-left transition-colors ${
                  isSelected
                    ? "border-blue-200 bg-blue-50 text-blue-700"
                    : "border-transparent bg-slate-50 text-slate-600 hover:border-slate-200 hover:bg-white"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-[12px] font-semibold">
                    Query {items.length - index}
                  </span>
                  <span className="text-[10px] opacity-70">{statusLabel(item.status)}</span>
                </div>
                <div className="mt-1 truncate text-[10px] opacity-70">
                  {item.spans.length} span · {formatClock(item.started_at)}
                </div>
              </button>
            );
          })}
        </div>
      )}
    </section>
  );
}

function Metric({
  icon,
  label,
  tone = "slate",
}: {
  icon: React.ReactNode;
  label: string;
  tone?: "slate" | "blue";
}) {
  return (
    <div
      className={`flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-[12px] font-medium ${
        tone === "blue"
          ? "border-blue-100 bg-blue-50 text-blue-700"
          : "border-black/[0.06] bg-white text-slate-500"
      }`}
    >
      {icon}
      {label}
    </div>
  );
}

function statusLabel(status: string) {
  if (status === "running") return "运行中";
  if (status === "completed") return "已完成";
  if (status === "error") return "异常";
  return "待运行";
}

function formatClock(timestamp: number) {
  try {
    return new Date(timestamp * 1000).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return "";
  }
}
