"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Circle,
  Copy,
  Database,
  Cpu,
  ExternalLink,
  FileText,
  KeyRound,
  ListChecks,
  MessageSquare,
  Network,
  PlugZap,
  Route,
  Split,
  XCircle,
} from "lucide-react";
import { markdownRemarkPlugins } from "@/lib/markdown";
import type {
  AgentTrace,
  GraphStructure,
  TraceHookBoundarySnapshot,
  TraceMiddlewareEffect,
  TraceMiddlewareInvocation,
  TraceRuntimeInventory,
  TraceRuntimeMiddlewareEntry,
  TraceSpan,
} from "@/lib/api";

interface TraceViewerProps {
  trace: AgentTrace | null;
  graph: GraphStructure | null;
  activeGraphNode: string | null;
  maxGraphHeight?: number;
  minCanvasWidth?: number;
}

function formatMiddlewareSource(source: string | undefined): string {
  if (!source) return "runtime";
  // User-facing label: project-specific middleware are registered under the
  // custom layer, even if the backend currently tags them as "user".
  return source.replace(/^puddingclaw\.user$/, "puddingclaw.custom");
}

function middlewareOrderLabel(entry: TraceRuntimeMiddlewareEntry): string {
  const executionOrder = entry.execution_order;
  const stackOrder = entry.stack_order || entry.order;
  if (executionOrder && stackOrder && executionOrder !== stackOrder) {
    return `执行 #${executionOrder} · stack #${stackOrder}`;
  }
  if (executionOrder) return `执行 #${executionOrder}`;
  if (stackOrder) return `stack #${stackOrder}`;
  return "执行顺序 —";
}

export default function TraceViewer({
  trace,
  graph,
  activeGraphNode,
  maxGraphHeight = 520,
  minCanvasWidth = 360,
}: TraceViewerProps) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [expandedFlow, setExpandedFlow] = useState<Set<string>>(new Set());
  const [mainView, setMainView] = useState<"harness" | "flow" | "middleware" | "graph">("harness");
  const [runView, setRunView] = useState<"flow" | "tree">("flow");
  const [selectedType, setSelectedType] = useState<TraceSpan["type"] | null>(null);
  const [selectedSpan, setSelectedSpan] = useState<TraceSpan | null>(null);

  const toggle = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const spansByParent = useMemo(() => {
    const map = new Map<string | null, TraceSpan[]>();
    if (!trace) return map;
    const walk = (span: TraceSpan) => {
      const key = span.parent_id ?? null;
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(span);
      for (const child of span.children || []) walk(child);
    };
    for (const span of trace.spans) walk(span);
    Array.from(map.values()).forEach((list) => {
      list.sort((a: TraceSpan, b: TraceSpan) => a.started_at - b.started_at);
    });
    return map;
  }, [trace]);

  const rootSpans = useMemo(() => {
    const roots = spansByParent.get(null) || [];
    return roots.sort((a: TraceSpan, b: TraceSpan) => a.started_at - b.started_at);
  }, [spansByParent]);

  const hasTrace = trace && trace.spans && trace.spans.length > 0;
  const hasGraph = graph && graph.nodes && graph.nodes.length > 0;
  const graphActivity = useMemo(
    () => buildGraphActivity(trace, trace?.status === "running" ? activeGraphNode : null),
    [trace, activeGraphNode]
  );
  const summary = useMemo(() => buildHarnessSummary(trace), [trace]);
  const flatSpans = useMemo(() => flattenTraceSpans(trace), [trace]);
  const actualFlow = useMemo(() => buildActualFlowForTrace(trace, flatSpans), [trace, flatSpans]);
  useEffect(() => {
    emitTraceFlowDebug(trace, flatSpans, actualFlow);
  }, [trace, flatSpans, actualFlow]);
  const filteredActualFlow = useMemo(
    () => filterActualFlow(actualFlow, selectedType),
    [actualFlow, selectedType]
  );
  const visibleRootSpans = useMemo(() => {
    if (!selectedType) return rootSpans;
    const ids = new Set(
      flatSpans
        .filter((span) => span.type === selectedType)
        .flatMap((span) => lineageIds(span, flatSpans))
    );
    return rootSpans.filter((span) => ids.has(span.id));
  }, [flatSpans, rootSpans, selectedType]);

  const handleSummarySelect = (type: TraceSpan["type"] | null) => {
    setMainView("flow");
    setSelectedType(type);
    setRunView("flow");
    if (!type) {
      setSelectedSpan(null);
      return;
    }
    const matches = flatSpans.filter((span) => span.type === type);
    const first = matches[0] || null;
    setSelectedSpan(first);
    if (first) {
      const ids = lineageIds(first, flatSpans);
      setExpanded((prev) => new Set([...Array.from(prev), ...ids]));
    }
  };

  return (
    <div className="space-y-3">
      {hasTrace && trace.runtime_inventory && (
        <RuntimeMountPanel inventory={trace.runtime_inventory} />
      )}

      {hasTrace && (
        <TracePerspectiveSwitch
          view={mainView}
          onChange={setMainView}
          trace={trace}
          hasGraph={Boolean(hasGraph)}
        />
      )}

      {hasTrace && mainView === "harness" && (
        <HarnessTraceOverview
          summary={summary}
          selectedType={selectedType}
          onSelect={handleSummarySelect}
        />
      )}

      {hasTrace && mainView === "flow" && (
        <TraceRunPanel
          view={runView}
          onViewChange={setRunView}
          trace={trace}
          flowItems={filteredActualFlow}
          expandedFlow={expandedFlow}
          onToggleFlow={(id) =>
            setExpandedFlow((prev) => {
              const next = new Set(prev);
              if (next.has(id)) next.delete(id);
              else next.add(id);
              return next;
            })
          }
          visibleRootSpans={visibleRootSpans}
          spansByParent={spansByParent}
          expandedTree={expanded}
          onToggleTree={toggle}
          selectedType={selectedType}
          selectedSpan={selectedSpan}
          selectedSpanId={selectedSpan?.id || null}
          onSelect={setSelectedSpan}
          onCloseDetail={() => setSelectedSpan(null)}
          onClearFilter={() => handleSummarySelect(null)}
        />
      )}

      {hasTrace && mainView === "middleware" && trace.runtime_inventory && (
        <MiddlewareTracePanel inventory={trace.runtime_inventory} trace={trace} />
      )}

      {hasTrace && mainView === "middleware" && !trace.runtime_inventory && (
        <EmptyTraceBlock title="没有运行挂载清单" detail="这条 trace 没有 runtime inventory 快照。" />
      )}

      {hasTrace && mainView === "graph" && (
        <GraphInspectorPanel
          trace={trace}
          graph={graph}
          hasGraph={Boolean(hasGraph)}
          activeGraphNode={activeGraphNode}
          graphActivity={graphActivity}
          maxGraphHeight={maxGraphHeight}
          minCanvasWidth={minCanvasWidth}
        />
      )}

      {!hasTrace && (
        <div className="flex flex-col items-center justify-center py-12 text-center">
          <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-2xl bg-black/[0.045]">
            <Cpu className="h-6 w-6 text-slate-400" />
          </div>
          <p className="text-[14px] font-medium text-slate-400">运行链路将显示在这里</p>
          <p className="mt-1 max-w-[200px] text-[12px] text-slate-400">
            Agent 运行时会实时更新 trace
          </p>
        </div>
      )}
    </div>
  );
}

function TracePerspectiveSwitch({
  view,
  onChange,
  trace,
  hasGraph,
}: {
  view: "harness" | "flow" | "middleware" | "graph";
  onChange: (view: "harness" | "flow" | "middleware" | "graph") => void;
  trace: AgentTrace;
  hasGraph: boolean;
}) {
  const effects = trace.middleware_effects?.length || 0;
  const tabs = [
    { key: "harness" as const, label: "Harness 视图", count: trace.status === "running" ? "运行中" : "摘要" },
    { key: "flow" as const, label: "流程视图", count: `${trace.spans.length} span` },
    { key: "middleware" as const, label: "中间件视图", count: `${effects} effect` },
    { key: "graph" as const, label: "编译图检查", count: hasGraph ? "graph" : "空" },
  ];
  return (
    <div className="rounded-xl border border-slate-100 bg-white p-2 shadow-sm">
      <div className="grid gap-1 rounded-lg bg-slate-100 p-1 sm:grid-cols-4">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            type="button"
            onClick={() => onChange(tab.key)}
            className={`flex min-w-0 items-center justify-center gap-2 rounded-md px-3 py-2 text-[12px] font-semibold transition-colors ${
              view === tab.key
                ? "bg-white text-slate-900 shadow-sm"
                : "text-slate-500 hover:text-slate-800"
            }`}
          >
            <span className="truncate">{tab.label}</span>
            <span
              className={`shrink-0 rounded-full px-1.5 py-0.5 text-[9px] font-medium ${
                view === tab.key ? "bg-slate-100 text-slate-500" : "bg-white/60 text-slate-400"
              }`}
            >
              {tab.count}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

function HarnessTraceOverview({
  summary,
  selectedType,
  onSelect,
}: {
  summary: HarnessSummaryItem[];
  selectedType: TraceSpan["type"] | null;
  onSelect: (type: TraceSpan["type"]) => void;
}) {
  return (
    <div className="space-y-3">
      <HarnessSummary summary={summary} selectedType={selectedType} onSelect={onSelect} />
    </div>
  );
}

function GraphInspectorPanel({
  trace,
  graph,
  hasGraph,
  activeGraphNode,
  graphActivity,
  maxGraphHeight,
  minCanvasWidth,
}: {
  trace: AgentTrace;
  graph: GraphStructure | null;
  hasGraph: boolean;
  activeGraphNode: string | null;
  graphActivity: GraphActivity;
  maxGraphHeight: number;
  minCanvasWidth: number;
}) {
  return (
    <div className="rounded-xl border border-slate-100 bg-white p-3 shadow-sm">
      <div className="mb-2 flex items-center gap-2">
        <Network className="h-4 w-4 text-slate-500" />
        <span className="text-[13px] font-semibold text-slate-800">编译图检查器</span>
      </div>
      {hasGraph && graph ? (
        <>
          <p className="mb-2 rounded-lg bg-slate-50 px-3 py-2 text-[11px] leading-relaxed text-slate-500">
            优先使用 LangGraph xray Mermaid PNG 渲染；它表示编译后的可能执行边，不是本次真实执行顺序。
          </p>
          {graph.mermaid_png_data_url ? (
            <div
              className="overflow-auto rounded-lg border border-slate-100 bg-white p-3"
              style={{ maxHeight: maxGraphHeight }}
            >
              <img
                src={graph.mermaid_png_data_url}
                alt="LangGraph xray compiled graph"
                className="block max-w-none"
              />
            </div>
          ) : (
            <>
              <div className="mb-2 rounded-lg border border-amber-100 bg-amber-50 px-3 py-2 text-[11px] leading-relaxed text-amber-700">
                当前后端没有生成 Mermaid PNG，通常是 mermaid.ink 不可用或本地渲染器未安装。下面显示前端兜底图。
              </div>
              <GraphSvg
                graph={graph}
                activeNode={trace.status === "running" ? activeGraphNode : null}
                graphActivity={graphActivity}
                maxGraphHeight={maxGraphHeight}
                minCanvasWidth={minCanvasWidth}
              />
              {graph.mermaid && (
                <details className="mt-2 rounded-lg border border-slate-100 bg-slate-50 p-2">
                  <summary className="cursor-pointer text-[11px] font-medium text-slate-500">
                    Mermaid 源码
                  </summary>
                  <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-[10px] leading-relaxed text-slate-500">
                    {graph.mermaid}
                  </pre>
                </details>
              )}
            </>
          )}
        </>
      ) : (
        <div className="rounded-lg border border-dashed border-slate-200 bg-slate-50 px-3 py-4 text-[12px] leading-relaxed text-slate-500">
          当前 session 没有持久化的 LangGraph 编译图。重新发起一次 Agent 请求后，后端会把
          graph_structure 写入 session，之后刷新页面也能看到这个检查器。
        </div>
      )}
    </div>
  );
}

function EmptyTraceBlock({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="rounded-xl border border-dashed border-slate-200 bg-white px-4 py-8 text-center shadow-sm">
      <Cpu className="mx-auto mb-2 h-5 w-5 text-slate-300" />
      <p className="text-[13px] font-semibold text-slate-500">{title}</p>
      <p className="mt-1 text-[11px] text-slate-400">{detail}</p>
    </div>
  );
}

function TraceRunPanel({
  view,
  onViewChange,
  trace,
  flowItems,
  expandedFlow,
  onToggleFlow,
  visibleRootSpans,
  spansByParent,
  expandedTree,
  onToggleTree,
  selectedType,
  selectedSpan,
  selectedSpanId,
  onSelect,
  onCloseDetail,
  onClearFilter,
}: {
  view: "flow" | "tree";
  onViewChange: (view: "flow" | "tree") => void;
  trace: AgentTrace;
  flowItems: ActualFlowItem[];
  expandedFlow: Set<string>;
  onToggleFlow: (id: string) => void;
  visibleRootSpans: TraceSpan[];
  spansByParent: Map<string | null, TraceSpan[]>;
  expandedTree: Set<string>;
  onToggleTree: (id: string) => void;
  selectedType: TraceSpan["type"] | null;
  selectedSpan: TraceSpan | null;
  selectedSpanId: string | null;
  onSelect: (span: TraceSpan) => void;
  onCloseDetail: () => void;
  onClearFilter: () => void;
}) {
  return (
    <div className="rounded-xl border border-slate-100 bg-white p-3 shadow-sm">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Route className="h-4 w-4 text-slate-500" />
          <span className="text-[13px] font-semibold text-slate-800">本次实际流程</span>
          <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-500">
            {trace.spans.length} span
          </span>
          <span className="text-[11px] font-medium text-slate-500">
            {trace.status === "running" ? "运行中" : trace.status === "error" ? "异常" : "已完成"}
          </span>
          {selectedType && (
            <button
              type="button"
              onClick={onClearFilter}
              className="rounded-full border border-blue-100 bg-blue-50 px-2 py-0.5 text-[10px] font-medium text-blue-600 hover:bg-blue-100"
            >
              只看 {typeLabel(selectedType)} · 清除
            </button>
          )}
        </div>
        <ViewSwitch view={view} onChange={onViewChange} />
      </div>

      <div className="grid min-h-[360px] gap-3 xl:h-[clamp(460px,62vh,760px)] xl:grid-cols-[minmax(260px,360px)_minmax(0,1fr)]">
        <div className="min-h-0 min-w-0 overflow-auto rounded-xl border border-slate-100 bg-slate-50/40 p-2">
          {view === "flow" ? (
            <ActualFlow
              items={flowItems}
              expanded={expandedFlow}
              onToggle={onToggleFlow}
              selectedSpanId={selectedSpanId}
              onSelect={onSelect}
            />
          ) : (
            <RawRunTree
              trace={trace}
              visibleRootSpans={visibleRootSpans}
              spansByParent={spansByParent}
              expanded={expandedTree}
              toggle={onToggleTree}
              selectedType={selectedType}
              selectedSpanId={selectedSpanId}
              onSelect={onSelect}
              onClearFilter={onClearFilter}
            />
          )}
        </div>
        <div className="min-h-0 min-w-0 overflow-auto rounded-xl border border-slate-100 bg-slate-50/30 p-2">
          {selectedSpan ? (
            <SpanDetail span={selectedSpan} allSpans={trace.spans} onClose={onCloseDetail} />
          ) : (
            <div className="flex min-h-[220px] items-center justify-center rounded-xl border border-dashed border-slate-200 bg-slate-50/60 px-4 py-8 text-center">
              <div>
                <Cpu className="mx-auto mb-2 h-5 w-5 text-slate-300" />
                <p className="text-[12px] font-medium text-slate-400">选择左侧节点查看详情</p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function ViewSwitch({
  view,
  onChange,
}: {
  view: "flow" | "tree";
  onChange: (view: "flow" | "tree") => void;
}) {
  return (
    <div className="flex rounded-lg bg-slate-100 p-1">
      {[
        { key: "flow" as const, label: "流程树" },
        { key: "tree" as const, label: "原始树" },
      ].map((item) => (
        <button
          key={item.key}
          type="button"
          onClick={() => onChange(item.key)}
          className={`rounded-md px-3 py-1.5 text-[12px] font-semibold transition-colors ${
            view === item.key
              ? "bg-white text-slate-900 shadow-sm"
              : "text-slate-500 hover:text-slate-800"
          }`}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

function RawRunTree({
  trace,
  visibleRootSpans,
  spansByParent,
  expanded,
  toggle,
  selectedType,
  selectedSpanId,
  onSelect,
  onClearFilter,
}: {
  trace: AgentTrace;
  visibleRootSpans: TraceSpan[];
  spansByParent: Map<string | null, TraceSpan[]>;
  expanded: Set<string>;
  toggle: (id: string) => void;
  selectedType: TraceSpan["type"] | null;
  selectedSpanId: string | null;
  onSelect: (span: TraceSpan) => void;
  onClearFilter: () => void;
}) {
  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[11px] font-medium text-slate-500">
          原始 agent.run 树{selectedType ? ` · 只看 ${typeLabel(selectedType)}` : ""}
        </span>
        <div className="flex items-center gap-2">
          {selectedType && (
            <button
              type="button"
              onClick={onClearFilter}
              className="rounded-full border border-slate-200 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-50"
            >
              清除过滤
            </button>
          )}
          <span className="text-[11px] text-slate-400">
            {trace.status === "running" ? "运行中" : trace.status === "error" ? "异常" : "已完成"}
          </span>
        </div>
      </div>
      <div className="space-y-1">
        {visibleRootSpans.map((span) => (
          <SpanNode
            key={span.id}
            span={span}
            depth={0}
            spansByParent={spansByParent}
            expanded={expanded}
            toggle={toggle}
            selectedType={selectedType}
            selectedSpanId={selectedSpanId}
            onSelect={onSelect}
          />
        ))}
      </div>
    </div>
  );
}

function SpanNode({
  span,
  depth,
  spansByParent,
  expanded,
  toggle,
  selectedType,
  selectedSpanId,
  onSelect,
}: {
  span: TraceSpan;
  depth: number;
  spansByParent: Map<string | null, TraceSpan[]>;
  expanded: Set<string>;
  toggle: (id: string) => void;
  selectedType: TraceSpan["type"] | null;
  selectedSpanId: string | null;
  onSelect: (span: TraceSpan) => void;
}) {
  const allChildren = spansByParent.get(span.id) || [];
  const children = selectedType
    ? allChildren.filter((child) => subtreeHasType(child, spansByParent, selectedType))
    : allChildren;
  const hasChildren = children.length > 0;
  const isOpen = expanded.has(span.id);
  const isSelected = span.id === selectedSpanId;
  const duration =
    span.completed_at && span.started_at
      ? Math.max(0, span.completed_at - span.started_at)
      : null;

  return (
    <div className="select-text">
      <button
        type="button"
        onClick={() => {
          onSelect(span);
          if (hasChildren) toggle(span.id);
        }}
        className={`flex w-full items-start gap-2 rounded-lg px-2 py-1.5 text-left hover:bg-black/[0.03] ${
          isSelected ? "bg-blue-50 ring-1 ring-blue-100" : ""
        }`}
        style={{ paddingLeft: `${8 + depth * 16}px` }}
      >
        <SpanIcon type={span.type} status={span.status} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-[13px] font-medium text-slate-800">
              {spanLabel(span)}
            </span>
            <TypePill span={span} />
            {duration !== null && (
              <span className="whitespace-nowrap text-[10px] text-slate-400">
                {formatDuration(duration)}
              </span>
            )}
          </div>
          {span.output !== null && span.output !== undefined && (
            <p className="mt-0.5 line-clamp-2 text-[11px] text-slate-500">
              {spanOutputPreview(span)}
            </p>
          )}
        </div>
        {hasChildren ? (
          isOpen ? (
            <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400" />
          ) : (
            <ChevronRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400" />
          )
        ) : null}
      </button>

      {isOpen &&
        children.map((child) => (
          <SpanNode
            key={child.id}
            span={child}
            depth={depth + 1}
            spansByParent={spansByParent}
            expanded={expanded}
            toggle={toggle}
            selectedType={selectedType}
            selectedSpanId={selectedSpanId}
            onSelect={onSelect}
          />
        ))}
    </div>
  );
}

function subtreeHasType(
  span: TraceSpan,
  spansByParent: Map<string | null, TraceSpan[]>,
  type: TraceSpan["type"]
): boolean {
  if (span.type === type) return true;
  return (spansByParent.get(span.id) || []).some((child) => subtreeHasType(child, spansByParent, type));
}

function SpanIcon({
  type,
  status,
}: {
  type: TraceSpan["type"];
  status: TraceSpan["status"];
}) {
  const className = "mt-0.5 h-4 w-4 shrink-0";
  const color =
    status === "error" ? "text-red-500" : status === "running" ? "text-blue-500" : "text-slate-500";

  if (status === "error") {
    return <XCircle className={`${className} ${color}`} />;
  }
  if (status === "running") {
    return <Circle className={`${className} ${color} animate-pulse`} />;
  }

  switch (type) {
    case "llm":
      return <MessageSquare className={`${className} text-[#002fa7]`} />;
    case "model_input":
      return <Cpu className={`${className} text-blue-600`} />;
    case "tool":
      return <Cpu className={`${className} text-slate-600`} />;
    case "middleware":
      return <Route className={`${className} text-violet-600`} />;
    case "memory":
      return <Database className={`${className} text-emerald-600`} />;
    case "skill":
      return <PlugZap className={`${className} text-indigo-600`} />;
    case "subagent":
      return <Split className={`${className} text-fuchsia-600`} />;
    case "graph":
      return <Network className={`${className} text-sky-600`} />;
    case "reasoning":
      return <FileText className={`${className} text-amber-600`} />;
    case "todo":
      return <ListChecks className={`${className} text-emerald-600`} />;
    case "permission":
      return <KeyRound className={`${className} text-rose-600`} />;
    case "rag":
      return <Network className={`${className} text-emerald-600`} />;
    case "custom":
      return <CheckCircle2 className={`${className} text-slate-500`} />;
    default:
      return <CheckCircle2 className={`${className} text-slate-500`} />;
  }
}

function TypePill({ span }: { span: TraceSpan }) {
  const labelByType: Partial<Record<TraceSpan["type"], string>> = {
    graph: "graph",
    middleware: "middleware",
    memory: "memory",
    model_input: "model input",
    skill: "skill",
    subagent: "subagent",
    tool: "tool",
    rag: "rag",
    permission: "permission",
  };
  const label = labelByType[span.type];
  if (!label) return null;
  return (
    <span className="shrink-0 rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-normal text-slate-500">
      {label}
    </span>
  );
}

interface HarnessSummaryItem {
  key: string;
  type: TraceSpan["type"];
  label: string;
  value: string;
  detail: string;
  tone: "blue" | "slate" | "green" | "indigo" | "fuchsia" | "amber";
}

function HarnessSummary({
  summary,
  selectedType,
  onSelect,
}: {
  summary: HarnessSummaryItem[];
  selectedType: TraceSpan["type"] | null;
  onSelect: (type: TraceSpan["type"]) => void;
}) {
  return (
    <div className="rounded-xl border border-slate-100 bg-white p-3 shadow-sm">
      <div className="mb-2 flex items-center gap-2">
        <Route className="h-4 w-4 text-slate-500" />
        <span className="text-[13px] font-semibold text-slate-800">Trace 关键点</span>
      </div>
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
        {summary.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => onSelect(item.type)}
            className={`rounded-lg border px-3 py-2 text-left transition-transform hover:-translate-y-0.5 ${
              selectedType === item.type ? "ring-2 ring-blue-200" : ""
            } ${summaryToneClass(item.tone)}`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-[11px] font-medium">{item.label}</span>
              <span className="text-[13px] font-bold">{item.value}</span>
            </div>
            <p className="mt-1 truncate text-[10px] opacity-75">{item.detail}</p>
          </button>
        ))}
      </div>
    </div>
  );
}

interface MiddlewareEffectGroup {
  key: string;
  title: string;
  description: string;
  middleware: TraceRuntimeMiddlewareEntry[];
  evidence: string[];
  effects: TraceMiddlewareEffect[];
  tone: string;
}

function RuntimeMountPanel({ inventory }: { inventory: TraceRuntimeInventory }) {
  const [isOpen, setIsOpen] = useState(false);
  const tools = inventory.tools || [];
  const skills = inventory.skills || [];
  const subagents = inventory.subagents || [];
  const packageVersions = inventory.package_versions || {};
  const stack = inventory.middleware?.stack || [];
  const middlewareHookGroups = useMemo(() => buildMountedMiddlewareHookGroups(inventory), [inventory]);

  return (
    <div className="rounded-xl border border-slate-100 bg-white p-3 shadow-sm">
      <button
        type="button"
        onClick={() => setIsOpen((value) => !value)}
        className="flex w-full flex-wrap items-center justify-between gap-2 text-left"
      >
        <div className="flex items-center gap-2">
          <PlugZap className="h-4 w-4 text-slate-500" />
          <span className="text-[13px] font-semibold text-slate-800">运行挂载清单</span>
          <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-500">
            本次请求快照
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-1 text-[10px] text-slate-400">
          <span>{stack.length} middleware</span>
          <span>·</span>
          <span>{tools.length} tools</span>
          <span>·</span>
          <span>{skills.length} skills</span>
          <span>·</span>
          <span>{subagents.length} subagents</span>
          {isOpen ? (
            <ChevronDown className="h-4 w-4 text-slate-400" />
          ) : (
            <ChevronRight className="h-4 w-4 text-slate-400" />
          )}
        </div>
      </button>

      {isOpen && (
        <div className="mt-3 grid gap-3 xl:grid-cols-4">
          <MountedMiddlewareHookCard
            groups={middlewareHookGroups}
            stackCount={stack.length}
          />

          <MountedListCard
            icon={<Cpu className="h-4 w-4 text-slate-500" />}
            title="Tools"
            subtitle="包含 DeepAgents 内置工具与 PuddingClaw 工具"
            empty="没有工具挂载"
            items={tools.map((tool) => ({
              key: tool.name,
              title: tool.name,
              subtitle: tool.description || tool.source || "",
              badge: tool.source || "tool",
            }))}
          />

          <MountedListCard
            icon={<PlugZap className="h-4 w-4 text-indigo-500" />}
            title="Skills"
            subtitle="用于确认 skills snapshot 已进入 system prompt"
            empty="没有 skill snapshot"
            items={skills.map((skill) => ({
              key: skill.name,
              title: skill.name,
              subtitle: skill.description || skill.location || "",
              badge: skill.in_system_prompt ? "system prompt" : "skill",
              href: `/skills?skill=${encodeURIComponent(skill.name)}`,
            }))}
          />

          <MountedListCard
            icon={<Split className="h-4 w-4 text-fuchsia-500" />}
            title="SubAgents"
            subtitle="设置页声明的子代理，可委派给 task 工具"
            empty="没有 SubAgent 配置"
            items={subagents.map((subagent) => ({
              key: subagent.name,
              title: subagent.name,
              subtitle: compactEvidence([
                subagent.description || "",
                subagent.route_trigger ? `trigger: ${subagent.route_trigger}` : "",
                subagent.model ? `model: ${subagent.model}` : "",
              ]).join(" · "),
              badge: subagent.enabled ? "enabled" : "disabled",
              href:
                subagent.href ||
                `/settings?category=harness&tab=subagent&subagent=${encodeURIComponent(subagent.name)}`,
            }))}
          />
          {Object.keys(packageVersions).length > 0 && (
            <div className="xl:col-span-4 rounded-lg border border-slate-100 bg-slate-50 px-3 py-2">
              <div className="mb-1 text-[10px] font-semibold uppercase text-slate-400">Runtime Packages</div>
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(packageVersions).map(([name, packageVersion]) => (
                  <span key={name} className="rounded-md bg-white px-2 py-1 text-[10px] font-medium text-slate-500">
                    {name}: {packageVersion}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

interface MountedMiddlewareHookGroup {
  hook: MiddlewareHookName;
  description: string;
  middleware: TraceRuntimeMiddlewareEntry[];
}

function buildMountedMiddlewareHookGroups(inventory: TraceRuntimeInventory): MountedMiddlewareHookGroup[] {
  const stack = inventory.middleware?.stack || [];
  const inventoryHooks = inventory.middleware?.hooks || {};
  return MIDDLEWARE_HOOK_SPECS.map((spec) => {
    const middleware = uniqueMiddlewareEntries([
      ...(inventoryHooks[spec.hook] || []),
      ...stack.filter((entry) => middlewareHooks(entry).includes(spec.hook)),
    ]).sort((a, b) => Number(a.stack_order || a.order || 0) - Number(b.stack_order || b.order || 0));
    return {
      hook: spec.hook,
      description: spec.description,
      middleware,
    };
  }).filter((group) => group.middleware.length > 0);
}

function MiddlewareTracePanel({
  inventory,
  trace,
}: {
  inventory: TraceRuntimeInventory;
  trace: AgentTrace;
}) {
  const [selectedHook, setSelectedHook] = useState<MiddlewareHookName>("before_agent");
  const [selectedInvocationByHook, setSelectedInvocationByHook] = useState<Record<string, number>>({});
  const [flowPreviewInvocation, setFlowPreviewInvocation] = useState<MiddlewareHookInvocation | null>(null);
  const hookGroups = useMemo(() => buildMiddlewareHookGroups(inventory, trace), [inventory, trace]);
  const stack = inventory.middleware?.stack || [];
  const currentGroup = hookGroups.find((group) => group.hook === selectedHook) || hookGroups[0];
  const selectedInvocationIndex = Math.min(
    selectedInvocationByHook[currentGroup.hook] || 0,
    Math.max(currentGroup.invocations.length - 1, 0)
  );
  const currentInvocation = currentGroup.invocations[selectedInvocationIndex] || null;
  const currentEffects = currentInvocation ? currentInvocation.effects : currentGroup.effects;
  const selectedInvocationId = currentInvocation?.invocation?.id || "";
  const hookBoundarySnapshots = (trace.hook_boundary_snapshots || [])
    .filter((snapshot) => normalizeHookName(snapshot.hook) === currentGroup.hook)
    .sort((a, b) => Number(a.sequence || 0) - Number(b.sequence || 0));
  const currentBoundarySnapshots = selectedInvocationId
    ? hookBoundarySnapshots.filter((snapshot) => snapshot.metadata?.middleware_invocation_id === selectedInvocationId)
    : hookBoundarySnapshots;
  const currentEvidence = currentInvocation?.evidence || [];
  const currentEvidenceItems = dedupeStrings([
    ...currentEvidence,
    ...currentEffects.flatMap((effect) => effect.evidence || []),
  ]).slice(0, 10);

  const selectHook = (hook: MiddlewareHookName) => {
    setSelectedHook(hook);
  };

  const selectInvocation = (index: number) => {
    setSelectedInvocationByHook((prev) => ({ ...prev, [currentGroup.hook]: index }));
  };

  return (
    <div className="rounded-xl border border-slate-100 bg-white p-3 shadow-sm">
      <div className="mb-3 flex w-full flex-wrap items-start justify-between gap-2">
        <div className="flex items-center gap-2">
          <Route className="h-4 w-4 text-violet-500" />
          <div>
            <span className="text-[13px] font-semibold text-slate-800">六大 Hook 中间件视图</span>
            <p className="mt-0.5 text-[10px] text-slate-400">
              badge 表示本次 query 触发次数；middleware 数量只作为辅助信息
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-1 text-[10px] text-slate-400">
          <span>{stack.length} middleware</span>
          <span>·</span>
          <span>{trace.middleware_effects?.length || 0} effects</span>
        </div>
      </div>

      <div className="grid gap-3 xl:grid-cols-[minmax(220px,280px)_minmax(0,1fr)_minmax(240px,320px)]">
        <div className="space-y-2">
          {hookGroups.map((group) => {
            const active = group.hook === currentGroup.hook;
            return (
              <button
                key={group.hook}
                type="button"
                onClick={() => selectHook(group.hook)}
                className={`w-full rounded-2xl border p-3 text-left transition-colors ${
                  active
                    ? "border-blue-100 bg-blue-50/70 text-slate-900 shadow-sm"
                    : "border-slate-100 bg-slate-50/60 text-slate-600 hover:border-slate-200 hover:bg-white"
                }`}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="truncate text-[13px] font-bold">{group.hook}</p>
                    <p className="mt-1 line-clamp-2 text-[10px] leading-relaxed text-slate-500">
                      {group.description}
                    </p>
                  </div>
                  <span className="shrink-0 rounded-full bg-white px-2.5 py-1 text-[11px] font-extrabold text-blue-700 shadow-sm">
                    {group.invocations.length}×
                  </span>
                </div>
                <div className="mt-2 flex flex-wrap gap-1">
                  <span className="rounded-full bg-white/80 px-2 py-0.5 text-[9px] font-semibold text-slate-500">
                    {group.middleware.length} middleware
                  </span>
                  <span className="rounded-full bg-white/80 px-2 py-0.5 text-[9px] font-semibold text-slate-500">
                    {group.rule}
                  </span>
                </div>
              </button>
            );
          })}
        </div>

        <div className="min-w-0 space-y-3">
          <div className="rounded-2xl border border-slate-100 bg-slate-50/50 p-3">
            <div className="flex flex-wrap items-center gap-2">
              {(currentInvocation?.flow || [currentGroup.hook]).map((step, index, items) => (
                <React.Fragment key={`${step}-${index}`}>
                  <span
                    className={`rounded-full border px-3 py-1 text-[11px] font-semibold ${
                      step === currentGroup.hook
                        ? "border-blue-200 bg-blue-600 text-white shadow-sm"
                        : "border-slate-100 bg-white text-slate-500"
                    }`}
                  >
                    {step}
                  </span>
                  {index < items.length - 1 && <span className="text-[11px] font-bold text-slate-300">→</span>}
                </React.Fragment>
              ))}
            </div>
            <div className="mt-3 flex gap-2 overflow-x-auto pb-1">
              {currentGroup.invocations.length ? (
                currentGroup.invocations.map((invocation, index) => (
                  <button
                    key={invocation.id}
                    type="button"
                    onClick={() => selectInvocation(index)}
                    className={`min-w-[158px] rounded-xl border px-3 py-2 text-left transition-colors ${
                      index === selectedInvocationIndex
                        ? "border-blue-200 bg-white text-slate-900 shadow-sm"
                        : "border-transparent bg-white/60 text-slate-500 hover:border-slate-100"
                    }`}
                  >
                    <p className="text-[10px] font-extrabold text-blue-700">
                      #{index + 1} · {invocation.sequenceLabel}
                    </p>
                    <p className="mt-1 truncate text-[11px] font-bold">{invocation.title}</p>
                    <p className="mt-0.5 truncate text-[9px] text-slate-400">{invocation.note}</p>
                  </button>
                ))
              ) : (
                <div className="rounded-xl border border-dashed border-slate-200 bg-white px-3 py-2 text-[11px] text-slate-400">
                  本轮 trace 暂未记录到该 hook 的触发；只展示运行时挂载清单。
                </div>
              )}
            </div>
          </div>

          <div className="rounded-2xl border border-slate-100 bg-white p-3">
            <div className="mb-3 flex items-start justify-between gap-2">
              <div>
                <p className="text-[13px] font-bold text-slate-900">
                  {currentGroup.hook}
                  {currentInvocation ? ` · #${selectedInvocationIndex + 1}` : ""}
                </p>
                <p className="mt-0.5 text-[10px] text-slate-400">
                  {currentGroup.middleware.length} middleware · {currentGroup.invocations.length} invocations · {currentGroup.rule}
                </p>
              </div>
              <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-slate-500">
                不伪造 diff
              </span>
            </div>

            <div className="grid gap-2">
              {currentGroup.middleware.length ? (
                currentGroup.middleware.map((entry) => {
                  const effects = effectsForMiddleware(currentEffects, entry);
                  const changedEffects = effects.filter((effect) => effectRepresentsMiddlewareChange(effect, entry));
                  const observedOnlyEffects = effects.filter((effect) => !effectRepresentsMiddlewareChange(effect, entry));
                  const participatesInCurrentInvocation = currentInvocation
                    ? invocationIncludesMiddleware(currentInvocation, entry)
                    : false;
                  const directInvocation = participatesInCurrentInvocation ? currentInvocation?.invocation : undefined;
                  const directDiff = directInvocation?.diff || {};
                  const directHasFactDiff = hasFactDiffSummary(directDiff);
                  const status = changedEffects.length ? "changed" : effects.length || participatesInCurrentInvocation ? "read" : "noop";
                  return (
                    <div key={`${currentGroup.hook}-${entry.name}-${entry.stack_order || entry.order || ""}`} className="rounded-xl border border-slate-100 bg-slate-50/40 p-3">
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <p className="truncate text-[12px] font-bold text-slate-800">{entry.name}</p>
                          <p className="mt-0.5 text-[10px] text-slate-400">
                            {formatMiddlewareSource(entry.source)} · {middlewareOrderLabel(entry)}
                          </p>
                        </div>
                        <span className={`rounded-full px-2 py-0.5 text-[9px] font-bold ${middlewareStatusClass(status)}`}>
                          {middlewareStatusLabel(status)}
                        </span>
                      </div>
                      {changedEffects.length > 0 ? (
                        <div className="mt-2 grid gap-2">
                          {changedEffects.slice(0, 2).map((effect) => (
                            <MiddlewareEffectEvidence key={effect.id} effect={effect} />
                          ))}
                        </div>
                      ) : directHasFactDiff ? (
                        <div className="mt-2 rounded-lg border border-blue-100 bg-white p-2">
                          <p className="mb-2 text-[10px] font-bold text-slate-700">事实变化</p>
                          <FactDiffSummary
                            diff={directDiff}
                            before={directInvocation?.before}
                            after={directInvocation?.after}
                          />
                        </div>
                      ) : observedOnlyEffects.length > 0 ? (
                        <div className="mt-2 rounded-lg border border-dashed border-slate-200 bg-white px-3 py-2 text-[10px] leading-relaxed text-slate-400">
                          <p className="font-medium text-slate-500">
                            已进入 {currentGroup.hook} 检查链，但当前步骤没有触发该 middleware 的实际改写。
                          </p>
                          {entry.name === "SummarizationMiddleware" && (
                            <p className="mt-1">
                              本轮只捕获到模型输入边界；没有检测到 summary 产物或压缩 diff，因此不能算作摘要触发。
                            </p>
                          )}
                          <div className="mt-2 flex flex-wrap gap-1">
                            {observedOnlyEffects
                              .flatMap((effect) => effect.evidence || [])
                              .slice(0, 3)
                              .map((item, index) => (
                                <span key={`${item}-${index}`} className="rounded-full bg-slate-50 px-2 py-0.5 text-[9px] text-slate-500">
                                  {item}
                                </span>
                              ))}
                          </div>
                        </div>
                      ) : (
                        <p className="mt-2 rounded-lg border border-dashed border-slate-200 bg-white px-3 py-2 text-[10px] leading-relaxed text-slate-400">
                          {participatesInCurrentInvocation
                            ? "当前步骤记录到该 middleware 参与，但没有 before / after 快照，因此只展示挂载与触发证据。"
                            : "当前选中的 invocation 尚未触达该 middleware；切换到对应步骤查看它的 diff。"}
                        </p>
                      )}
                    </div>
                  );
                })
              ) : (
                <p className="rounded-xl border border-dashed border-slate-200 bg-slate-50 px-3 py-6 text-center text-[12px] text-slate-400">
                  当前运行时没有 middleware 声明会触达 {currentGroup.hook}。
                </p>
              )}
            </div>
          </div>
        </div>

        <div className="min-w-0 space-y-3">
          <div className="rounded-2xl border border-slate-100 bg-white p-3">
            <p className="text-[13px] font-bold text-slate-900">当前触发摘要</p>
            <div className="mt-3 space-y-2">
              <SummaryRow
                label="当前触发"
                value={currentInvocation?.title || "本轮暂无记录"}
                onClick={currentInvocation ? () => setFlowPreviewInvocation(currentInvocation) : undefined}
              />
              <SummaryRow label="流程序号" value={currentInvocation?.sequenceLabel || "—"} />
              <SummaryRow
                label="上一节点"
                value={currentInvocation?.previous || "—"}
                onClick={currentInvocation ? () => setFlowPreviewInvocation(currentInvocation) : undefined}
              />
              <SummaryRow
                label="下一节点"
                value={currentInvocation?.next || "—"}
                onClick={currentInvocation ? () => setFlowPreviewInvocation(currentInvocation) : undefined}
              />
              <SummaryRow label="触发原因" value={currentInvocation?.reason || "没有可用 trace 证据。"} />
            </div>
          </div>

          <HookBoundarySnapshotPanel snapshots={currentBoundarySnapshots} />

          <div className="rounded-2xl border border-slate-100 bg-white p-3">
            <div className="mb-2 flex items-center justify-between gap-2">
              <p className="text-[13px] font-bold text-slate-900">实际执行证据</p>
              <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-slate-500">
                {currentEvidenceItems.length}
              </span>
            </div>
            <div className="space-y-2">
              {currentEvidenceItems.length ? (
                currentEvidenceItems.map((item, index) => (
                  <div key={`${item}-${index}`} className="flex gap-2 rounded-xl bg-slate-50 px-3 py-2 text-[10px] leading-relaxed text-slate-500">
                    <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-white text-[9px] font-bold text-blue-700 shadow-sm">
                      {index + 1}
                    </span>
                    <span>{item}</span>
                  </div>
                ))
              ) : (
                <p className="rounded-xl border border-dashed border-slate-200 bg-slate-50 px-3 py-4 text-[11px] leading-relaxed text-slate-400">
                  当前 query 没有该 hook 的执行证据。重新发起 Agent 请求后，如果后端记录到 hook 或 effect，会在这里出现。
                </p>
              )}
            </div>
          </div>

          <div className="rounded-2xl border border-amber-100 bg-amber-50/60 p-3">
            <p className="text-[12px] font-bold text-amber-900">实现边界</p>
            <p className="mt-1 text-[10px] leading-relaxed text-amber-700">
              PuddingClaw 传入的 middleware 已通过 proxy 记录直接 before/after；DeepAgents 自动注入的 base middleware 暂时仍以 observed / inferred 证据呈现。
            </p>
          </div>
        </div>
      </div>

      {flowPreviewInvocation && (
        <MiddlewareFlowPreviewModal
          trace={trace}
          invocation={flowPreviewInvocation}
          onClose={() => setFlowPreviewInvocation(null)}
        />
      )}
    </div>
  );
}

type MiddlewareHookName =
  | "before_agent"
  | "before_model"
  | "wrap_model_call"
  | "after_model"
  | "wrap_tool_call"
  | "after_agent";

type MiddlewareStatus = "changed" | "read" | "noop";

interface MiddlewareHookSpec {
  hook: MiddlewareHookName;
  description: string;
  rule: string;
}

interface MiddlewareHookGroup extends MiddlewareHookSpec {
  middleware: TraceRuntimeMiddlewareEntry[];
  invocations: MiddlewareHookInvocation[];
  effects: TraceMiddlewareEffect[];
}

interface MiddlewareHookInvocation {
  id: string;
  hook: MiddlewareHookName;
  sequence: number;
  sequenceLabel: string;
  title: string;
  note: string;
  previous: string;
  next: string;
  reason: string;
  flow: string[];
  spans: TraceSpan[];
  effects: TraceMiddlewareEffect[];
  invocation?: TraceMiddlewareInvocation;
  evidence: string[];
}

const MIDDLEWARE_HOOK_SPECS: MiddlewareHookSpec[] = [
  { hook: "before_agent", description: "Agent 启动前，准备全局状态。", rule: "正序执行" },
  { hook: "before_model", description: "每次模型调用前，整理上下文窗口。", rule: "正序执行" },
  { hook: "wrap_model_call", description: "包住真实 LLM 调用，改写 request。", rule: "洋葱式包裹" },
  { hook: "after_model", description: "模型返回后，修正输出与同步状态。", rule: "反序执行" },
  { hook: "wrap_tool_call", description: "包住工具调用，处理文件系统与子代理。", rule: "洋葱式包裹" },
  { hook: "after_agent", description: "Agent 结束后，收尾和落盘。", rule: "反序执行" },
];

function SummaryRow({
  label,
  value,
  onClick,
}: {
  label: string;
  value: string;
  onClick?: () => void;
}) {
  return (
    <div className="grid grid-cols-[64px_minmax(0,1fr)] gap-2 text-[11px] leading-relaxed">
      <span className="font-semibold text-slate-400">{label}</span>
      {onClick ? (
        <button
          type="button"
          onClick={onClick}
          className="min-w-0 text-left font-medium text-slate-600 underline decoration-slate-300 underline-offset-4 transition-colors hover:text-blue-700 hover:decoration-blue-300"
          title="查看局部流程"
        >
          {value}
        </button>
      ) : (
        <span className="min-w-0 text-slate-600">{value}</span>
      )}
    </div>
  );
}

function HookBoundarySnapshotPanel({ snapshots }: { snapshots: TraceHookBoundarySnapshot[] }) {
  const hasDirectSnapshots = snapshots.some((snapshot) => snapshot.metadata?.coverage === "direct");
  return (
    <div className="rounded-2xl border border-blue-100 bg-blue-50/45 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div>
          <p className="text-[13px] font-bold text-slate-900">Hook Boundary Snapshot</p>
          <p className="mt-0.5 text-[10px] text-slate-500">
            {hasDirectSnapshots ? "direct 表示 proxy 采集到单个 middleware 前后对比" : "边界事实，不等同于单个 middleware 归因"}
          </p>
        </div>
        <span className="rounded-full bg-white px-2 py-0.5 text-[10px] font-semibold text-blue-600">
          {snapshots.length}
        </span>
      </div>
      {snapshots.length ? (
        <div className="space-y-2">
          {snapshots.map((snapshot) => {
            const payload = (snapshot.snapshot || {}) as {
              fingerprints?: ModelCallFingerprints;
              contract?: { fingerprints?: ModelCallFingerprints };
              messages_hash?: string;
              system_prompt_hash?: string;
              tool_schema_hash?: string;
              message_count?: number;
              system_prompt_chars?: number;
              tool_schema_count?: number;
              estimated_tokens?: number;
              payload_kind?: string;
              state_field_count?: number;
              state_fields?: Record<string, unknown>;
            };
            const fingerprints = {
              ...(payload.fingerprints || {}),
              ...(payload.contract?.fingerprints || {}),
              ...(payload.messages_hash ? { messages_hash: payload.messages_hash } : {}),
              ...(payload.system_prompt_hash ? { system_prompt_hash: payload.system_prompt_hash } : {}),
              ...(payload.tool_schema_hash ? { tool_schema_hash: payload.tool_schema_hash } : {}),
            };
            const coverage = snapshot.metadata?.coverage ? String(snapshot.metadata.coverage) : "";
            return (
              <div key={snapshot.id} className="rounded-xl border border-blue-100 bg-white p-2.5">
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <span className="rounded-md bg-blue-600 px-2 py-0.5 text-[10px] font-bold text-white">
                    {snapshot.title}
                  </span>
                  <span className="rounded-md bg-blue-50 px-2 py-0.5 text-[10px] font-semibold text-blue-600">
                    phase: {snapshot.phase}
                  </span>
                  {coverage && (
                    <span className="rounded-md bg-violet-50 px-2 py-0.5 text-[10px] font-semibold text-violet-600">
                      {coverage}
                    </span>
                  )}
                  {typeof snapshot.metadata?.model_call_index === "number" && (
                    <span className="rounded-md bg-slate-50 px-2 py-0.5 text-[10px] text-slate-500">
                      model #{Number(snapshot.metadata.model_call_index) + 1}
                    </span>
                  )}
                  {payload.payload_kind && (
                    <span className="rounded-md bg-slate-50 px-2 py-0.5 text-[10px] text-slate-500">
                      {payload.payload_kind}
                    </span>
                  )}
                </div>
                <div className="grid gap-1.5 sm:grid-cols-3">
                  <SnapshotMetric
                    label="Messages"
                    value={typeof payload.message_count === "number" ? String(payload.message_count) : "-"}
                    sub={typeof payload.estimated_tokens === "number" ? `~${payload.estimated_tokens} tokens` : fingerprints.messages_hash}
                  />
                  <SnapshotMetric
                    label="System"
                    value={typeof payload.system_prompt_chars === "number" ? `${payload.system_prompt_chars} chars` : "-"}
                    sub={fingerprints.system_prompt_hash}
                  />
                  <SnapshotMetric
                    label="Tools"
                    value={typeof payload.tool_schema_count === "number" ? String(payload.tool_schema_count) : "-"}
                    sub={fingerprints.tool_schema_hash}
                  />
                </div>
                <div className="mt-2 grid gap-1.5">
                  {fingerprints.messages_hash && <HashPill label="messages_hash" value={fingerprints.messages_hash} />}
                  {fingerprints.system_prompt_hash && <HashPill label="system_prompt_hash" value={fingerprints.system_prompt_hash} />}
                  {fingerprints.tool_schema_hash && <HashPill label="tool_schema_hash" value={fingerprints.tool_schema_hash} />}
                </div>
                {payload.state_fields && (
                  <div className="mt-2 rounded-lg bg-blue-50/50 px-2 py-1.5">
                    <div className="mb-1 flex items-center justify-between gap-2">
                      <span className="text-[9px] font-bold text-blue-600">State fields</span>
                      <span className="rounded-full bg-white px-1.5 py-0.5 text-[8px] font-semibold text-slate-500">
                        {payload.state_field_count || Object.keys(payload.state_fields).length}
                      </span>
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {Object.entries(payload.state_fields).slice(0, 8).map(([field, value]) => {
                        const summary = recordFromUnknown(value);
                        return (
                          <span key={`${snapshot.id}-${field}`} className="rounded-md border border-blue-100 bg-white px-1.5 py-0.5 text-[8px] font-semibold text-slate-600">
                            {field}
                            {typeof summary.count === "number" && <span className="ml-1 font-normal text-slate-400">({summary.count})</span>}
                          </span>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      ) : (
        <p className="rounded-xl border border-dashed border-blue-100 bg-white/70 px-3 py-4 text-[11px] leading-relaxed text-slate-400">
          当前 hook 还没有边界快照。新请求会在模型输入边界记录 before_model.after 与 wrap_model_call.before。
        </p>
      )}
    </div>
  );
}

function SnapshotMetric({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="min-w-0 rounded-lg border border-blue-100 bg-white px-3 py-2">
      <div className="text-[10px] font-semibold uppercase text-blue-400">{label}</div>
      <div className="mt-0.5 truncate text-[13px] font-bold text-slate-700">{value}</div>
      {sub && <div className="mt-0.5 truncate font-mono text-[9px] text-slate-400">{sub}</div>}
    </div>
  );
}

function HashPill({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex min-w-0 items-center justify-between gap-2 rounded-lg bg-blue-50/60 px-2 py-1 text-[9px]">
      <span className="shrink-0 font-semibold text-blue-500">{label}</span>
      <span className="truncate font-mono text-slate-500">{value}</span>
    </div>
  );
}

function MiddlewareFlowPreviewModal({
  trace,
  invocation,
  onClose,
}: {
  trace: AgentTrace;
  invocation: MiddlewareHookInvocation;
  onClose: () => void;
}) {
  const allSpans = useMemo(() => flattenTraceSpans(trace), [trace]);
  const markerSpan = useMemo(() => middlewareInvocationMarkerSpan(invocation), [invocation]);
  const flowItems = useMemo(
    () => buildActualFlowWithInvocation(allSpans, invocation, markerSpan),
    [allSpans, invocation, markerSpan]
  );
  const initialSpan = markerSpan;
  const [selectedSpan, setSelectedSpan] = useState<TraceSpan | null>(initialSpan);
  const [expanded, setExpanded] = useState<Set<string>>(() =>
    new Set(expandedFlowItemIdsForSpan(flowItems, initialSpan?.id || null))
  );
  const flowScrollRef = useRef<HTMLDivElement | null>(null);
  const selectedSpanId = selectedSpan?.id || initialSpan?.id || null;
  const hasActualFlow = flowItems.length > 0;

  useEffect(() => {
    if (!selectedSpanId) return;
    const frame = window.requestAnimationFrame(() => {
      const target = findFlowNodeElement(flowScrollRef.current, selectedSpanId);
      target?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [selectedSpanId, expanded]);

  const toggle = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/30 px-4 py-6 backdrop-blur-sm">
      <div className="flex h-[88vh] max-h-[920px] w-full max-w-6xl flex-col overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-2xl">
        <div className="shrink-0 flex items-start justify-between gap-3 border-b border-slate-100 px-5 py-4">
          <div className="min-w-0">
            <p className="text-[15px] font-bold text-slate-950">关联流程视图</p>
            <p className="mt-1 text-[11px] text-slate-500">
              复用本次实际流程视图，并插入中间件触发点：{invocation.hook} · {invocation.sequenceLabel}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-slate-100 text-slate-500 transition-colors hover:bg-slate-200 hover:text-slate-900"
            aria-label="关闭流程弹窗"
          >
            <XCircle className="h-4 w-4" />
          </button>
        </div>

        <div className="grid min-h-0 flex-1 grid-rows-[minmax(180px,0.45fr)_minmax(0,1fr)] gap-3 overflow-hidden bg-slate-50/60 p-4 xl:grid-cols-[minmax(320px,420px)_minmax(0,1fr)] xl:grid-rows-none">
          <div
            ref={flowScrollRef}
            className="min-h-0 min-w-0 overflow-auto rounded-2xl border border-slate-100 bg-white p-3 shadow-sm"
          >
            <div className="mb-3 flex items-center justify-between gap-2">
              <div>
                <p className="text-[13px] font-bold text-slate-900">本次实际流程</p>
                <p className="mt-0.5 text-[10px] text-slate-400">和流程视图一致；蓝色卡片是当前中间件触发位置</p>
              </div>
              <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-slate-500">
                {flowItems.length} 节点
              </span>
            </div>
            {hasActualFlow ? (
              <ActualFlow
                items={flowItems}
                expanded={expanded}
                onToggle={toggle}
                selectedSpanId={selectedSpanId}
                onSelect={setSelectedSpan}
              />
            ) : (
              <p className="rounded-xl border border-dashed border-slate-200 bg-slate-50 px-3 py-8 text-center text-[12px] text-slate-400">
                当前 trace 没有可渲染的实际流程节点。
              </p>
            )}
          </div>

          <div className="min-h-0 min-w-0 overflow-auto rounded-2xl border border-slate-100 bg-white p-3 shadow-sm">
            {selectedSpan ? (
              <SpanDetail span={selectedSpan} allSpans={allSpans} onClose={() => setSelectedSpan(initialSpan)} />
            ) : (
              <MiddlewareInvocationFallbackDetail invocation={invocation} />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function MiddlewareInvocationFallbackDetail({ invocation }: { invocation: MiddlewareHookInvocation }) {
  return (
    <div className="space-y-3">
      <div className="rounded-2xl border border-slate-100 bg-white p-4">
        <p className="text-[13px] font-bold text-slate-900">流程上下文</p>
        <div className="mt-3 space-y-2">
          <SummaryRow label="上一节点" value={invocation.previous} />
          <SummaryRow label="当前节点" value={invocation.title} />
          <SummaryRow label="下一节点" value={invocation.next} />
          <SummaryRow label="触发原因" value={invocation.reason} />
        </div>
      </div>

      <div className="rounded-2xl border border-slate-100 bg-white p-4">
        <p className="text-[13px] font-bold text-slate-900">执行证据</p>
        <div className="mt-3 space-y-2">
          {invocation.evidence.length ? (
            invocation.evidence.map((item, index) => (
              <div key={`${invocation.id}-fallback-evidence-${index}`} className="flex gap-2 rounded-xl bg-slate-50 px-3 py-2 text-[10px] leading-relaxed text-slate-500">
                <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-white text-[9px] font-bold text-blue-700 shadow-sm">
                  {index + 1}
                </span>
                <span>{item}</span>
              </div>
            ))
          ) : (
            <p className="rounded-xl border border-dashed border-slate-200 bg-slate-50 px-3 py-4 text-[11px] text-slate-400">
              这次触发来自 middleware effect，暂未关联到具体流程 span。
            </p>
          )}
        </div>
      </div>

      <div className="rounded-2xl border border-slate-100 bg-white p-4">
        <p className="text-[13px] font-bold text-slate-900">关联 Effect</p>
        <div className="mt-3 space-y-2">
          {invocation.effects.length ? (
            invocation.effects.map((effect) => (
              <div key={`${invocation.id}-fallback-effect-${effect.id}`} className="rounded-xl border border-slate-100 bg-slate-50/70 p-3">
                <p className="truncate text-[11px] font-bold text-slate-800">{effect.title}</p>
                <p className="mt-1 text-[10px] text-slate-400">
                  {effect.category}
                  {effect.middleware?.length ? ` · ${effect.middleware.join(", ")}` : ""}
                </p>
              </div>
            ))
          ) : (
            <p className="rounded-xl border border-dashed border-slate-200 bg-slate-50 px-3 py-4 text-[11px] text-slate-400">
              当前没有后端明确记录的 middleware effect。
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function expandedFlowItemIdsForSpan(items: ActualFlowItem[], spanId: string | null): string[] {
  if (!spanId) return [];
  for (const item of items) {
    if (item.span?.id === spanId) return [item.id];
    const childMatch = (item.children || []).some((child) => child.span?.id === spanId || child.id === spanId);
    if (childMatch) return [item.id];
    const nested = expandedFlowItemIdsForSpan(item.children || [], spanId);
    if (nested.length) return [item.id, ...nested];
  }
  return [];
}

function findFlowNodeElement(root: HTMLElement | null, spanId: string): HTMLElement | null {
  if (!root) return null;
  const candidates = Array.from(root.querySelectorAll<HTMLElement>("[data-flow-span-id]"));
  return candidates.find((item) => item.dataset.flowSpanId === spanId) || null;
}

function buildActualFlowWithInvocation(
  spans: TraceSpan[],
  invocation: MiddlewareHookInvocation,
  markerSpan: TraceSpan
): ActualFlowItem[] {
  const base = buildActualFlow(spans);
  return insertTopLevelFlowMarkerForInvocation(
    base,
    middlewareInvocationFlowItem(invocation, markerSpan),
    invocation,
    null
  );
}

function middlewareInvocationFlowItem(
  invocation: MiddlewareHookInvocation,
  markerSpan: TraceSpan
): ActualFlowItem {
  return {
    id: `middleware-trigger-${invocation.id}`,
    type: "middleware",
    status: markerSpan.status,
    label: middlewareInvocationFlowLabel(invocation),
    subtitle: `中间件触发 · ${invocation.title}`,
    title: `${invocation.hook} · ${invocation.title}`,
    span: markerSpan,
  };
}

function middlewareInvocationFlowLabel(invocation: MiddlewareHookInvocation): string {
  const firstMiddleware = stringArray(invocation.invocation?.middleware)[0] || stringArray(invocation.invocation?.metadata?.middleware)[0];
  const raw = invocation.title && invocation.title !== invocation.hook ? invocation.title : firstMiddleware;
  if (!raw) return invocation.hook;
  const compact = raw.replace(/Middleware/g, "").replace(new RegExp(`\\.${invocation.hook}$`), "");
  return compact === invocation.hook ? invocation.hook : `${compact}.${invocation.hook}`;
}

function insertTopLevelFlowMarkerByOrder(
  items: ActualFlowItem[],
  marker: ActualFlowItem,
  markerOrder: number
): ActualFlowItem[] {
  const result: ActualFlowItem[] = [];
  let inserted = false;
  for (const item of items) {
    const itemOrder = item.span ? spanEventOrder(item.span) : Number.POSITIVE_INFINITY;
    if (!inserted && markerOrder <= itemOrder) {
      result.push(marker);
      inserted = true;
    }
    result.push(item);
  }
  if (!inserted) result.push(marker);
  return dedupeFlowItems(result, marker.id);
}

function addMountedWrapperSignatures(items: ActualFlowItem[], trace: AgentTrace | null): ActualFlowItem[] {
  const mountedWraps = mountedHookEntries(trace, "wrap_model_call");
  if (mountedWraps.length === 0) return items;
  return items.map((item) => {
    if (!isGraphModelFlowItem(item)) return item;
    let signature = item.signature || [];
    for (const entry of mountedWraps) {
      const order = middlewareStackOrderFromEntry(entry);
      const hookName = compactMiddlewareName(String(entry.name || "Middleware"));
      const existingIndex = signature.findIndex((candidate) =>
        signatureHookName(candidate) === "wrap_model_call" &&
        signatureMiddlewareName(candidate) === normalizeSignatureMiddlewareName(hookName)
      );
      const mountedMarker: ActualFlowItem = {
        id: `mounted-wrap-${item.id}-${String(entry.name || "middleware")}`,
        type: "middleware",
        status: "completed",
        label: `${hookName}.wrap_model_call`,
        subtitle: "已挂载 wrap_model_call",
        title: `${entry.name}.wrap_model_call · ${entry.note || "runtime inventory"}`,
        signatureOrder: order,
      };
      if (existingIndex >= 0) {
        const currentOrder = middlewareSignatureOrder(signature[existingIndex]);
        signature[existingIndex] = {
          ...signature[existingIndex],
          signatureOrder: currentOrder === Number.POSITIVE_INFINITY ? order : currentOrder,
        };
      } else {
        signature = [...signature, mountedMarker];
      }
    }
    return {
      ...item,
      signature: sortModelHookSignature(signature),
    };
  });
}

function mountedHookEntries(trace: AgentTrace | null, hook: MiddlewareHookName): TraceRuntimeMiddlewareEntry[] {
  const inventory = trace?.runtime_inventory as TraceRuntimeInventory | undefined;
  const hookEntries = inventory?.middleware?.hooks?.[hook] || [];
  const stackEntries = (inventory?.middleware?.stack || []).filter((entry) => middlewareHooks(entry).includes(hook));
  return uniqueMiddlewareEntries([...hookEntries, ...stackEntries]).sort((a, b) => {
    return middlewareStackOrderFromEntry(a) - middlewareStackOrderFromEntry(b);
  });
}

function middlewareStackOrderFromEntry(entry: TraceRuntimeMiddlewareEntry | undefined): number {
  return numberFromUnknown(entry?.stack_order) ??
    numberFromUnknown(entry?.order) ??
    numberFromUnknown(entry?.execution_order) ??
    9999;
}

function compactMiddlewareName(name: string): string {
  return name.replace(/Middleware$/, "");
}

function normalizeSignatureMiddlewareName(label: string): string {
  return compactMiddlewareName(label.split(".", 1)[0] || label).toLowerCase();
}

function signatureMiddlewareName(item: ActualFlowItem): string {
  return normalizeSignatureMiddlewareName(middlewareNameForFlowItem(item) || item.label);
}

function insertTopLevelFlowMarkerForInvocation(
  items: ActualFlowItem[],
  marker: ActualFlowItem,
  invocation: MiddlewareHookInvocation,
  trace: AgentTrace | null
): ActualFlowItem[] {
  if (invocation.hook === "wrap_model_call") {
    return insertWrapperMarkerForInvocation(items, marker, invocation, trace, isGraphModelFlowItem, findGraphModelFlowIndex);
  }
  if (invocation.hook === "wrap_tool_call") {
    return insertWrapperMarkerForInvocation(items, marker, invocation, trace, isGraphToolsFlowItem, findGraphToolsFlowIndex);
  }
  if (invocation.hook === "before_agent") {
    return insertBeforeFirstModelBoundary(items, marker);
  }
  if (invocation.hook === "before_model" || invocation.hook === "after_model") {
    const targetIndex = findGraphModelFlowIndex(items, invocation, spanEventOrder(marker.span!));
    if (targetIndex >= 0) {
      const next = [...items];
      next.splice(invocation.hook === "after_model" ? targetIndex + 1 : targetIndex, 0, marker);
      return dedupeFlowItems(next, marker.id);
    }
  }
  const modelCallIndex = invocationModelCallIndex(invocation);
  if (modelCallIndex !== null) {
    const targetIndex = items.findIndex((item) => flowItemModelCallIndex(item) === modelCallIndex);
    if (targetIndex >= 0) {
      const next = [...items];
      const insertIndex = invocation.hook === "after_model" ? targetIndex + 1 : targetIndex;
      next.splice(insertIndex, 0, marker);
      return dedupeFlowItems(next, marker.id);
    }
  }
  return insertTopLevelFlowMarkerByOrder(items, marker, spanEventOrder(marker.span!));
}

function insertBeforeFirstModelBoundary(items: ActualFlowItem[], marker: ActualFlowItem): ActualFlowItem[] {
  const targetIndex = items.findIndex((item) =>
    isGraphModelFlowItem(item) ||
    (item.type === "middleware" && normalizeHookName(item.span?.metadata?.hook) === "before_model")
  );
  if (targetIndex < 0) return insertTopLevelFlowMarkerByOrder(items, marker, spanEventOrder(marker.span!));
  const next = [...items];
  next.splice(targetIndex, 0, marker);
  return dedupeFlowItems(next, marker.id);
}

function insertWrapperMarkerForInvocation(
  items: ActualFlowItem[],
  marker: ActualFlowItem,
  invocation: MiddlewareHookInvocation,
  trace: AgentTrace | null,
  isTarget: (item: ActualFlowItem) => boolean,
  findTargetIndex: (items: ActualFlowItem[], invocation: MiddlewareHookInvocation, markerOrder: number) => number
): ActualFlowItem[] {
  const targetIndex = findTargetIndex(items, invocation, spanEventOrder(marker.span!));
  if (targetIndex < 0) return insertTopLevelFlowMarkerByOrder(items, marker, spanEventOrder(marker.span!));
  const next = [...items];
  const target = next[targetIndex];
  if (!isTarget(target)) return insertTopLevelFlowMarkerByOrder(items, marker, spanEventOrder(marker.span!));
  next[targetIndex] = {
    ...target,
    signature: insertModelHookSignature(
      target.signature || [],
      withMiddlewareExecutionOrder(marker, middlewareInvocationStackOrder(invocation, trace)),
      invocation.hook
    ),
  };
  return dedupeFlowItems(next, marker.id);
}

function withMiddlewareExecutionOrder(marker: ActualFlowItem, order: number): ActualFlowItem {
  if (!marker.span) return { ...marker, signatureOrder: order };
  return {
    ...marker,
    signatureOrder: order,
    span: {
      ...marker.span,
      metadata: {
        ...(marker.span.metadata || {}),
        middleware_execution_order: order,
      },
    },
  };
}

function findGraphModelFlowIndex(
  items: ActualFlowItem[],
  invocation: MiddlewareHookInvocation,
  markerOrder: number
): number {
  const modelCallIndex = invocationModelCallIndex(invocation);
  if (modelCallIndex !== null) {
    const byIndex = items.findIndex((item) => isGraphModelFlowItem(item) && flowItemModelCallIndex(item) === modelCallIndex);
    if (byIndex >= 0) return byIndex;
  }
  const afterMarker = items.findIndex(
    (item) => isGraphModelFlowItem(item) && item.span && spanEventOrder(item.span) >= markerOrder
  );
  if (afterMarker >= 0) return afterMarker;
  return items.findIndex(isGraphModelFlowItem);
}

function isGraphModelFlowItem(item: ActualFlowItem): boolean {
  return item.type === "graph" && item.label === "graph.model";
}

function findGraphToolsFlowIndex(
  items: ActualFlowItem[],
  invocation: MiddlewareHookInvocation,
  markerOrder: number
): number {
  const sourceSpanId = stringFromUnknown(invocation.invocation?.metadata?.source_span_id);
  if (sourceSpanId) {
    const bySource = items.findIndex((item) =>
      isGraphToolsFlowItem(item) &&
      (item.span?.id === sourceSpanId || (item.children || []).some((child) => child.span?.id === sourceSpanId))
    );
    if (bySource >= 0) return bySource;
  }
  const afterMarker = items.findIndex(
    (item) => isGraphToolsFlowItem(item) && item.span && spanEventOrder(item.span) >= markerOrder
  );
  if (afterMarker >= 0) return afterMarker;
  return items.findIndex(isGraphToolsFlowItem);
}

function isGraphToolsFlowItem(item: ActualFlowItem): boolean {
  return item.type === "graph" && item.label === "graph.tools";
}

function insertModelHookSignature(
  signature: ActualFlowItem[],
  marker: ActualFlowItem,
  hook: MiddlewareHookName
): ActualFlowItem[] {
  const markerKey = modelHookSignatureKey(marker, hook);
  const withoutDuplicate = signature.filter((item) =>
    item.id !== marker.id && modelHookSignatureKey(item) !== markerKey
  );
  const subtitle =
    hook === "before_model"
      ? "模型调用前的中间件边界"
      : hook === "wrap_model_call"
        ? "包裹真实 LLM 调用"
        : "模型返回后的中间件边界";
  return sortModelHookSignature([
    ...withoutDuplicate,
    {
      ...marker,
      label: modelHookSignatureLabel(marker, hook),
      subtitle,
      title: `${marker.title} · ${subtitle}`,
    },
  ]);
}

function modelHookSignatureKey(item: ActualFlowItem, hookOverride?: MiddlewareHookName): string {
  const hook = hookOverride || signatureHookName(item);
  return `${hook}:${signatureMiddlewareName(item)}`;
}

function sortModelHookSignature(signature: ActualFlowItem[]): ActualFlowItem[] {
  return [...signature].sort((a, b) => {
    const rank = (item: ActualFlowItem) => modelHookSignatureRank(signatureHookName(item));
    const rankDelta = rank(a) - rank(b);
    if (rankDelta !== 0) return rankDelta;
    const stackDelta = middlewareSignatureOrder(a) - middlewareSignatureOrder(b);
    if (stackDelta !== 0) return stackDelta;
    const orderDelta = (a.span ? spanEventOrder(a.span) : Number.POSITIVE_INFINITY) -
      (b.span ? spanEventOrder(b.span) : Number.POSITIVE_INFINITY);
    if (orderDelta !== 0) return orderDelta;
    return a.label.localeCompare(b.label);
  });
}

function signatureHookName(item: ActualFlowItem): MiddlewareHookName {
  const fromMetadata = normalizeHookName(item.span?.metadata?.hook);
  if (fromMetadata) return fromMetadata;
  const fromLabel = item.label.match(/\.(before_agent|before_model|wrap_model_call|after_model|wrap_tool_call|after_agent)$/)?.[1];
  return normalizeHookName(fromLabel) || "wrap_model_call";
}

function modelHookSignatureRank(hook: MiddlewareHookName): number {
  const ranks: Record<MiddlewareHookName, number> = {
    before_agent: 0,
    before_model: 1,
    wrap_model_call: 2,
    after_model: 3,
    wrap_tool_call: 4,
    after_agent: 5,
  };
  return ranks[hook];
}

function middlewareSignatureOrder(item: ActualFlowItem): number {
  return numberFromUnknown(item.span?.metadata?.middleware_execution_order) ?? Number.POSITIVE_INFINITY;
}

function modelHookSignatureLabel(marker: ActualFlowItem, hook: MiddlewareHookName): string {
  const rawTitle = marker.title.replace(`${hook} · `, "").replace(`${hook}: `, "");
  const middlewareName = middlewareNameForFlowItem(marker);
  const raw = middlewareName || (rawTitle && rawTitle !== hook ? rawTitle : hook);
  const compact = raw.replace(/Middleware/g, "").replace(new RegExp(`\\.${hook}$`), "");
  return compact === hook ? hook : `${compact}.${hook}`;
}

function middlewareNameForFlowItem(item: ActualFlowItem): string {
  const metadata = item.span?.metadata || {};
  const candidates = [
    ...stringArray(metadata.middleware),
    ...stringArray(metadata.proxied_middleware),
    stringFromUnknown(metadata.proxied_middleware),
    stringFromUnknown(metadata.middleware_name),
    stringFromUnknown(metadata.middleware),
  ];
  for (const candidate of candidates) {
    const text = candidate.trim();
    if (text) return text;
  }
  const labelMatch = item.label.match(/^([A-Za-z0-9_]+)(?:Middleware)?\./);
  if (labelMatch) return labelMatch[1];
  return "";
}

function invocationModelCallIndex(invocation: MiddlewareHookInvocation): number | null {
  const candidates = [
    invocation.invocation?.metadata?.model_call_index,
    recordFromUnknown(invocation.invocation?.before).model_call_index,
    recordFromUnknown(invocation.invocation?.after).model_call_index,
    recordFromUnknown(invocation.invocation?.diff).model_call_index,
    recordFromUnknown(invocation.invocation?.diff).model_call_index_after,
    invocation.spans[0]?.metadata?.model_call_index,
    invocation.effects[0]?.metadata?.model_call_index,
  ];
  for (const candidate of candidates) {
    const parsed = numberFromUnknown(candidate);
    if (parsed !== null) return parsed;
  }
  return null;
}

function numberFromUnknown(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function flowItemModelCallIndex(item: ActualFlowItem): number | null {
  const candidates = [
    item.span?.metadata?.model_call_index,
    item.span?.metadata?.display_model_call_index,
    ...(item.children || []).flatMap((child) => [
      child.span?.metadata?.model_call_index,
      child.span?.metadata?.display_model_call_index,
    ]),
  ];
  for (const candidate of candidates) {
    if (typeof candidate === "number") return candidate;
  }
  return null;
}

function dedupeFlowItems(items: ActualFlowItem[], markerId: string): ActualFlowItem[] {
  let markerSeen = false;
  return items.flatMap((item) => {
    if (item.id === markerId) {
      if (markerSeen) return [];
      markerSeen = true;
      return [item];
    }
    return [
      {
        ...item,
        signature: item.signature ? dedupeFlowItems(item.signature, markerId) : item.signature,
        children: item.children ? dedupeFlowItems(item.children, markerId) : item.children,
      },
    ];
  });
}

function middlewareInvocationMarkerSpan(invocation: MiddlewareHookInvocation): TraceSpan {
  const existing = invocation.spans[0];
  const metadata = {
    ...(existing?.metadata || {}),
    ...(invocation.invocation?.metadata || {}),
    event_order:
      invocation.invocation?.sequence ??
      existing?.metadata?.event_order ??
      invocation.sequence,
    hook: invocation.hook,
    middleware_invocation_id: invocation.id,
    middleware_marker: true,
    linked_span_id: existing?.id,
  };
  return {
    id: `middleware-invocation-${invocation.id}`,
    parent_id: existing?.parent_id || null,
    type: "middleware",
    name: `${invocation.hook}: ${invocation.title}`,
    started_at: existing?.started_at || invocation.invocation?.created_at || Date.now() / 1000,
    completed_at: existing?.completed_at || invocation.invocation?.created_at || null,
    status: existing?.status || "completed",
    input: existing?.input ?? invocation.invocation?.before ?? null,
    output:
      existing?.output ??
      {
        before: invocation.invocation?.before,
        after: invocation.invocation?.after,
        diff: invocation.invocation?.diff || {},
        evidence: invocation.evidence,
      },
    metadata,
    children: existing?.children || [],
  };
}

function dedupeStrings(items: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const item of items) {
    const normalized = item.trim();
    if (!normalized || seen.has(normalized)) continue;
    seen.add(normalized);
    result.push(normalized);
  }
  return result;
}

function buildMiddlewareHookGroups(
  inventory: TraceRuntimeInventory,
  trace: AgentTrace
): MiddlewareHookGroup[] {
  const stack = inventory.middleware?.stack || [];
  const inventoryHooks = inventory.middleware?.hooks || {};
  const effects = trace.middleware_effects || [];

  return MIDDLEWARE_HOOK_SPECS.map((spec) => {
    const middleware = uniqueMiddlewareEntries([
      ...(inventoryHooks[spec.hook] || []),
      ...stack.filter((entry) => middlewareHooks(entry).includes(spec.hook)),
    ]);
    const hookEffects = effects.filter((effect) => normalizeHookName(effect.hook || effect.metadata?.hook || effect.metadata?.langchain_hook) === spec.hook);
    return {
      ...spec,
      middleware,
      effects: hookEffects,
      invocations: buildHookInvocations(spec.hook, trace, hookEffects, middleware),
    };
  });
}

function buildHookInvocations(
  hook: MiddlewareHookName,
  trace: AgentTrace,
  effects: TraceMiddlewareEffect[],
  middleware: TraceRuntimeMiddlewareEntry[] = []
): MiddlewareHookInvocation[] {
  const spans = flattenTraceSpans(trace);
  const exactInvocations = dedupeInferredInvocations((trace.middleware_invocations || [])
    .filter((invocation) => normalizeHookName(invocation.hook) === hook)
    .sort((a, b) => {
      const sequenceDelta = Number(a.sequence || 0) - Number(b.sequence || 0);
      if (sequenceDelta !== 0) return sequenceDelta;
      const createdAtDelta = Number(a.created_at || 0) - Number(b.created_at || 0);
      if (createdAtDelta !== 0) return createdAtDelta;
      const aMiddlewareOrder = middlewareInvocationOrder(a, middleware);
      const bMiddlewareOrder = middlewareInvocationOrder(b, middleware);
      if ((Number.isFinite(aMiddlewareOrder) || Number.isFinite(bMiddlewareOrder)) && aMiddlewareOrder !== bMiddlewareOrder) {
        return aMiddlewareOrder - bMiddlewareOrder;
      }
      return 0;
    }));
  if (exactInvocations.length) {
    return exactInvocations.map((invocation, index, all) => {
      const linkedSpans = spansForMiddlewareInvocation(invocation, spans);
      const linkedEffects = effectsForMiddlewareInvocation(invocation, effects);
      const previous = stringFromUnknown(invocation.flow_ref?.previous) || previousExactInvocationLabel(hook, all, index);
      const next = stringFromUnknown(invocation.flow_ref?.next) || nextExactInvocationLabel(hook, all, index);
      return {
        id: invocation.id,
        hook,
        sequence: invocation.sequence,
        sequenceLabel: `第 ${index + 1} 次 / 共 ${all.length} 次`,
        title: invocation.title || `${hook} #${index + 1}`,
        note: invocation.category ? `${invocation.category} · ${invocation.status}` : invocation.status,
        previous,
        next,
        reason: "后端 middleware_invocation 已明确记录该 hook 触发。",
        flow: compactEvidence([previous, hook, next]).length ? compactEvidence([previous, hook, next]) : [hook],
        spans: linkedSpans,
        effects: linkedEffects,
        invocation,
        evidence: compactEvidence([
          ...((invocation.evidence || []) as string[]),
          linkedSpans[0] ? `${typeLabel(linkedSpans[0].type)} span: ${spanLabel(linkedSpans[0])}` : "",
          linkedEffects[0] ? `effect: ${linkedEffects[0].title}` : "",
        ]),
      };
    });
  }
  const explicitSpans = spans.filter((span) => spanMatchesHook(span, hook));
  const fallbackSpans = explicitSpans.length ? explicitSpans : fallbackSpansForHook(hook, spans, trace);
  const groups = fallbackSpans.map((span) => ({
    span,
    effects: effects.filter((effect) => effectBelongsNearSpan(effect, span)),
  }));

  const effectOnlyGroups = effects
    .filter((effect) => !groups.some((group) => group.effects.includes(effect)))
    .map((effect) => ({ span: null, effects: [effect] }));

  return [...groups, ...effectOnlyGroups]
    .sort((a, b) => eventTime(a.span, a.effects) - eventTime(b.span, b.effects))
    .map((group, index, all) => {
      const span = group.span;
      const sequence = span ? spanEventOrder(span) : index + 1;
      const title = invocationTitle(hook, span, index);
      const previous = previousInvocationLabel(hook, all, index);
      const next = nextInvocationLabel(hook, all, index);
      return {
        id: `${hook}-${span?.id || group.effects[0]?.id || index}`,
        hook,
        sequence,
        sequenceLabel: `第 ${index + 1} 次 / 共 ${all.length} 次`,
        title,
        note: invocationNote(hook, span, group.effects),
        previous,
        next,
        reason: invocationReason(hook, span, group.effects),
        flow: compactEvidence([previous, hook, next]).length
          ? compactEvidence([previous, hook, next])
          : [hook],
        spans: span ? [span] : [],
        effects: group.effects,
        invocation: undefined,
        evidence: invocationEvidence(span, group.effects),
      };
    });
}

function middlewareInvocationOrder(
  invocation: TraceMiddlewareInvocation,
  middleware: TraceRuntimeMiddlewareEntry[]
): number {
  const invocationNames = new Set((invocation.middleware || []).map((name) => String(name).toLowerCase()));
  const title = String(invocation.title || "").toLowerCase();
  const matchedOrders = middleware
    .filter((entry) => {
      const entryName = entry.name.toLowerCase();
      const shortName = entryName.replace(/middleware$/, "");
      return invocationNames.has(entryName) || title.includes(entryName) || Boolean(shortName && title.includes(shortName));
    })
    .map((entry) => Number(entry.execution_order || entry.stack_order || entry.order || Number.POSITIVE_INFINITY))
    .filter((order) => Number.isFinite(order));
  return matchedOrders.length ? Math.min(...matchedOrders) : Number.POSITIVE_INFINITY;
}

function dedupeInferredInvocations(invocations: TraceMiddlewareInvocation[]): TraceMiddlewareInvocation[] {
  const directKeys = new Set(
    invocations
      .filter((invocation) => invocation.metadata?.coverage === "direct")
      .flatMap((invocation) =>
        (invocation.middleware || []).map((middleware) => `${normalizeHookName(invocation.hook)}::${String(middleware)}`)
      )
  );
  return invocations.filter((invocation) => {
    if (isHookLevelBoundaryInvocation(invocation)) return false;
    if (invocation.metadata?.coverage !== "inferred") return true;
    return !(invocation.middleware || []).some((middleware) =>
      directKeys.has(`${normalizeHookName(invocation.hook)}::${String(middleware)}`)
    );
  });
}

function isHookLevelBoundaryInvocation(invocation: TraceMiddlewareInvocation): boolean {
  return (
    invocation.metadata?.coverage === "inferred" &&
    invocation.category === "model_input" &&
    invocation.title === "Model input boundary"
  );
}

function spansForMiddlewareInvocation(
  invocation: TraceMiddlewareInvocation,
  spans: TraceSpan[]
): TraceSpan[] {
  const sourceSpanId = stringFromUnknown(invocation.metadata?.source_span_id);
  if (sourceSpanId) {
    const span = spans.find((item) => item.id === sourceSpanId);
    if (span) return [span];
  }
  return spans.filter((span) => spanMatchesHook(span, normalizeHookName(invocation.hook) || "before_agent")).slice(0, 1);
}

function effectsForMiddlewareInvocation(
  invocation: TraceMiddlewareInvocation,
  effects: TraceMiddlewareEffect[]
): TraceMiddlewareEffect[] {
  const effectId = stringFromUnknown(invocation.metadata?.effect_id);
  if (effectId) {
    const effect = effects.find((item) => item.id === effectId);
    if (effect) return [effect];
  }
  const invocationMiddleware = new Set((invocation.middleware || []).map((item) => String(item)));
  const hook = normalizeHookName(invocation.hook);
  const matched = effects.filter((effect) => {
    if (isHookLevelBoundaryEffect(effect)) return false;
    if (effect.title === invocation.title) return true;
    if (normalizeHookName(effect.hook || effect.metadata?.hook || effect.metadata?.langchain_hook) !== hook) {
      return false;
    }
    return (effect.middleware || []).some((item) => invocationMiddleware.has(String(item)));
  });
  return matched.slice(0, 3);
}

function previousExactInvocationLabel(
  hook: MiddlewareHookName,
  invocations: TraceMiddlewareInvocation[],
  index: number
): string {
  if (index <= 0) return previousHookBoundaryLabel(hook);
  return invocations[index - 1]?.title || "previous";
}

function nextExactInvocationLabel(
  hook: MiddlewareHookName,
  invocations: TraceMiddlewareInvocation[],
  index: number
): string {
  if (index >= invocations.length - 1) return nextHookBoundaryLabel(hook);
  return invocations[index + 1]?.title || "next";
}

function stringFromUnknown(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function fallbackSpansForHook(
  hook: MiddlewareHookName,
  spans: TraceSpan[],
  trace: AgentTrace
): TraceSpan[] {
  if (hook === "before_agent") return spans.filter((span) => span.type === "root").slice(0, 1);
  if (hook === "after_agent") {
    const root = spans.find((span) => span.type === "root");
    return root && trace.status !== "running" ? [root] : [];
  }
  if (hook === "before_model") return spans.filter((span) => span.type === "model_input");
  if (hook === "wrap_model_call") return spans.filter((span) => span.type === "llm");
  if (hook === "after_model") return spans.filter((span) => span.type === "llm" || span.type === "model_input");
  if (hook === "wrap_tool_call") {
    return spans.filter((span) => ["tool", "skill", "memory", "subagent", "permission"].includes(span.type));
  }
  return [];
}

function spanMatchesHook(span: TraceSpan, hook: MiddlewareHookName): boolean {
  return normalizeHookName(span.metadata?.hook) === hook ||
    normalizeHookName(span.metadata?.langchain_hook) === hook ||
    normalizeHookName(span.metadata?.middleware_hook) === hook ||
    normalizeHookName(span.name) === hook;
}

function normalizeHookName(value: unknown): MiddlewareHookName | null {
  const text = String(value || "").toLowerCase();
  return MIDDLEWARE_HOOK_SPECS.find((spec) => text.includes(spec.hook))?.hook || null;
}

function eventTime(span: TraceSpan | null, effects: TraceMiddlewareEffect[]): number {
  return span?.started_at || effects[0]?.created_at || 0;
}

function effectBelongsNearSpan(effect: TraceMiddlewareEffect, span: TraceSpan): boolean {
  const createdAt = effect.created_at;
  if (!createdAt) return false;
  return Math.abs(createdAt - span.started_at) < 2;
}

function invocationTitle(hook: MiddlewareHookName, span: TraceSpan | null, index: number): string {
  if (span?.name) return spanLabel(span);
  if (hook === "before_agent") return "Session bootstrap";
  if (hook === "after_agent") return "Session close";
  return `${hook} #${index + 1}`;
}

function invocationNote(
  hook: MiddlewareHookName,
  span: TraceSpan | null,
  effects: TraceMiddlewareEffect[]
): string {
  if (effects.length) return `记录到 ${effects.length} 条 middleware effect`;
  if (span?.type === "model_input") return "模型调用前的上下文快照";
  if (span?.type === "llm") return "真实模型调用边界";
  if (["tool", "skill", "memory", "subagent"].includes(span?.type || "")) return "工具调用边界";
  if (hook === "before_agent") return "进入 Agent graph";
  if (hook === "after_agent") return "Agent run 收尾";
  return "从现有 trace 推导";
}

function invocationReason(
  hook: MiddlewareHookName,
  span: TraceSpan | null,
  effects: TraceMiddlewareEffect[]
): string {
  if (effects.length) return "后端 trace 已记录该 hook 的 middleware effect。";
  if (span) return `由 ${typeLabel(span.type)} span 推导；后续可用 middleware_invocation 事件提升精度。`;
  return `本轮没有捕获 ${hook} 的真实事件。`;
}

function previousInvocationLabel(
  hook: MiddlewareHookName,
  groups: Array<{ span: TraceSpan | null; effects: TraceMiddlewareEffect[] }>,
  index: number
): string {
  if (index <= 0) return previousHookBoundaryLabel(hook);
  const prev = groups[index - 1];
  return prev.span ? spanLabel(prev.span) : prev.effects[0]?.title || "previous";
}

function nextInvocationLabel(
  hook: MiddlewareHookName,
  groups: Array<{ span: TraceSpan | null; effects: TraceMiddlewareEffect[] }>,
  index: number
): string {
  if (index >= groups.length - 1) return nextHookBoundaryLabel(hook);
  const next = groups[index + 1];
  return next.span ? spanLabel(next.span) : next.effects[0]?.title || "next";
}

function previousHookBoundaryLabel(hook: MiddlewareHookName): string {
  const labels: Record<MiddlewareHookName, string> = {
    before_agent: "Agent start",
    before_model: "before_agent",
    wrap_model_call: "before_model / model request",
    after_model: "LLM response",
    wrap_tool_call: "graph.tools",
    after_agent: "after_model / tools",
  };
  return labels[hook];
}

function nextHookBoundaryLabel(hook: MiddlewareHookName): string {
  const labels: Record<MiddlewareHookName, string> = {
    before_agent: "before_model",
    before_model: "Model input boundary",
    wrap_model_call: "LLM call",
    after_model: "tools / end",
    wrap_tool_call: "after_model",
    after_agent: "Agent end",
  };
  return labels[hook];
}

function invocationEvidence(span: TraceSpan | null, effects: TraceMiddlewareEffect[]): string[] {
  return compactEvidence([
    span ? `${typeLabel(span.type)} span: ${spanLabel(span)}` : "",
    span?.status ? `status=${span.status}` : "",
    span ? `started=${formatTraceClock(span.started_at)}` : "",
    ...effects.flatMap((effect) => effect.evidence || []),
  ]);
}

function formatTraceClock(timestamp: number): string {
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

function effectsForMiddleware(
  effects: TraceMiddlewareEffect[],
  entry: TraceRuntimeMiddlewareEntry
): TraceMiddlewareEffect[] {
  const target = entry.name.toLowerCase();
  return effects.filter((effect) =>
    !isHookLevelBoundaryEffect(effect) &&
    (effect.middleware || []).some((name) => name.toLowerCase() === target || name.toLowerCase().includes(target))
  );
}

function effectRepresentsMiddlewareChange(
  effect: TraceMiddlewareEffect,
  entry: TraceRuntimeMiddlewareEntry
): boolean {
  if (isHookLevelBoundaryEffect(effect)) {
    return false;
  }
  const name = entry.name.toLowerCase();
  if (name.includes("summarization") && effect.category === "model_input") {
    return false;
  }
  return true;
}

function isHookLevelBoundaryEffect(effect: TraceMiddlewareEffect): boolean {
  return effect.category === "model_input" && effect.title === "Model input boundary";
}

function invocationIncludesMiddleware(
  invocation: MiddlewareHookInvocation,
  entry: TraceRuntimeMiddlewareEntry
): boolean {
  const target = entry.name.toLowerCase();
  return Boolean(
    invocation.invocation?.middleware?.some(
      (name) => name.toLowerCase() === target || name.toLowerCase().includes(target)
    )
  );
}

function middlewareStatusLabel(status: MiddlewareStatus): string {
  if (status === "changed") return "changed";
  if (status === "read") return "observed";
  return "noop";
}

function middlewareStatusClass(status: MiddlewareStatus): string {
  if (status === "changed") return "bg-blue-50 text-blue-700";
  if (status === "read") return "bg-emerald-50 text-emerald-700";
  return "bg-slate-100 text-slate-500";
}

function MiddlewareEffectCard({ group }: { group: MiddlewareEffectGroup }) {
  return (
    <div className={`rounded-lg border p-2 ${group.tone}`}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-[11px] font-semibold">{group.title}</p>
          <p className="mt-0.5 line-clamp-2 text-[10px] opacity-70">{group.description}</p>
        </div>
        <span className="shrink-0 rounded-full bg-white/80 px-2 py-0.5 text-[9px] font-semibold shadow-sm">
          {group.middleware.length}
        </span>
      </div>
      <div className="mt-2 flex flex-wrap gap-1">
        {group.middleware.length ? (
          group.middleware.map((entry) => (
            <span
              key={`${group.key}-${entry.name}-${entry.stack_order || entry.order || ""}`}
              className="rounded border border-white/80 bg-white/70 px-1.5 py-0.5 text-[9px] font-medium"
            >
              {entry.name}
            </span>
          ))
        ) : (
          <span className="rounded border border-white/80 bg-white/60 px-1.5 py-0.5 text-[9px] opacity-60">
            暂无
          </span>
        )}
      </div>
      {group.evidence.length > 0 && (
        <div className="mt-2 space-y-1">
          {group.evidence.map((item) => (
            <p key={item} className="rounded bg-white/60 px-2 py-1 text-[9px] leading-relaxed opacity-75">
              {item}
            </p>
          ))}
        </div>
      )}
      {group.effects.length > 0 && (
        <div className="mt-2 rounded bg-white/70 px-2 py-1 text-[9px] font-medium opacity-80">
          已记录 {group.effects.length} 条实际 effect
        </div>
      )}
    </div>
  );
}

function MiddlewareEffectEvidence({ effect }: { effect: TraceMiddlewareEffect }) {
  const middleware = effect.middleware || [];
  const evidence = effect.evidence || [];
  return (
    <div className="rounded-md border border-slate-100 bg-white p-2 shadow-sm">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[11px] font-semibold text-slate-800">{effect.title}</span>
            <span className="rounded border border-blue-100 bg-blue-50 px-1.5 py-0.5 text-[9px] font-medium text-blue-700">
              {effect.category}
            </span>
            {effect.hook && (
              <span className="rounded border border-slate-100 bg-slate-50 px-1.5 py-0.5 text-[9px] font-medium text-slate-500">
                {effect.hook}
              </span>
            )}
          </div>
          {middleware.length > 0 && (
            <p className="mt-1 line-clamp-1 text-[10px] text-slate-400">
              middleware: {middleware.join(", ")}
            </p>
          )}
        </div>
      </div>
      {evidence.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {evidence.slice(0, 4).map((item) => (
            <span
              key={item}
              className="rounded-full bg-slate-50 px-2 py-0.5 text-[9px] font-medium text-slate-500"
            >
              {item}
            </span>
          ))}
        </div>
      )}
      <FactDiffSummary diff={effect.diff || {}} before={effect.before} after={effect.after} />
      <div className="mt-2 grid gap-2 md:grid-cols-3">
        <EffectMiniBlock title="Before" value={effect.before} />
        <EffectMiniBlock title="After" value={effect.after} />
        <EffectMiniBlock title="Diff" value={effect.diff || {}} />
      </div>
    </div>
  );
}

function FactDiffSummary({
  diff,
  before,
  after,
}: {
  diff: Record<string, unknown>;
  before?: unknown;
  after?: unknown;
}) {
  const hasState = hasStateDiff(diff);
  const modelInputItems = modelInputDiffItems(diff);
  if (!hasState && !modelInputItems.length) return null;
  return (
    <div className="mt-2 grid gap-2">
      {hasState && <StateDiffSummary diff={diff} before={before} after={after} />}
      {modelInputItems.length > 0 && <ModelInputDiffSummary items={modelInputItems} before={before} after={after} />}
    </div>
  );
}

function hasFactDiffSummary(diff: Record<string, unknown>): boolean {
  return hasStateDiff(diff) || modelInputDiffItems(diff).length > 0;
}

function StateDiffSummary({
  diff,
  before,
  after,
}: {
  diff: Record<string, unknown>;
  before?: unknown;
  after?: unknown;
}) {
  const added = stringArray(diff.state_keys_added);
  const removed = stringArray(diff.state_keys_removed);
  const changed = stringArray(diff.state_fields_changed);
  const countDeltas = Object.entries(diff)
    .filter(([key, value]) => key.startsWith("state_") && key.endsWith("_count_delta") && typeof value === "number")
    .map(([key, value]) => ({
      field: key.replace(/^state_/, "").replace(/_count_delta$/, ""),
      delta: Number(value),
    }));
  const fieldDetails = stateFieldDetails(after, changed);
  if (!added.length && !removed.length && !changed.length && !countDeltas.length) return null;
  return (
    <div className="rounded-lg border border-blue-50 bg-blue-50/40 px-2 py-2">
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <span className="text-[10px] font-bold text-blue-700">State fields</span>
        {stateFieldCount(after) > 0 && (
          <span className="rounded-full bg-white px-2 py-0.5 text-[9px] font-semibold text-slate-500">
            {stateFieldCount(after)} fields
          </span>
        )}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {added.map((field) => (
          <StateFieldChip key={`added-${field}`} tone="green" label={`+ ${field}`} />
        ))}
        {removed.map((field) => (
          <StateFieldChip key={`removed-${field}`} tone="red" label={`- ${field}`} />
        ))}
        {changed.map((field) => (
          <StateFieldChip key={`changed-${field}`} tone="blue" label={`~ ${field}`} detail={fieldDetails[field]} />
        ))}
        {countDeltas.map((item) => (
          <StateFieldChip
            key={`delta-${item.field}`}
            tone={item.delta > 0 ? "green" : "amber"}
            label={`${item.field} ${item.delta > 0 ? "+" : ""}${item.delta}`}
          />
        ))}
      </div>
      {Boolean(before) && stateFieldCount(before) > 0 && (
        <p className="mt-1.5 text-[9px] text-slate-400">
          before {stateFieldCount(before)} fields · after {stateFieldCount(after)} fields
        </p>
      )}
    </div>
  );
}

function ModelInputDiffSummary({
  items,
  before,
  after,
}: {
  items: Array<{ label: string; tone: "blue" | "green" | "amber" | "red" }>;
  before?: unknown;
  after?: unknown;
}) {
  return (
    <div className="rounded-lg border border-indigo-50 bg-indigo-50/40 px-2 py-2">
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <span className="text-[10px] font-bold text-indigo-700">Model input</span>
        {modelInputMetric(after, "message_count") !== undefined && (
          <span className="rounded-full bg-white px-2 py-0.5 text-[9px] font-semibold text-slate-500">
            {modelInputMetric(after, "message_count")} messages
          </span>
        )}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {items.map((item) => (
          <StateFieldChip key={item.label} tone={item.tone} label={item.label} />
        ))}
      </div>
      {modelInputMetric(before, "estimated_tokens") !== undefined &&
        modelInputMetric(after, "estimated_tokens") !== undefined && (
          <p className="mt-1.5 text-[9px] text-slate-400">
            before ~{modelInputMetric(before, "estimated_tokens")} tokens · after ~
            {modelInputMetric(after, "estimated_tokens")} tokens
          </p>
        )}
    </div>
  );
}

function StateFieldChip({
  label,
  detail,
  tone,
}: {
  label: string;
  detail?: string;
  tone: "blue" | "green" | "red" | "amber";
}) {
  const toneClass = {
    blue: "border-blue-100 bg-white text-blue-700",
    green: "border-emerald-100 bg-white text-emerald-700",
    red: "border-rose-100 bg-white text-rose-700",
    amber: "border-amber-100 bg-white text-amber-700",
  }[tone];
  return (
    <span className={`max-w-full rounded-md border px-2 py-1 text-[9px] font-semibold ${toneClass}`}>
      {label}
      {detail && <span className="ml-1 font-normal text-slate-400">{detail}</span>}
    </span>
  );
}

function EffectMiniBlock({ title, value }: { title: string; value: unknown }) {
  return (
    <div className="min-w-0 rounded border border-slate-100 bg-slate-50/70 p-1.5">
      <p className="mb-1 text-[9px] font-semibold uppercase tracking-normal text-slate-400">{title}</p>
      <pre className="max-h-24 overflow-auto whitespace-pre-wrap break-words text-[9px] leading-relaxed text-slate-600">
        {formatCompactJson(value)}
      </pre>
    </div>
  );
}

function formatCompactJson(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

function recordFromUnknown(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function hasStateDiff(diff: Record<string, unknown>): boolean {
  return (
    stringArray(diff.state_keys_added).length > 0 ||
    stringArray(diff.state_keys_removed).length > 0 ||
    stringArray(diff.state_fields_changed).length > 0 ||
    Object.entries(diff).some(
      ([key, value]) => key.startsWith("state_") && key.endsWith("_count_delta") && typeof value === "number"
    )
  );
}

function modelInputDiffItems(diff: Record<string, unknown>): Array<{ label: string; tone: "blue" | "green" | "amber" | "red" }> {
  const items: Array<{ label: string; tone: "blue" | "green" | "amber" | "red" }> = [];
  const deltaKeys: Array<[string, string]> = [
    ["message_count_delta", "messages"],
    ["estimated_tokens_delta", "tokens"],
    ["system_prompt_chars_delta", "system prompt"],
    ["tool_schema_count_delta", "tool schemas"],
    ["tool_call_count_delta", "tool calls"],
  ];
  deltaKeys.forEach(([key, label]) => {
    const value = diff[key];
    if (typeof value === "number" && value !== 0) {
      items.push({
        label: `${label} ${value > 0 ? "+" : ""}${value}`,
        tone: value > 0 ? "green" : "amber",
      });
    }
  });
  const hashKeys: Array<[string, string]> = [
    ["messages_hash_changed", "messages changed"],
    ["system_prompt_hash_changed", "system prompt changed"],
    ["tool_schema_hash_changed", "tool schemas changed"],
  ];
  hashKeys.forEach(([key, label]) => {
    if (diff[key] === true) items.push({ label, tone: "blue" });
  });
  if (diff.initial === true) items.push({ label: "initial model input", tone: "blue" });
  return items;
}

function modelInputMetric(value: unknown, key: string): number | undefined {
  const record = recordFromUnknown(value);
  const metric = record[key];
  return typeof metric === "number" ? metric : undefined;
}

function stateFieldCount(value: unknown): number {
  const fields = recordFromUnknown(recordFromUnknown(value).state_fields);
  return Object.keys(fields).length;
}

function stateFieldDetails(value: unknown, fields: string[]): Record<string, string> {
  const stateFields = recordFromUnknown(recordFromUnknown(value).state_fields);
  return Object.fromEntries(
    fields.map((field) => {
      const summary = recordFromUnknown(stateFields[field]);
      const parts = [
        typeof summary.type === "string" ? summary.type : "",
        typeof summary.count === "number" ? `count ${summary.count}` : "",
        typeof summary.chars === "number" ? `${summary.chars} chars` : "",
      ].filter(Boolean);
      return [field, parts.join(" · ")];
    })
  );
}

function buildMiddlewareEffectGroups(
  inventory: TraceRuntimeInventory,
  trace: AgentTrace
): MiddlewareEffectGroup[] {
  const stack = inventory.middleware?.stack || [];
  const spans = flattenTraceSpans(trace);
  const modelInputs = spans.filter((span) => span.type === "model_input");
  const latestModelInput = modelInputs[modelInputs.length - 1];
  const latestModelMeta = latestModelInput?.metadata || {};
  const todoSpans = spans.filter((span) => span.type === "todo");
  const tools = inventory.tools || [];
  const skills = inventory.skills || [];
  const effects = trace.middleware_effects || [];
  const modelInputEffects = effects.filter((effect) => effect.category === "model_input");
  const contextEffects = effects.filter((effect) => effect.category === "context");
  const skillEffects = effects.filter((effect) => effect.category === "skills");
  const stateEffects = effects.filter((effect) => effect.category === "state");
  const latestModelInputEffect = modelInputEffects[modelInputEffects.length - 1];
  const latestSkillEffect = skillEffects[skillEffects.length - 1];
  const latestStateEffect = stateEffects[stateEffects.length - 1];

  const byEffect = {
    modelInput: stack.filter((entry) => middlewareTouchesModelInput(entry)),
    context: stack.filter((entry) => middlewareTouchesContext(entry)),
    skills: stack.filter((entry) => middlewareTouchesSkills(entry)),
    state: stack.filter((entry) => middlewareTouchesState(entry)),
  };

  return [
    {
      key: "model-input",
      title: "改变 Model Input",
      description: "改写进入 LLM 的 messages、system prompt、tool schemas 或模型调用包装层。",
      middleware: uniqueMiddlewareEntries(byEffect.modelInput),
      evidence: compactEvidence([
        ...effectEvidence(latestModelInputEffect),
        modelInputs.length ? `${modelInputs.length} 次模型输入快照` : "",
        typeof latestModelMeta.message_count === "number"
          ? `${latestModelMeta.message_count} messages / ~${latestModelMeta.estimated_tokens || 0} tokens`
          : "",
        typeof latestModelMeta.tool_schema_count === "number"
          ? `${latestModelMeta.tool_schema_count} tool schemas`
          : "",
      ]),
      effects: modelInputEffects,
      tone: "border-blue-100 bg-blue-50/70 text-blue-800",
    },
    {
      key: "context",
      title: "改变上下文",
      description: "注入/裁剪/压缩上下文，或把 filesystem、memory、历史消息整理进 agent state。",
      middleware: uniqueMiddlewareEntries(byEffect.context),
      evidence: compactEvidence([
        ...effectEvidence(contextEffects[contextEffects.length - 1]),
        typeof latestModelMeta.system_prompt_chars === "number"
          ? `system prompt ${latestModelMeta.system_prompt_chars} chars`
          : "",
        tools.length ? `${tools.length} 个工具 schema 可进入上下文` : "",
      ]),
      effects: contextEffects,
      tone: "border-emerald-100 bg-emerald-50/70 text-emerald-800",
    },
    {
      key: "skills",
      title: "改变 Skill 注入",
      description: "把 skills snapshot、skill 路由或 skill 文件能力接入 system prompt / 工具空间。",
      middleware: uniqueMiddlewareEntries(byEffect.skills),
      evidence: compactEvidence([
        ...effectEvidence(latestSkillEffect),
        skills.length ? `${skills.length} 个 skills snapshot` : "",
        skills.length ? skills.slice(0, 3).map((skill) => skill.name).join(", ") : "",
      ]),
      effects: skillEffects,
      tone: "border-indigo-100 bg-indigo-50/70 text-indigo-800",
    },
    {
      key: "state",
      title: "副作用与状态",
      description: "不一定直接改模型输入，但会写入 todo、tool call patch、memory 或运行状态。",
      middleware: uniqueMiddlewareEntries(byEffect.state),
      evidence: compactEvidence([
        ...effectEvidence(latestStateEffect),
        todoSpans.length ? `${todoSpans.length} 次 todo 状态更新` : "",
        spans.some((span) => span.type === "memory") ? "memory span 已触发" : "",
      ]),
      effects: stateEffects,
      tone: "border-amber-100 bg-amber-50/70 text-amber-800",
    },
  ];
}

function effectEvidence(effect: TraceMiddlewareEffect | undefined): string[] {
  return (effect?.evidence || []).slice(0, 2);
}

function uniqueMiddlewareEntries(entries: TraceRuntimeMiddlewareEntry[]): TraceRuntimeMiddlewareEntry[] {
  const seen = new Set<string>();
  return entries.filter((entry) => {
    const key = `${entry.name}-${entry.stack_order || entry.order || ""}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function compactEvidence(items: string[]): string[] {
  return items.filter((item) => item.trim().length > 0).slice(0, 3);
}

function middlewareName(entry: TraceRuntimeMiddlewareEntry): string {
  return entry.name.toLowerCase();
}

function middlewareHooks(entry: TraceRuntimeMiddlewareEntry): string[] {
  return entry.hooks || [];
}

function middlewareTouchesModelInput(entry: TraceRuntimeMiddlewareEntry): boolean {
  const name = middlewareName(entry);
  const hooks = middlewareHooks(entry);
  return (
    hooks.some((hook) => hook.includes("model")) ||
    name.includes("prompt") ||
    name.includes("cache") ||
    name.includes("summarization") ||
    name.includes("trim") ||
    name.includes("compaction")
  );
}

function middlewareTouchesContext(entry: TraceRuntimeMiddlewareEntry): boolean {
  const name = middlewareName(entry);
  return (
    name.includes("memory") ||
    name.includes("filesystem") ||
    name.includes("summarization") ||
    name.includes("trim") ||
    name.includes("compaction") ||
    name.includes("context")
  );
}

function middlewareTouchesSkills(entry: TraceRuntimeMiddlewareEntry): boolean {
  const name = middlewareName(entry);
  return name.includes("skill");
}

function middlewareTouchesState(entry: TraceRuntimeMiddlewareEntry): boolean {
  const name = middlewareName(entry);
  return (
    name.includes("todo") ||
    name.includes("patchtoolcalls") ||
    name.includes("taskstate") ||
    name.includes("memory") ||
    middlewareHooks(entry).some((hook) => hook.startsWith("after_"))
  );
}

function MountedListCard({
  icon,
  title,
  subtitle,
  empty,
  items,
}: {
  icon: React.ReactNode;
  title: string;
  subtitle: string;
  empty: string;
  items: Array<{ key: string; title: string; subtitle: string; badge: string; href?: string }>;
}) {
  return (
    <div className="min-w-0 rounded-lg border border-slate-100 bg-slate-50/40 p-2">
      <div className="mb-2 flex items-start gap-2">
        {icon}
        <div className="min-w-0">
          <p className="text-[12px] font-semibold text-slate-800">{title}</p>
          <p className="text-[10px] text-slate-400">{subtitle}</p>
        </div>
      </div>
      <div className="max-h-[360px] space-y-1 overflow-auto pr-1">
        {items.length ? (
          items.map((item) => {
            const content = (
              <>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="truncate text-[11px] font-semibold text-slate-800">{item.title}</span>
                    {item.href && <ExternalLink className="h-3 w-3 shrink-0 text-slate-300" />}
                  </div>
                  {item.subtitle && (
                    <p className="mt-0.5 line-clamp-2 text-[10px] leading-relaxed text-slate-500">
                      {item.subtitle}
                    </p>
                  )}
                </div>
                <span className="shrink-0 rounded-full bg-white px-2 py-0.5 text-[9px] font-medium text-slate-500 shadow-sm">
                  {item.badge}
                </span>
              </>
            );
            const className =
              "flex w-full items-start gap-2 rounded-md border border-slate-100 bg-white px-2 py-1.5 text-left transition hover:border-blue-100 hover:bg-blue-50/30";
            return item.href ? (
              <a key={item.key} href={item.href} className={className}>
                {content}
              </a>
            ) : (
              <div key={item.key} className={className}>
                {content}
              </div>
            );
          })
        ) : (
          <p className="rounded-md bg-white px-2 py-3 text-[11px] text-slate-400">{empty}</p>
        )}
      </div>
    </div>
  );
}

function MountedMiddlewareHookCard({
  groups,
  stackCount,
}: {
  groups: MountedMiddlewareHookGroup[];
  stackCount: number;
}) {
  return (
    <div className="min-w-0 rounded-lg border border-slate-100 bg-slate-50/40 p-2">
      <div className="mb-2 flex items-start gap-2">
        <Route className="h-4 w-4 text-violet-500" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <p className="text-[12px] font-semibold text-slate-800">Middleware</p>
            <span className="rounded-full bg-white px-2 py-0.5 text-[9px] font-medium text-slate-500 shadow-sm">
              {stackCount} stack
            </span>
          </div>
          <p className="text-[10px] text-slate-400">按 hook 分组展示本次 agent 实际挂载顺序</p>
        </div>
      </div>

      <div className="max-h-[360px] space-y-2 overflow-auto pr-1">
        {groups.length ? (
          groups.map((group) => (
            <div key={group.hook} className="rounded-lg border border-slate-100 bg-white p-2 shadow-sm">
              <div className="mb-2 flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="text-[11px] font-bold text-slate-900">{group.hook}</p>
                  <p className="mt-0.5 line-clamp-1 text-[9px] text-slate-400">{group.description}</p>
                </div>
                <span className="shrink-0 rounded-full bg-violet-50 px-2 py-0.5 text-[9px] font-bold text-violet-600">
                  {group.middleware.length}
                </span>
              </div>
              <div className="space-y-1">
                {group.middleware.map((entry) => (
                  <div
                    key={`${group.hook}-${entry.name}-${entry.stack_order || entry.order || ""}`}
                    className="flex items-center gap-2 rounded-md bg-slate-50 px-2 py-1.5"
                  >
                    <span className="min-w-7 shrink-0 rounded-full bg-white px-1.5 py-0.5 text-center text-[9px] font-semibold text-slate-500 shadow-sm">
                      #{entry.stack_order || entry.order || "-"}
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-[10px] font-semibold text-slate-800">{entry.name}</p>
                      {entry.note && (
                        <p className="mt-0.5 line-clamp-1 text-[9px] text-slate-400">{entry.note}</p>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))
        ) : (
          <p className="rounded-md bg-white px-2 py-3 text-[11px] text-slate-400">没有 middleware 挂载</p>
        )}
      </div>
    </div>
  );
}

function buildHarnessSummary(trace: AgentTrace | null): HarnessSummaryItem[] {
  const spans = flattenTraceSpans(trace);
  const byType = (type: TraceSpan["type"]) =>
    spans.filter((span) => span.type === type && !isGraphNodeSpan(span));
  const modelInputs = byType("model_input");
  const latestModelInput = modelInputs[modelInputs.length - 1];
  const modelMeta = latestModelInput?.metadata || {};
  const tools = byType("tool");
  const failedTools = tools.filter((span) => span.status === "error").length;
  const todo = byType("todo");
  const latestTodoDiff = (todo[todo.length - 1]?.metadata?.todo_diff || {}) as {
    added?: unknown[];
    updated?: unknown[];
    removed?: unknown[];
  };
  const memories = byType("memory");
  const skills = byType("skill");
  const subagents = byType("subagent");
  const permissions = byType("permission");

  return [
    {
      key: "model_input",
      type: "model_input",
      label: "Model Input",
      value: `${modelInputs.length} 次`,
      detail:
        latestModelInput && typeof modelMeta.message_count === "number"
          ? `${modelMeta.message_count} messages / ~${modelMeta.estimated_tokens || 0} tokens`
          : "等待模型调用",
      tone: "blue",
    },
    {
      key: "tools",
      type: "tool",
      label: "Tools",
      value: `${tools.length} 次`,
      detail: failedTools > 0 ? `${failedTools} 次失败` : "无失败",
      tone: "slate",
    },
    {
      key: "todo",
      type: "todo",
      label: "Todo",
      value: `${todo.length} 次`,
      detail: `+${latestTodoDiff.added?.length || 0} / ~${latestTodoDiff.updated?.length || 0} / -${latestTodoDiff.removed?.length || 0}`,
      tone: "green",
    },
    {
      key: "memory",
      type: "memory",
      label: "Memory",
      value: `${memories.length} 次`,
      detail: memories.length ? "读写/检索已记录" : "未触发",
      tone: "amber",
    },
    {
      key: "skill",
      type: "skill",
      label: "Skill",
      value: `${skills.length} 次`,
      detail: skills.length ? skills.map((span) => span.name).slice(0, 2).join(", ") : "未触发",
      tone: "indigo",
    },
    {
      key: "permission",
      type: "permission",
      label: "Permission",
      value: `${permissions.length} 次`,
      detail: permissions.length ? permissions.map((span) => span.name).slice(0, 2).join(", ") : "未触发",
      tone: "amber",
    },
    {
      key: "subagent",
      type: "subagent",
      label: "Subagent",
      value: `${subagents.length} 次`,
      detail: subagents.length ? subagents.map((span) => span.name).slice(0, 2).join(", ") : "未触发",
      tone: "fuchsia",
    },
  ];
}

function flattenTraceSpans(trace: AgentTrace | null): TraceSpan[] {
  if (!trace) return [];
  const result: TraceSpan[] = [];
  const seen = new Set<string>();
  const walk = (span: TraceSpan) => {
    if (seen.has(span.id)) return;
    seen.add(span.id);
    result.push(span);
    for (const child of span.children || []) walk(child);
  };
  for (const span of trace.spans || []) walk(span);
  return result;
}

function lineageIds(span: TraceSpan, allSpans: TraceSpan[]): string[] {
  const byId = new Map(allSpans.map((item) => [item.id, item]));
  const ids: string[] = [];
  let current: TraceSpan | undefined = span;
  while (current) {
    ids.push(current.id);
    current = current.parent_id ? byId.get(current.parent_id) : undefined;
  }
  return ids;
}

function SpanDetail({ span, allSpans = [], onClose }: { span: TraceSpan; allSpans?: TraceSpan[]; onClose: () => void }) {
  const isModelInput = span.type === "model_input";
  const isTool = span.type === "tool";
  return (
    <div className="rounded-lg border border-blue-100 bg-white p-3">
      <div className="mb-2 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <SpanIcon type={span.type} status={span.status} />
            <h3 className="truncate text-[13px] font-semibold text-slate-800">{spanLabel(span)}</h3>
            <TypePill span={span} />
          </div>
          <p className="mt-1 text-[11px] text-slate-400">
            {typeLabel(span.type)} · {span.status}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded-full border border-slate-200 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-50"
        >
          关闭
        </button>
      </div>
      {isModelInput ? (
        <ModelInputDetail span={span} allSpans={allSpans} />
      ) : isTool ? (
        <ToolSpanDetail span={span} allSpans={allSpans} />
      ) : (
        <div className="grid gap-2 lg:grid-cols-2">
          <DetailBlock title="Output" value={span.output} />
          <DetailBlock title="Metadata" value={span.metadata || {}} />
          {span.input !== null && span.input !== undefined && (
            <DetailBlock title="Input" value={span.input} />
          )}
        </div>
      )}
    </div>
  );
}

function ToolSpanDetail({ span, allSpans = [] }: { span: TraceSpan; allSpans?: TraceSpan[] }) {
  const ragSpans = allSpans.filter((item) => item.parent_id === span.id && item.type === "rag");
  const databaseSpans = traceDescendantsOf(span.id, allSpans, "database");
  const ragStages = ragStageCounts(ragSpans);
  return (
    <div className="space-y-2">
      {databaseSpans.length > 0 && <DatabaseTraceSummary spans={databaseSpans} />}
      {ragSpans.length > 0 && (
        <div className="rounded-lg border border-emerald-100 bg-emerald-50/50 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <p className="text-[11px] font-bold text-emerald-800">RAG 细节已折叠</p>
              <p className="mt-0.5 text-[10px] text-emerald-700/70">
                主流程只展示 tool output 和后续 model input；检索内部步骤保留在原始 trace 数据里。
              </p>
            </div>
            <span className="rounded-full bg-white px-2 py-0.5 text-[10px] font-semibold text-emerald-700 shadow-sm">
              {ragSpans.length} steps
            </span>
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {ragStages.map((item) => (
              <span key={item.stage} className="rounded-md border border-emerald-100 bg-white px-2 py-1 text-[10px] font-medium text-emerald-700">
                {item.stage}
                {item.count > 1 ? ` ×${item.count}` : ""}
              </span>
            ))}
          </div>
        </div>
      )}
      <div className="grid gap-2 lg:grid-cols-2">
        <DetailBlock title="Tool Input / Arguments" value={span.input} />
        <DetailBlock title="Tool Output / Result" value={span.output} />
        <DetailBlock title="Metadata" value={span.metadata || {}} />
      </div>
    </div>
  );
}

function traceDescendantsOf(parentId: string, allSpans: TraceSpan[], type?: string): TraceSpan[] {
  const byParent = new Map<string, TraceSpan[]>();
  allSpans.forEach((span) => {
    if (!span.parent_id) return;
    const children = byParent.get(span.parent_id) || [];
    children.push(span);
    byParent.set(span.parent_id, children);
  });

  const result: TraceSpan[] = [];
  const stack = [...(byParent.get(parentId) || [])];
  const seen = new Set<string>();
  while (stack.length) {
    const span = stack.shift()!;
    if (seen.has(span.id)) continue;
    seen.add(span.id);
    if (!type || span.type === type) result.push(span);
    stack.push(...(byParent.get(span.id) || []));
  }
  return result.sort((a, b) => spanEventOrder(a) - spanEventOrder(b));
}

function DatabaseTraceSummary({ spans }: { spans: TraceSpan[] }) {
  const ordered = [...spans].sort((a, b) => spanEventOrder(a) - spanEventOrder(b));
  const stages = databaseStageCounts(ordered);

  return (
    <div className="rounded-lg border border-blue-100 bg-blue-50/40 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="text-[11px] font-bold text-blue-900">问数 Trace 明细</p>
          <p className="mt-0.5 text-[10px] text-blue-700/70">
            表 Router、Vanna 召回、SQL 生成和执行明细都在这里。
          </p>
        </div>
        <span className="rounded-full bg-white px-2 py-0.5 text-[10px] font-semibold text-blue-700 shadow-sm">
          {ordered.length} spans
        </span>
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {stages.map((item) => (
          <span key={item.stage} className="rounded-md border border-blue-100 bg-white px-2 py-1 text-[10px] font-medium text-blue-700">
            {databaseStageLabel(item.stage)}
            {item.count > 1 ? ` ×${item.count}` : ""}
          </span>
        ))}
      </div>
      <div className="mt-3 space-y-2">
        {ordered.map((item) => (
          <DatabaseTraceCard key={item.id} span={item} />
        ))}
      </div>
    </div>
  );
}

function DatabaseTraceCard({ span }: { span: TraceSpan }) {
  const stage = databaseStage(span);
  const payload = objectFromUnknown(span.output);
  const summary = databasePayloadSummary(stage, payload);

  return (
    <div className="rounded-lg border border-blue-100 bg-white p-2">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded-md bg-blue-50 px-2 py-0.5 text-[10px] font-bold text-blue-700">
            {databaseStageLabel(stage)}
          </span>
          <span className="text-[10px] text-slate-400">{span.name}</span>
        </div>
        <span className="text-[10px] text-slate-400">{span.status}</span>
      </div>
      {summary.length > 0 && (
        <div className="mb-2 grid gap-1.5 sm:grid-cols-2 lg:grid-cols-3">
          {summary.map((item) => (
            <MiniMetric key={item.label} label={item.label} value={item.value} />
          ))}
        </div>
      )}
      {stage === "vanna_entities" && <DatabaseEntitiesPreview value={payload} />}
      <DetailBlock title="完整详情" value={span.output} collapsible defaultCollapsed copyable />
    </div>
  );
}

function DatabaseEntitiesPreview({ value }: { value: unknown }) {
  const root = objectFromUnknown(value);
  const groups = Array.isArray(root.groups) ? root.groups : [];
  const total = Number(root.total ?? 0);
  const strategy = typeof root.strategy === "string" ? root.strategy : "";
  const topK = objectFromUnknown(root.top_k);
  const defaultTopK = topK.default;
  if (groups.length === 0 && total === 0) return null;

  return (
    <div className="mb-2 space-y-2">
      <div className="rounded-lg border border-blue-100 bg-blue-50/50 p-2">
        <div className="mb-1 flex items-center justify-between gap-2">
          <span className="text-[10px] font-bold text-blue-700">按类型召回</span>
          <span className="text-[10px] text-blue-400">
            {total} 个{defaultTopK ? ` · 默认 Top ${String(defaultTopK)}` : ""}
          </span>
        </div>
        <div className="flex flex-wrap gap-1">
          {groups.map((rawGroup, index) => {
            const group = objectFromUnknown(rawGroup);
            const type = databaseEntityItemName(group.type ?? `类型 ${index + 1}`);
            const count = Number(group.count ?? 0);
            const top = group.top_k;
            return (
              <span key={`${type}-${index}`} className="rounded-full bg-white px-2 py-0.5 text-[10px] font-semibold text-blue-700">
                {type} · {count}{top ? `/${String(top)}` : ""}
              </span>
            );
          })}
        </div>
        {strategy && <div className="mt-1 text-[10px] text-blue-300">{strategy}</div>}
      </div>
      <div className="grid gap-2 lg:grid-cols-2">
        {groups.map((rawGroup, groupIndex) => {
          const group = objectFromUnknown(rawGroup);
          const entityType = databaseEntityItemName(group.type ?? `类型 ${groupIndex + 1}`);
          const column = typeof group.column === "string" ? group.column : "";
          const items = (Array.isArray(group.items) ? group.items : []).slice(0, 8);
          return (
            <div key={`${entityType}-${groupIndex}`} className="rounded-lg border border-slate-100 bg-slate-50 p-2">
              <div className="mb-1 flex items-center justify-between gap-2">
                <span className="text-[10px] font-bold text-slate-700">{entityType}</span>
                <span className="text-[10px] text-slate-400">
                  Top {items.length}
                  {group.top_k ? `/${String(group.top_k)}` : ""}
                </span>
              </div>
              {column && <div className="mb-1 truncate text-[10px] text-slate-400">{column}</div>}
              <div className="space-y-1">
                {items.map((rawItem, index) => {
                  const name = databaseEntityItemName(rawItem);
                  const score = databaseEntityItemScore(rawItem);
                  return (
                    <div
                      key={`${entityType}-${name}-${index}`}
                      className="flex items-center justify-between gap-2 rounded-md bg-white px-2 py-1 text-[10px]"
                    >
                      <span className="truncate text-slate-600">{name}</span>
                      {score && <span className="shrink-0 font-mono text-slate-400">{score}</span>}
                    </div>
                  );
                })}
                {items.length === 0 && <div className="rounded-md bg-white px-2 py-1 text-[10px] text-slate-400">无命中实体</div>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function databaseEntityItemName(value: unknown): string {
  if (value === null || value === undefined) return "-";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  const item = objectFromUnknown(value);
  for (const key of ["canonical_name", "name", "entity_type", "type", "label", "value"]) {
    const candidate = item[key];
    if (typeof candidate === "string" || typeof candidate === "number") return String(candidate);
  }
  return formatTraceSummaryValue(value);
}

function databaseEntityItemScore(value: unknown): string | null {
  const item = objectFromUnknown(value);
  const rawScore = item.score ?? item.similarity ?? item.distance;
  if (rawScore === undefined || rawScore === null || rawScore === "") return null;
  const numericScore = Number(rawScore);
  if (Number.isFinite(numericScore)) return numericScore.toFixed(4);
  return String(rawScore);
}

function databaseStageCounts(spans: TraceSpan[]): Array<{ stage: string; count: number }> {
  const counts = new Map<string, number>();
  spans.forEach((span) => {
    const stage = databaseStage(span);
    counts.set(stage, (counts.get(stage) || 0) + 1);
  });
  return Array.from(counts.entries()).map(([stage, count]) => ({ stage, count }));
}

function databaseStage(span: TraceSpan): string {
  const rawStage = typeof span.metadata?.database_stage === "string" ? span.metadata.database_stage : span.name.replace(/^database\./, "");
  return rawStage || "database";
}

function databaseStageLabel(stage: string): string {
  const labels: Record<string, string> = {
    router: "表 Router",
    vanna_references: "Vanna 资料",
    vanna_entities: "实体召回",
    sql_generation: "SQL 生成",
    sql_execution: "SQL 执行",
  };
  return labels[stage] || stage;
}

function databasePayloadSummary(stage: string, payload: Record<string, unknown>): Array<{ label: string; value: string }> {
  if (stage === "router") {
    return [
      { label: "selected_tables", value: formatUnknownList(payload.selected_tables) },
      { label: "available_tables", value: formatUnknownList(payload.available_tables) },
    ].filter((item) => item.value !== "-");
  }
  if (stage === "sql_generation") {
    return [
      { label: "source", value: formatTraceSummaryValue(payload.source) },
      { label: "tables", value: formatUnknownList(payload.tables) },
    ].filter((item) => item.value !== "-");
  }
  if (stage === "sql_execution") {
    return [
      { label: "rows", value: String(payload.row_count ?? "-") },
      { label: "columns", value: formatUnknownList(payload.columns) },
      { label: "limited", value: String(payload.limited ?? "-") },
    ].filter((item) => item.value !== "-");
  }
  return [];
}

function formatUnknownList(value: unknown): string {
  if (!Array.isArray(value)) return "-";
  if (value.length === 0) return "0";
  return value.slice(0, 5).map((item) => formatTraceSummaryValue(item)).join(", ") + (value.length > 5 ? ` +${value.length - 5}` : "");
}

function formatTraceSummaryValue(value: unknown): string {
  if (value === null || value === undefined) return "-";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return formatUnknownList(value);
  const objectValue = objectFromUnknown(value);
  for (const key of ["name", "source_name", "database", "database_source_id", "id", "type"]) {
    const candidate = objectValue[key];
    if (typeof candidate === "string" || typeof candidate === "number") return String(candidate);
  }
  return prettyValue(value);
}

function objectFromUnknown(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return value as Record<string, unknown>;
}

function ragStageCounts(spans: TraceSpan[]): Array<{ stage: string; count: number }> {
  const counts = new Map<string, number>();
  spans.forEach((span) => {
    const rawStage = typeof span.metadata?.rag_stage === "string" ? span.metadata.rag_stage : span.name.replace(/^rag\./, "");
    const group = rawStage.split(".", 1)[0] || rawStage || "rag";
    counts.set(group, (counts.get(group) || 0) + 1);
  });
  return Array.from(counts.entries()).map(([stage, count]) => ({ stage, count }));
}

function ModelInputDetail({ span, allSpans = [] }: { span: TraceSpan; allSpans?: TraceSpan[] }) {
  const output = (span.output || {}) as {
    messages_preview?: Array<{
      role?: string;
      name?: string | null;
      chars?: number;
      estimated_tokens?: number;
      tool_call_count?: number;
      tool_calls?: ModelInputToolCall[];
      preview?: string;
      content?: string;
    }>;
    model_call_contract?: ModelCallContract;
  };
  const messages = output.messages_preview || [];
  const systemMessages = messages.filter((message) => isSystemMessage(message));
  const conversationMessages = messages.filter((message) => !isSystemMessage(message));
  const contract = output.model_call_contract;
  const metadata = span.metadata || {};
  const fingerprints = contract?.fingerprints || ((metadata.fingerprints || {}) as ModelCallFingerprints);
  const toolSchemas = contract?.tool_schemas || [];
  const previousToolSchemas = modelInputContractFromSpan(previousModelInputSpan(span, allSpans))?.tool_schemas || [];
  const toolDiff = compareToolSchemas(previousToolSchemas, toolSchemas);
  const assembly = contract?.assembly || fallbackModelInputAssembly(messages, toolSchemas, contract?.params || {}, metadata, fingerprints);
  const visibleModelParams = Object.entries(contract?.params || {})
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .slice(0, 6);

  // System prompts are long and noisy; collapse them by default.
  const [collapsed, setCollapsed] = useState<Set<number>>(() => {
    const initial = new Set<number>();
    messages.forEach((message, index) => {
      if ((message.role || "").toLowerCase().includes("system")) {
        initial.add(index);
      }
    });
    return initial;
  });
  const toggle = (index: number) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  };

  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-4">
        <MiniMetric label="Messages" value={String(metadata.message_count ?? messages.length)} />
        <MiniMetric label="Tokens" value={`~${metadata.estimated_tokens ?? 0}`} />
        <MiniMetric label="Tools" value={String(metadata.tool_schema_count ?? 0)} />
        <MiniMetric label="LLM 调用方式" value={formatModelCallBoundary(metadata.capture_boundary)} />
      </div>
      {visibleModelParams.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {visibleModelParams.map(([key, value]) => (
            <span key={key} className="rounded-md border border-slate-100 bg-slate-50 px-2 py-1 text-[10px] font-medium text-slate-500">
              {key}: {String(value)}
            </span>
          ))}
        </div>
      )}

      <ModelInputAssemblyPanel assembly={assembly} />

      <ModelInputSection
        icon={<Cpu className="h-4 w-4" />}
        title="System prompt"
        subtitle={`${systemMessages.length} messages · ${metadata.system_prompt_chars ?? contract?.system_prompt_chars ?? 0} chars`}
        hash={fingerprints.system_prompt_hash}
      >
        {systemMessages.length ? (
          <div className="space-y-2">
            {systemMessages.map((message, index) => {
              const messageIndex = messages.indexOf(message);
              const isCollapsed = collapsed.has(messageIndex);
              const text = modelMessageText(message);
              return (
                <div key={`system-${messageIndex}`} className="rounded-lg border border-slate-100 bg-slate-50 p-3">
                  <div className="mb-2 flex flex-wrap items-center gap-2">
                    <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase ${roleClass(message.role)}`}>
                      {roleLabel(message.role)}
                    </span>
                    <span className="text-[10px] text-slate-400">{message.chars || 0} chars</span>
                    <span className="text-[10px] text-slate-400">~{message.estimated_tokens || 0} tokens</span>
                    <button
                      type="button"
                      onClick={() => toggle(messageIndex)}
                      className="ml-auto flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-medium text-slate-500 hover:bg-slate-200/60"
                    >
                      {isCollapsed ? (
                        <>
                          <ChevronRight className="h-3 w-3" /> 展开
                        </>
                      ) : (
                        <>
                          <ChevronDown className="h-3 w-3" /> 折叠
                        </>
                      )}
                    </button>
                  </div>
                  {isCollapsed ? (
                    <div className="rounded-md bg-white/60 px-3 py-2 text-[11px] italic text-slate-400">
                      System prompt 已折叠 · 展开后可滚动查看完整内容
                    </div>
                  ) : (
                    <MessageContentBlock message={message} maxClass="max-h-80" />
                  )}
                </div>
              );
            })}
          </div>
        ) : (
          <EmptyModelInputBlock text="本次模型输入没有单独的 system message。" />
        )}
      </ModelInputSection>

      <ModelInputSection
        icon={<MessageSquare className="h-4 w-4" />}
        title="Messages"
        subtitle={`${conversationMessages.length} messages`}
        hash={fingerprints.messages_hash}
      >
        {conversationMessages.length ? (
          <div className="space-y-2">
            {conversationMessages.map((message, index) => (
              <ModelMessageCard key={`${message.role}-${index}-${message.chars}`} message={message} />
            ))}
          </div>
        ) : (
          <EmptyModelInputBlock text="没有非 system 消息。" />
        )}
      </ModelInputSection>

      <ModelInputSection
        icon={<Network className="h-4 w-4" />}
        title="Tools"
        subtitle={`${toolSchemas.length} schemas`}
        hash={fingerprints.tool_schema_hash}
        defaultCollapsed
        collapsedText="Tools schema 已折叠 · 展开后查看完整工具列表"
      >
        <ToolSchemaSummary tools={toolSchemas} diff={toolDiff} />
      </ModelInputSection>
    </div>
  );
}

interface ModelInputToolCall {
  id?: string;
  name?: string;
  args?: unknown;
}

interface ModelCallFingerprints {
  messages_hash?: string;
  system_prompt_hash?: string;
  tool_schema_hash?: string;
}

interface ModelCallContract {
  message_count?: number;
  system_prompt_chars?: number;
  estimated_tokens?: number;
  tool_schema_count?: number;
  tool_schemas?: ModelToolSchema[];
  params?: Record<string, unknown>;
  fingerprints?: ModelCallFingerprints;
  assembly?: ModelInputAssembly;
}

interface ModelInputAssembly {
  boundary?: string;
  principle?: string;
  sections?: ModelInputAssemblySection[];
}

interface ModelInputAssemblySection {
  key?: string;
  label?: string;
  source?: string;
  count?: number;
  chars?: number;
  hash?: string;
  included?: boolean;
  roles?: Record<string, number>;
  tool_call_count?: number;
  binding?: {
    mode?: string;
    kwargs?: Record<string, unknown>;
  };
  params?: Record<string, unknown>;
  notes?: string[];
}

interface ToolSchemaDiff {
  added: ModelToolSchema[];
  removed: ModelToolSchema[];
  changed: Array<{ before: ModelToolSchema; after: ModelToolSchema }>;
}

interface ModelToolSchema {
  name: string;
  description?: string;
  schema_hash?: string;
}

function formatModelCallBoundary(value: unknown): string {
  const raw = String(value || "").replace("ModelClientChatModel.", "");
  const labelMap: Record<string, string> = {
    _astream: "异步流式",
    _stream: "同步流式",
    _agenerate: "异步非流式",
    _generate: "同步非流式",
  };
  return labelMap[raw] || raw || "-";
}

function fallbackModelInputAssembly(
  messages: ModelMessagePreview[],
  tools: ModelToolSchema[],
  params: Record<string, unknown>,
  metadata: Record<string, unknown>,
  fingerprints: ModelCallFingerprints
): ModelInputAssembly {
  const systemMessages = messages.filter((message) => isSystemMessage(message));
  const conversationMessages = messages.filter((message) => !isSystemMessage(message));
  const roleCounts: Record<string, number> = {};
  messages.forEach((message) => {
    const role = String(message.role || "unknown").toLowerCase();
    roleCounts[role] = (roleCounts[role] || 0) + 1;
  });
  const toolCallCount = messages.reduce((sum, message) => sum + (message.tool_call_count || 0), 0);
  return {
    boundary: String(metadata.capture_boundary || "model boundary"),
    principle: "final_payload_entering_llm",
    sections: [
      {
        key: "system_prompt",
        label: "System prompt",
        source: "LangChain messages with role=system",
        count: systemMessages.length,
        chars: Number(metadata.system_prompt_chars || 0),
        hash: fingerprints.system_prompt_hash,
        included: systemMessages.length > 0,
        notes: ["System prompt is read from the final structured messages payload."],
      },
      {
        key: "messages",
        label: "Messages",
        source: "LangChain messages payload",
        count: conversationMessages.length,
        chars: conversationMessages.reduce((sum, message) => sum + (message.chars || 0), 0),
        hash: fingerprints.messages_hash,
        included: conversationMessages.length > 0,
        roles: roleCounts,
        tool_call_count: toolCallCount,
      },
      {
        key: "tools",
        label: "Tools",
        source: "ModelClient.bind_tools structured schema",
        count: tools.length,
        hash: fingerprints.tool_schema_hash,
        included: tools.length > 0,
        binding: { mode: "bind_tools", kwargs: {} },
        notes: ["Tool schemas are separate from system prompt text."],
      },
      {
        key: "params",
        label: "Model params",
        source: "ModelClient runtime configuration",
        count: Object.values(params).filter((value) => value !== null && value !== undefined && value !== "").length,
        included: Object.keys(params).length > 0,
        params,
      },
    ],
  };
}

function ModelInputAssemblyPanel({ assembly }: { assembly: ModelInputAssembly }) {
  const sections = assembly.sections || [];
  if (!sections.length) return null;
  return (
    <section className="rounded-xl border border-blue-100 bg-blue-50/40 p-3">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 items-start gap-2">
          <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-white text-blue-600 shadow-sm">
            <Split className="h-4 w-4" />
          </span>
          <div className="min-w-0">
            <p className="text-[13px] font-bold text-slate-900">Model input 组装细则</p>
            <p className="mt-0.5 text-[10px] text-slate-500">
              {formatModelCallBoundary(assembly.boundary)}
              {assembly.principle ? ` · ${assembly.principle}` : ""}
            </p>
          </div>
        </div>
        <span className="rounded-full bg-white px-2 py-1 text-[10px] font-semibold text-blue-600 shadow-sm">
          {sections.length} parts
        </span>
      </div>
      <div className="grid gap-2 lg:grid-cols-4">
        {sections.map((section) => (
          <ModelInputAssemblyCard key={section.key || section.label} section={section} />
        ))}
      </div>
    </section>
  );
}

function ModelInputAssemblyCard({ section }: { section: ModelInputAssemblySection }) {
  const notes = section.notes || [];
  const metrics = [
    typeof section.count === "number" ? `${section.count} item${section.count === 1 ? "" : "s"}` : null,
    typeof section.chars === "number" ? `${section.chars} chars` : null,
    typeof section.tool_call_count === "number" && section.tool_call_count > 0 ? `${section.tool_call_count} tool calls` : null,
  ].filter(Boolean);
  return (
    <div className="min-w-0 rounded-lg border border-blue-100 bg-white p-2.5">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-[11px] font-bold text-slate-900">{section.label || section.key || "part"}</p>
          {section.source && <p className="mt-0.5 line-clamp-2 text-[10px] leading-relaxed text-slate-500">{section.source}</p>}
        </div>
        <span
          className={`shrink-0 rounded-full px-1.5 py-0.5 text-[9px] font-bold ${
            section.included ? "bg-emerald-50 text-emerald-700" : "bg-slate-100 text-slate-400"
          }`}
        >
          {section.included ? "included" : "empty"}
        </span>
      </div>
      {metrics.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {metrics.map((metric) => (
            <span key={metric} className="rounded-md bg-slate-50 px-1.5 py-0.5 text-[9px] font-semibold text-slate-500">
              {metric}
            </span>
          ))}
        </div>
      )}
      {section.hash && <p className="mt-2 font-mono text-[9px] text-slate-300">hash {shortHash(section.hash)}</p>}
      {section.binding?.mode && (
        <p className="mt-2 rounded-md bg-emerald-50 px-2 py-1 text-[10px] font-medium text-emerald-700">
          binding: {section.binding.mode}
        </p>
      )}
      {notes.length > 0 && (
        <ul className="mt-2 space-y-1">
          {notes.slice(0, 2).map((note) => (
            <li key={note} className="text-[10px] leading-relaxed text-slate-400">
              {note}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ModelInputSection({
  icon,
  title,
  subtitle,
  hash,
  defaultCollapsed = false,
  collapsedText,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  subtitle?: string;
  hash?: string;
  defaultCollapsed?: boolean;
  collapsedText?: string;
  children: React.ReactNode;
}) {
  const [isCollapsed, setIsCollapsed] = useState(defaultCollapsed);
  return (
    <section className="rounded-xl border border-slate-100 bg-slate-50/50 p-3">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 items-start gap-2">
          <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-white text-blue-600 shadow-sm">
            {icon}
          </span>
          <div className="min-w-0">
            <p className="text-[13px] font-bold text-slate-900">{title}</p>
            {subtitle && <p className="mt-0.5 text-[10px] text-slate-400">{subtitle}</p>}
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          {hash && (
            <span className="rounded-md border border-slate-100 bg-white px-2 py-1 font-mono text-[10px] font-semibold text-slate-500">
              {shortHash(hash)}
            </span>
          )}
          {defaultCollapsed && (
            <button
              type="button"
              onClick={() => setIsCollapsed((value) => !value)}
              className="flex items-center gap-1 rounded-md border border-slate-100 bg-white px-2 py-1 text-[10px] font-semibold text-slate-500 hover:bg-slate-50"
            >
              {isCollapsed ? <ChevronRight className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
              {isCollapsed ? "展开" : "折叠"}
            </button>
          )}
        </div>
      </div>
      {isCollapsed ? (
        <div className="rounded-lg border border-dashed border-slate-200 bg-white/70 px-3 py-3 text-[11px] text-slate-400">
          {collapsedText || "已折叠，展开后查看完整内容。"}
        </div>
      ) : (
        children
      )}
    </section>
  );
}

function EmptyModelInputBlock({ text }: { text: string }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-200 bg-white/70 px-3 py-4 text-[11px] text-slate-400">
      {text}
    </div>
  );
}

function ModelMessageCard({ message }: { message: ModelMessagePreview }) {
  return (
    <div className="rounded-lg border border-slate-100 bg-white p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase ${roleClass(message.role)}`}>
          {roleLabel(message.role)}
        </span>
        {message.name && <span className="text-[10px] text-slate-400">{message.name}</span>}
        <span className="text-[10px] text-slate-400">{message.chars || 0} chars</span>
        <span className="text-[10px] text-slate-400">~{message.estimated_tokens || 0} tokens</span>
        {Boolean(message.tool_call_count) && (
          <span className="text-[10px] text-slate-400">{message.tool_call_count} tool calls</span>
        )}
      </div>
      <MessageContentBlock message={message} maxClass="max-h-72" />
    </div>
  );
}

function MessageContentBlock({ message, maxClass }: { message: ModelMessagePreview; maxClass: string }) {
  const text = modelMessageText(message);
  const toolCalls = message.tool_calls || [];
  const hasToolCalls = toolCalls.length > 0 || Boolean(message.tool_call_count);
  if (!text.trim() && !hasToolCalls) {
    return (
      <div className="rounded-md border border-dashed border-slate-200 bg-slate-50 px-3 py-3 text-[11px] text-slate-400">
        这条消息没有文本内容。
      </div>
    );
  }
  return (
    <div className={`${maxClass} overflow-auto rounded-md bg-slate-50/70 p-3 text-[12px] leading-relaxed text-slate-700`}>
      {text.trim() ? (
        <div className="markdown-content trace-markdown-content">
          <ReactMarkdown remarkPlugins={markdownRemarkPlugins}>{text}</ReactMarkdown>
        </div>
      ) : (
        <ModelToolCalls calls={toolCalls} count={message.tool_call_count || 0} />
      )}
      {text.trim() && hasToolCalls && (
        <div className="mt-3">
          <ModelToolCalls calls={toolCalls} count={message.tool_call_count || 0} />
        </div>
      )}
    </div>
  );
}

function ToolSchemaSummary({ tools, diff }: { tools: ModelToolSchema[]; diff: ToolSchemaDiff }) {
  const hasDiff = diff.added.length > 0 || diff.removed.length > 0 || diff.changed.length > 0;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-1.5">
        {hasDiff ? (
          <>
            {diff.added.slice(0, 8).map((tool) => (
              <StateFieldChip key={`tool-added-${tool.name}`} tone="green" label={`+ ${tool.name}`} />
            ))}
            {diff.removed.slice(0, 8).map((tool) => (
              <StateFieldChip key={`tool-removed-${tool.name}`} tone="red" label={`- ${tool.name}`} />
            ))}
            {diff.changed.slice(0, 8).map(({ after }) => (
              <StateFieldChip key={`tool-changed-${after.name}`} tone="blue" label={`~ ${after.name}`} detail="schema" />
            ))}
          </>
        ) : (
          <span className="rounded-md border border-slate-100 bg-white px-2 py-1 text-[10px] font-semibold text-slate-500">
            与上一轮相比没有 tool schema 变化
          </span>
        )}
      </div>
      {tools.length ? (
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {tools.map((tool, index) => {
            const change = toolSchemaChangeLabel(tool, diff);
            return (
              <div key={`${tool.name}-${index}`} className="min-w-0 rounded-lg border border-slate-100 bg-white p-2">
                <div className="flex items-center justify-between gap-2">
                  <p className="truncate text-[11px] font-bold text-slate-800">{tool.name}</p>
                  {change && (
                    <span className={`rounded-full px-1.5 py-0.5 text-[9px] font-bold ${toolChangeClass(change)}`}>
                      {toolChangeText(change)}
                    </span>
                  )}
                </div>
                {tool.description && (
                  <p className="mt-1 line-clamp-2 text-[10px] leading-relaxed text-slate-400">{tool.description}</p>
                )}
                {tool.schema_hash && (
                  <p className="mt-1 font-mono text-[9px] text-slate-300">schema {shortHash(tool.schema_hash)}</p>
                )}
              </div>
            );
          })}
        </div>
      ) : (
        <EmptyModelInputBlock text="本次模型输入没有 tool schemas。" />
      )}
    </div>
  );
}

function toolSchemaChangeLabel(tool: ModelToolSchema, diff: ToolSchemaDiff): "added" | "changed" | null {
  if (diff.added.some((item) => item.name === tool.name)) return "added";
  if (diff.changed.some((item) => item.after.name === tool.name)) return "changed";
  return null;
}

function toolChangeClass(change: "added" | "changed") {
  return change === "added"
    ? "bg-emerald-50 text-emerald-700"
    : "bg-blue-50 text-blue-700";
}

function toolChangeText(change: "added" | "changed") {
  return change === "added" ? "新增" : "schema changed";
}

function previousModelInputSpan(span: TraceSpan, allSpans: TraceSpan[]): TraceSpan | null {
  const currentOrder = spanEventOrder(span);
  const previous = allSpans
    .filter((item) => item.type === "model_input" && item.id !== span.id && spanEventOrder(item) < currentOrder)
    .sort((a, b) => spanEventOrder(b) - spanEventOrder(a))[0];
  return previous || null;
}

function modelInputContractFromSpan(span: TraceSpan | null): ModelCallContract | null {
  if (!span?.output || typeof span.output !== "object") return null;
  const output = span.output as { model_call_contract?: ModelCallContract };
  return output.model_call_contract || null;
}

function compareToolSchemas(before: ModelToolSchema[], after: ModelToolSchema[]): ToolSchemaDiff {
  const beforeByName = new Map(before.map((tool) => [tool.name, tool]));
  const afterByName = new Map(after.map((tool) => [tool.name, tool]));
  const added = after.filter((tool) => !beforeByName.has(tool.name));
  const removed = before.filter((tool) => !afterByName.has(tool.name));
  const changed = after
    .filter((tool) => beforeByName.has(tool.name))
    .map((tool) => ({ before: beforeByName.get(tool.name)!, after: tool }))
    .filter(({ before: previous, after: current }) => Boolean(previous.schema_hash && current.schema_hash && previous.schema_hash !== current.schema_hash));
  return { added, removed, changed };
}

interface ModelMessagePreview {
  role?: string;
  name?: string | null;
  chars?: number;
  estimated_tokens?: number;
  tool_call_count?: number;
  tool_calls?: ModelInputToolCall[];
  preview?: string;
  content?: string;
}

function isSystemMessage(message: ModelMessagePreview): boolean {
  return (message.role || "").toLowerCase().includes("system");
}

function HashMetric({ label, value }: { label: string; value?: string }) {
  return (
    <div className="min-w-0 rounded-lg border border-blue-100 bg-white px-3 py-2">
      <div className="text-[10px] font-semibold uppercase text-blue-400">{label}</div>
      <div className="mt-0.5 truncate font-mono text-[12px] font-semibold text-slate-700">
        {value || "-"}
      </div>
    </div>
  );
}

function shortHash(value: string): string {
  return value.length > 8 ? value.slice(0, 8) : value;
}

function ModelToolCalls({ calls, count }: { calls: ModelInputToolCall[]; count: number }) {
  if (calls.length === 0) {
    return (
      <div className="rounded-md border border-slate-100 bg-slate-50 px-3 py-2 text-[11px] text-slate-500">
        模型发起了 {count} 个 tool call；此历史快照未保存工具名和参数，重新发起请求后会显示明细。
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {calls.map((call, index) => (
        <div key={`${call.id || call.name || "tool"}-${index}`} className="rounded-lg border border-slate-100 bg-slate-50 p-2">
          <div className="mb-1 flex flex-wrap items-center gap-2">
            <span className="rounded bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-700">
              {call.name || "tool"}
            </span>
            {call.id && <span className="text-[10px] text-slate-400">{call.id}</span>}
          </div>
          <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-md bg-white px-2 py-1.5 text-[11px] leading-relaxed text-slate-600">
            {prettyValue(call.args ?? {})}
          </pre>
        </div>
      ))}
    </div>
  );
}

function MiniMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-lg border border-slate-100 bg-slate-50 px-3 py-2">
      <div className="text-[10px] font-semibold uppercase text-slate-400">{label}</div>
      <div className="mt-0.5 truncate text-[12px] font-semibold text-slate-700">{value}</div>
    </div>
  );
}

function DetailBlock({
  title,
  value,
  collapsible = false,
  defaultCollapsed = false,
  copyable = false,
}: {
  title: string;
  value: unknown;
  collapsible?: boolean;
  defaultCollapsed?: boolean;
  copyable?: boolean;
}) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed);
  const [copied, setCopied] = useState(false);
  const formatted = prettyValue(value);

  const copyValue = async () => {
    try {
      await navigator.clipboard.writeText(formatted);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="min-w-0 rounded-lg border border-slate-100 bg-slate-50 p-2">
      <div className="mb-1 flex items-center justify-between gap-2">
        <button
          type="button"
          onClick={() => collapsible && setCollapsed((current) => !current)}
          className={`inline-flex min-w-0 items-center gap-1 text-left text-[10px] font-semibold uppercase tracking-normal text-slate-400 ${
            collapsible ? "transition hover:text-slate-600" : "cursor-default"
          }`}
        >
          {collapsible ? (
            collapsed ? <ChevronRight className="h-3 w-3 shrink-0" /> : <ChevronDown className="h-3 w-3 shrink-0" />
          ) : null}
          <span className="truncate">{title}</span>
        </button>
        <div className="flex shrink-0 items-center gap-1">
          {copyable ? (
            <button
              type="button"
              onClick={copyValue}
              className="inline-flex h-6 items-center gap-1 rounded-md border border-slate-200 bg-white px-2 text-[10px] font-semibold text-slate-500 transition hover:border-blue-100 hover:text-blue-700"
            >
              {copied ? <CheckCircle2 className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
              {copied ? "已复制" : "复制"}
            </button>
          ) : null}
          {collapsible ? (
            <button
              type="button"
              onClick={() => setCollapsed((current) => !current)}
              className="inline-flex h-6 items-center rounded-md border border-slate-200 bg-white px-2 text-[10px] font-semibold text-slate-500 transition hover:border-blue-100 hover:text-blue-700"
            >
              {collapsed ? "展开" : "收起"}
            </button>
          ) : null}
        </div>
      </div>
      {!collapsed && (
        <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words text-[11px] leading-relaxed text-slate-600">
          {formatted}
        </pre>
      )}
    </div>
  );
}

function prettyValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
      try {
        return JSON.stringify(JSON.parse(trimmed), null, 2);
      } catch {
        return value;
      }
    }
    return value;
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function roleLabel(role?: string): string {
  const normalized = (role || "message").toLowerCase();
  if (normalized === "system" || normalized === "systemmessage") return "System";
  if (normalized === "human" || normalized === "user" || normalized === "humanmessage") return "User";
  if (normalized === "ai" || normalized === "assistant" || normalized === "aimessage") return "Assistant";
  if (normalized === "tool" || normalized === "toolmessage") return "Tool";
  return role || "Message";
}

function roleClass(role?: string): string {
  const normalized = (role || "").toLowerCase();
  if (normalized.includes("system")) return "bg-slate-200 text-slate-700";
  if (normalized.includes("human") || normalized === "user") return "bg-blue-100 text-blue-700";
  if (normalized.includes("ai") || normalized === "assistant") return "bg-indigo-100 text-indigo-700";
  if (normalized.includes("tool")) return "bg-emerald-100 text-emerald-700";
  return "bg-slate-100 text-slate-600";
}

function messageScrollClass(role?: string): string {
  const normalized = (role || "").toLowerCase();
  if (normalized.includes("system")) return "max-h-72";
  return "max-h-[420px]";
}

function spanOutputPreview(span: TraceSpan): string {
  if (span.type === "model_input" && span.output && typeof span.output === "object") {
    const output = span.output as { messages_preview?: Array<{ role?: string; chars?: number; preview?: string; content?: string }> };
    const first = output.messages_preview?.[0];
    if (first) return `${first.role || "message"} · ${first.chars || 0} chars · ${modelMessageText(first).slice(0, 160)}`;
  }
  if (typeof span.output === "string") return span.output;
  return JSON.stringify(span.output as unknown);
}

function modelMessageText(message: { content?: string; preview?: string }): string {
  const raw = message.content || message.preview || "";
  return unwrapTextPartString(raw);
}

function unwrapTextPartString(raw: string): string {
  const text = String(raw || "");
  const trimmed = text.trim();
  if (!trimmed) return "";

  // Backward compatibility for traces saved before model-input normalization:
  // "{'type': 'text', 'text': '...'}" or "[{'type': 'text', 'text': '...'}]".
  if ((trimmed.startsWith("{") || trimmed.startsWith("[")) && trimmed.includes("'text'")) {
    const marker = "'text':";
    const markerIndex = trimmed.indexOf(marker);
    if (markerIndex >= 0) {
      let rest = trimmed.slice(markerIndex + marker.length).trim();
      if (rest.startsWith("'")) {
        rest = rest.slice(1);
        const endPatterns = ["'}]", "'}", "', '", "', \"", "',"];
        let end = rest.length;
        for (const pattern of endPatterns) {
          const idx = rest.lastIndexOf(pattern);
          if (idx >= 0) end = Math.min(end, idx);
        }
        return unescapePythonishString(rest.slice(0, end));
      }
      if (rest.startsWith('"')) {
        rest = rest.slice(1);
        const end = rest.lastIndexOf('"');
        return unescapePythonishString(end >= 0 ? rest.slice(0, end) : rest);
      }
    }
  }

  return unescapePythonishString(text);
}

function unescapePythonishString(value: string): string {
  return value
    .replace(/\\n/g, "\n")
    .replace(/\\t/g, "\t")
    .replace(/\\r/g, "\r")
    .replace(/\\'/g, "'")
    .replace(/\\"/g, '"')
    .replace(/\\\\/g, "\\");
}

function summaryToneClass(tone: HarnessSummaryItem["tone"]): string {
  const classes: Record<HarnessSummaryItem["tone"], string> = {
    blue: "border-blue-100 bg-blue-50 text-blue-700",
    slate: "border-slate-100 bg-slate-50 text-slate-700",
    green: "border-emerald-100 bg-emerald-50 text-emerald-700",
    indigo: "border-indigo-100 bg-indigo-50 text-indigo-700",
    fuchsia: "border-fuchsia-100 bg-fuchsia-50 text-fuchsia-700",
    amber: "border-amber-100 bg-amber-50 text-amber-700",
  };
  return classes[tone];
}

interface ActualFlowItem {
  id: string;
  type: TraceSpan["type"];
  status: TraceSpan["status"];
  label: string;
  subtitle: string;
  title: string;
  span?: TraceSpan;
  signatureOrder?: number;
  signature?: ActualFlowItem[];
  children?: ActualFlowItem[];
}

function buildActualFlow(spans: TraceSpan[]): ActualFlowItem[] {
  const priority: TraceSpan["type"][] = [
    "model_input",
    "llm",
    "reasoning",
    "tool",
    "permission",
    "memory",
    "skill",
    "subagent",
    "todo",
  ];
  const items = spans
    .filter((span) => priority.includes(span.type) && !isGraphNodeSpan(span))
    .sort((a, b) => {
      const byEventOrder = spanEventOrder(a) - spanEventOrder(b);
      if (byEventOrder !== 0) return byEventOrder;
      return a.started_at - b.started_at;
    })
    .map((span) => spanToFlowItem(span));
  return buildScopedActualFlow(items);
}

function buildActualFlowForTrace(trace: AgentTrace | null, spans: TraceSpan[]): ActualFlowItem[] {
  const base = buildActualFlow(spans);
  const invocations = (trace?.middleware_invocations || [])
    .map((rawInvocation) => middlewareInvocationFromRaw(rawInvocation, spans))
    .filter((invocation): invocation is MiddlewareHookInvocation => Boolean(invocation))
    .filter((invocation) => shouldShowInvocationInActualFlow(invocation))
    .sort((a, b) => actualFlowInvocationOrder(a, trace) - actualFlowInvocationOrder(b, trace));

  const withObservedInvocations = invocations.reduce((items, invocation) => {
    const markerSpan = middlewareInvocationMarkerSpan(invocation);
    const marker = middlewareInvocationFlowItem(invocation, markerSpan);
    return insertTopLevelFlowMarkerForInvocation(items, marker, invocation, trace);
  }, base);
  return addMountedWrapperSignatures(withObservedInvocations, trace);
}

function traceFlowDebugEnabled(): boolean {
  if (typeof window === "undefined") return false;
  try {
    const params = new URLSearchParams(window.location.search);
    return params.get("traceDebug") === "1" || window.localStorage.getItem("puddingclaw.traceDebug") === "1";
  } catch {
    return false;
  }
}

function emitTraceFlowDebug(trace: AgentTrace | null, spans: TraceSpan[], actualFlow: ActualFlowItem[]) {
  if (!trace || !traceFlowDebugEnabled()) return;
  const invocations = (trace.middleware_invocations || [])
    .map((rawInvocation) => middlewareInvocationFromRaw(rawInvocation, spans))
    .filter((invocation): invocation is MiddlewareHookInvocation => Boolean(invocation))
    .filter((invocation) => shouldShowInvocationInActualFlow(invocation));
  const hookOrder = trace.runtime_inventory?.middleware?.hooks?.before_agent || [];
  const table = invocations.map((invocation) => {
    const raw = invocation.invocation;
    return {
      hook: invocation.hook,
      title: invocation.title,
      middleware: Array.isArray(raw?.middleware) ? raw.middleware.join(", ") : "",
      coverage: raw?.metadata?.coverage,
      semantic_order: raw?.metadata?.semantic_order,
      stack_order: middlewareInvocationStackOrder(invocation, trace),
      sequence: invocation.sequence,
      computed_order: actualFlowInvocationOrder(invocation, trace),
    };
  });
  const flow = actualFlow.map((item, index) => ({
    index,
    type: item.type,
    label: item.label,
    title: item.title,
    hook: normalizeHookName(item.span?.metadata?.hook),
    middleware: Array.isArray(item.span?.metadata?.middleware) ? item.span?.metadata?.middleware.join(", ") : undefined,
  }));
  console.groupCollapsed(`[PuddingClaw trace flow debug] ${trace.trace_id}`);
  console.table(hookOrder.map((entry) => ({
    hook: "before_agent",
    name: entry.name,
    execution_order: entry.execution_order,
    stack_order: entry.stack_order || entry.order,
  })));
  console.table(table);
  console.table(flow);
  console.groupEnd();
}

function actualFlowInvocationOrder(invocation: MiddlewareHookInvocation, trace: AgentTrace | null): number {
  const semantic =
    numberFromUnknown(invocation.invocation?.metadata?.semantic_order) ??
    hookSemanticOrder(invocation.hook);
  const stackOrder = middlewareInvocationStackOrder(invocation, trace);
  return semantic * 100000 + stackOrder * 100 + invocation.sequence;
}

function hookSemanticOrder(hook: string): number {
  const order: Record<string, number> = {
    before_agent: 10,
    before_model: 20,
    wrap_model_call: 30,
    after_model: 40,
    wrap_tool_call: 50,
    after_agent: 60,
  };
  return order[hook] ?? 99;
}

function middlewareInvocationStackOrder(invocation: MiddlewareHookInvocation, trace: AgentTrace | null): number {
  const inventory = trace?.runtime_inventory as TraceRuntimeInventory | undefined;
  const invocationNames = new Set(
    [
      ...(Array.isArray(invocation.invocation?.middleware) ? invocation.invocation.middleware : []),
      invocation.invocation?.metadata?.proxied_middleware,
      invocation.title,
      invocation.title.split(".", 1)[0],
    ]
      .map((value) => String(value || "").trim())
      .filter(Boolean)
  );
  const hook = invocation.hook;
  const hookEntries = inventory?.middleware?.hooks?.[hook] || [];
  const hookMatch = hookEntries.find((entry) => invocationNames.has(String(entry.name || "").trim()));
  if (hookMatch) return middlewareStackOrderFromEntry(hookMatch);

  const stack = inventory?.middleware?.stack || [];
  const match = stack.find((entry) => {
    const name = String(entry.name || "").trim();
    const hooks = Array.isArray(entry.hooks) ? entry.hooks : [];
    return invocationNames.has(name) && hooks.includes(hook);
  });
  return middlewareStackOrderFromEntry(match);
}

function middlewareInvocationFromRaw(
  rawInvocation: TraceMiddlewareInvocation,
  spans: TraceSpan[]
): MiddlewareHookInvocation | null {
  const hook = normalizeHookName(rawInvocation.hook);
  if (!hook) return null;
  const linkedSpans = spansForMiddlewareInvocation(rawInvocation, spans);
  return {
    id: rawInvocation.id,
    hook,
    sequence: rawInvocation.sequence,
    sequenceLabel: `第 ${Number(rawInvocation.invocation_index || 0) + 1} 次`,
    title: rawInvocation.title || hook,
    note: rawInvocation.category ? `${rawInvocation.category} · ${rawInvocation.status}` : rawInvocation.status,
    previous: stringFromUnknown(rawInvocation.flow_ref?.previous) || previousHookBoundaryLabel(hook),
    next: stringFromUnknown(rawInvocation.flow_ref?.next) || nextHookBoundaryLabel(hook),
    reason: "后端 middleware_invocation 已明确记录该 hook 触发。",
    flow: compactEvidence([
      stringFromUnknown(rawInvocation.flow_ref?.previous) || previousHookBoundaryLabel(hook),
      hook,
      stringFromUnknown(rawInvocation.flow_ref?.next) || nextHookBoundaryLabel(hook),
    ]),
    spans: linkedSpans,
    effects: [],
    invocation: rawInvocation,
    evidence: (rawInvocation.evidence || []) as string[],
  };
}

function shouldShowInvocationInActualFlow(invocation: MiddlewareHookInvocation): boolean {
  return ["before_agent", "before_model", "wrap_model_call", "after_model", "wrap_tool_call", "after_agent"].includes(
    invocation.hook
  );
}

function filterActualFlow(
  items: ActualFlowItem[],
  selectedType: TraceSpan["type"] | null
): ActualFlowItem[] {
  if (!selectedType) return items;
  return items.flatMap((item) => {
    const filteredChildren = filterActualFlow(item.children || [], selectedType);
    const selfMatches = flowItemMatchesType(item, selectedType);
    if (!selfMatches && filteredChildren.length === 0) return [];
    return [
      {
        ...item,
        children: filteredChildren.length > 0 ? filteredChildren : item.children,
      },
    ];
  });
}

function flowItemMatchesType(item: ActualFlowItem, selectedType: TraceSpan["type"]): boolean {
  return item.type === selectedType || item.span?.type === selectedType;
}

function spanToFlowItem(span: TraceSpan): ActualFlowItem {
  return {
    id: span.id,
    type: span.type,
    status: span.status,
    label: spanLabel(span),
    subtitle: typeLabel(span.type),
    title: span.name,
    span,
  };
}

function graphModelFlowItem(item: ActualFlowItem, index: number, input?: ActualFlowItem): ActualFlowItem {
  const displaySpan = item.span ? withDisplayModelCallIndex(item.span, index - 1) : undefined;
  const modelCall: ActualFlowItem = {
    ...item,
    span: displaySpan,
    label: `第 ${index} 次模型调用`,
    subtitle: "LLM 调用",
  };
  const children = input ? [input] : [missingModelInputFlowItem(item, index)];
  if (hasUsefulFlowDetail(modelCall)) {
    children.push(modelCall);
  }
  return {
    id: `graph-model-${item.id}`,
    type: "graph",
    status: item.status,
    label: "graph.model",
    subtitle: "LangGraph model 节点",
    title: `graph.model · ${modelCall.label}`,
    span: displaySpan,
    children,
  };
}

function missingModelInputFlowItem(item: ActualFlowItem, index: number): ActualFlowItem {
  return {
    id: `missing-model-input-${item.id}`,
    type: "custom",
    status: item.status,
    label: "模型输入未捕获",
    subtitle: `第 ${index} 次模型调用没有对应 model.input 快照`,
    title: "缺少模型输入快照",
  };
}

function withDisplayModelCallIndex(span: TraceSpan, index: number): TraceSpan {
  return {
    ...span,
    metadata: {
      ...(span.metadata || {}),
      display_model_call_index: index,
    },
  };
}

function hasUsefulFlowDetail(item: ActualFlowItem): boolean {
  if (!item.span) return true;
  if (item.type !== "llm") return true;
  return Boolean(item.span.input) || Boolean(item.span.output);
}

function buildScopedActualFlow(items: ActualFlowItem[]): ActualFlowItem[] {
  const flow: ActualFlowItem[] = [];
  const modelInputsByIndex = new Map<number, ActualFlowItem>();
  const unindexedModelInputs: ActualFlowItem[] = [];
  for (const item of items) {
    if (item.type !== "model_input") continue;
    const callIndex = flowItemModelCallIndex(item);
    if (callIndex !== null) modelInputsByIndex.set(callIndex, item);
    else unindexedModelInputs.push(item);
  }
  const consumedModelInputIds = new Set<string>();
  let pendingModelInput: ActualFlowItem | undefined;
  let currentModel: ActualFlowItem | undefined;
  let modelIndex = 0;
  let index = 0;

  while (index < items.length) {
    const item = items[index];

    if (item.type === "model_input") {
      if (flowItemModelCallIndex(item) === null) {
        pendingModelInput = item;
      }
      index += 1;
      continue;
    }

    if (item.type === "llm") {
      modelIndex += 1;
      const callIndex = flowItemModelCallIndex(item) ?? modelIndex - 1;
      const indexedInput = modelInputsByIndex.get(callIndex);
      const fallbackInput =
        indexedInput ||
        pendingModelInput ||
        unindexedModelInputs.find((candidate) => !consumedModelInputIds.has(candidate.id));
      if (fallbackInput) consumedModelInputIds.add(fallbackInput.id);
      currentModel = graphModelFlowItem(item, modelIndex, fallbackInput);
      if (pendingModelInput?.id === fallbackInput?.id) pendingModelInput = undefined;
      flow.push(currentModel);
      index += 1;
      continue;
    }

    if (item.type === "reasoning" && currentModel) {
      currentModel.children = [...(currentModel.children || []), item];
      index += 1;
      continue;
    }

    if (item.type !== "tool") {
      flow.push(item);
      index += 1;
      continue;
    }

    const children: ActualFlowItem[] = [];
    const startIndex = index;
    let status: TraceSpan["status"] = "completed";

    while (index < items.length && items[index].type === "tool") {
      const child = items[index];
      children.push(child);
      if (child.status === "error") status = "error";
      else if (child.status === "running" && status !== "error") status = "running";
      index += 1;
    }

    flow.push({
      id: `graph-tools-${startIndex}-${children.map((child) => child.id).join("-")}`,
      type: "graph",
      status,
      label: "graph.tools",
      subtitle: "LangGraph tools 节点",
      title: children.map((child) => child.label).join(", "),
      span: children[0]?.span,
      children,
    });
  }

  const orphanInputs = items.filter(
    (item) => item.type === "model_input" && !consumedModelInputIds.has(item.id)
  );
  if (pendingModelInput && !consumedModelInputIds.has(pendingModelInput.id)) {
    flow.unshift(pendingModelInput);
  } else if (orphanInputs.length) {
    flow.unshift(...orphanInputs);
  }
  return flow;
}

function spanEventOrder(span: TraceSpan): number {
  const order = span.metadata?.event_order;
  return typeof order === "number" ? order : Number.POSITIVE_INFINITY;
}

function isGraphNodeSpan(span: TraceSpan): boolean {
  return span.type === "graph" || span.name.startsWith("graph.");
}

function ActualFlow({
  items,
  expanded,
  onToggle,
  selectedSpanId,
  onSelect,
}: {
  items: ActualFlowItem[];
  expanded: Set<string>;
  onToggle: (id: string) => void;
  selectedSpanId: string | null;
  onSelect: (span: TraceSpan) => void;
}) {
  return (
    <div>
      {items.length === 0 ? (
        <p className="rounded-lg bg-slate-50 px-3 py-3 text-[12px] text-slate-400">
          暂无实际运行节点。
        </p>
      ) : (
        <div className="space-y-1.5">
          {items.map((item) => (
            <div key={item.id}>
              <FlowNodeCard
                item={item}
                expanded={expanded.has(item.id)}
                selectedSpanId={selectedSpanId}
                onToggle={() => onToggle(item.id)}
                onSelect={onSelect}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function FlowNodeCard({
  item,
  expanded,
  selectedSpanId,
  onToggle,
  onSelect,
}: {
  item: ActualFlowItem;
  expanded: boolean;
  selectedSpanId: string | null;
  onToggle: () => void;
  onSelect: (span: TraceSpan) => void;
}) {
  const visibleChildren = item.children || [];
  const signatureItems = item.signature || [];
  const hasChildren = visibleChildren.length > 0;
  const isSelected = selectedSpanId === item.span?.id;
  const isMiddlewareMarker = item.type === "middleware" && Boolean(item.span?.metadata?.middleware_invocation_id);
  return (
    <div
      data-flow-span-id={item.span?.id || undefined}
      className={`w-full rounded-lg border transition-colors ${
        isMiddlewareMarker
          ? "border-violet-200 bg-violet-50 text-violet-800 shadow-sm"
          : isSelected
            ? "border-blue-200 bg-blue-50 text-blue-700"
            : "border-slate-100 bg-slate-50 text-slate-700"
      }`}
    >
      <button
        type="button"
        onClick={() => {
          if (hasChildren) onToggle();
          if (item.span) onSelect(item.span);
        }}
        className="flex min-h-10 w-full items-center gap-2 px-3 py-2 text-left hover:bg-white/60"
        title={item.title}
      >
        <SpanIcon type={item.type} status={item.status} />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            <div className="truncate text-[12px] font-semibold">{item.label}</div>
            {isMiddlewareMarker && (
              <span className="shrink-0 rounded-full bg-white px-2 py-0.5 text-[9px] font-bold text-violet-700 shadow-sm">
                middleware hook
              </span>
            )}
          </div>
          {hasChildren || signatureItems.length ? (
            <div className="mt-1 flex flex-wrap gap-1">
              {signatureItems.map((child) => (
                <button
                  key={child.id}
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation();
                    if (child.span) onSelect(child.span);
                  }}
                  className="rounded border border-violet-100 bg-violet-50 px-1.5 py-0.5 text-[9px] font-semibold text-violet-700 hover:bg-violet-100"
                  title={child.title}
                >
                  {child.label}
                </button>
              ))}
              {visibleChildren.map((child) => (
                <span
                  key={child.id}
                  className="rounded border border-slate-200 bg-white px-1.5 py-0.5 text-[9px] font-medium text-slate-500"
                >
                  {child.label}
                </span>
              ))}
            </div>
          ) : (
            <div className="truncate text-[10px] opacity-70">{item.subtitle}</div>
          )}
        </div>
        {hasChildren ? (
          expanded ? (
            <ChevronDown className="h-3.5 w-3.5 shrink-0 text-slate-400" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5 shrink-0 text-slate-400" />
          )
        ) : null}
      </button>
      {expanded && hasChildren && (
        <div className="space-y-1 border-t border-slate-100 bg-white/70 px-3 py-2">
          {visibleChildren.map((child) => (
            <button
              key={child.id}
              type="button"
              data-flow-span-id={child.span?.id || undefined}
              onClick={() => child.span && onSelect(child.span)}
              className={`flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left hover:bg-slate-50 ${
                child.type === "middleware"
                  ? selectedSpanId === child.span?.id
                    ? "bg-violet-100 text-violet-800"
                    : "bg-violet-50 text-violet-700"
                  : selectedSpanId === child.span?.id
                    ? "bg-blue-50 text-blue-700"
                    : "text-slate-600"
              }`}
              title={child.title}
            >
              <SpanIcon type={child.type} status={child.status} />
              <div className="min-w-0">
                <div className="truncate text-[11px] font-semibold">{child.label}</div>
                <div className="truncate text-[10px] opacity-70">{child.subtitle}</div>
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function spanLabel(span: TraceSpan): string {
  if (span.type === "graph" && typeof span.metadata?.graph_node === "string") {
    return `graph: ${span.metadata.graph_node}`;
  }
  if (span.type === "model_input") return "模型输入快照";
  if (span.type === "rag") return `RAG: ${String(span.metadata?.rag_stage || span.name.replace(/^rag\./, ""))}`;
  if (span.type === "llm") {
    const displayIndex = span.metadata?.display_model_call_index;
    if (typeof displayIndex === "number") return `第 ${displayIndex + 1} 次模型调用`;
    const metadataIndex = span.metadata?.model_call_index;
    if (typeof metadataIndex === "number") return `第 ${metadataIndex + 1} 次模型调用`;
    const match = span.name.match(/^model\.(\d+)$/);
    if (match) return `第 ${Number(match[1]) + 1} 次模型调用`;
  }
  return span.name;
}

function typeLabel(type: TraceSpan["type"]): string {
  const labels: Record<TraceSpan["type"], string> = {
    root: "Agent Run",
    llm: "模型调用",
    model_input: "模型输入",
    tool: "工具",
    reasoning: "推理内容",
    todo: "Todo",
    custom: "自定义事件",
    rag: "RAG",
    graph: "编译图节点",
    middleware: "Middleware",
    memory: "Memory",
    skill: "Skill",
    subagent: "Subagent",
    permission: "权限",
  };
  return labels[type] || type;
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

// ── LangGraph SVG renderer ────────────────────────────────────

interface LayoutNode {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
  label: string;
  labelLines: string[];
  kind: string;
  hook?: string;
}

interface LayoutEdge {
  source: string;
  target: string;
  route: "forward" | "lateral" | "feedback" | "self" | "to_tool" | "from_tool";
  channel: number;
  optional?: boolean;
}

interface LayoutScope {
  x: number;
  y: number;
  width: number;
  height: number;
  label: string;
}

type GraphActivity = Map<string, "running" | "completed" | "error">;

function GraphSvg({
  graph,
  activeNode,
  graphActivity,
  maxGraphHeight,
  minCanvasWidth,
}: {
  graph: GraphStructure;
  activeNode: string | null;
  graphActivity: GraphActivity;
  maxGraphHeight: number;
  minCanvasWidth: number;
}) {
  const layout = useMemo(() => computeGraphLayout(graph), [graph]);

  const nodeById = useMemo(() => {
    const map = new Map<string, LayoutNode>();
    for (const n of layout.nodes) map.set(n.id, n);
    return map;
  }, [layout]);

  const padding = 20;
  const svgWidth = layout.width + padding * 2;
  const svgHeight = layout.height + padding * 2;

  return (
    <div
      className="overflow-auto rounded-lg border border-slate-100 bg-slate-50/60 p-2"
      style={{ maxHeight: maxGraphHeight }}
    >
      <svg
        width={Math.max(svgWidth, minCanvasWidth)}
        height={svgHeight}
        viewBox={`0 0 ${svgWidth} ${svgHeight}`}
        className="block"
        role="img"
        aria-label="LangGraph execution graph"
      >
        <defs>
          <marker
            id="arrowhead"
            markerWidth="8"
            markerHeight="6"
            refX="7"
            refY="3"
            orient="auto"
          >
            <polygon points="0 0, 8 3, 0 6" fill="#cbd5e1" />
          </marker>
        </defs>
        <g transform={`translate(${padding}, ${padding})`}>
          {layout.scope && (
            <g>
              <rect
                x={layout.scope.x}
                y={layout.scope.y}
                width={layout.scope.width}
                height={layout.scope.height}
                rx={14}
                fill="#ffffff"
                stroke="#e2e8f0"
                strokeWidth={1.2}
              />
              <text
                x={layout.scope.x + 16}
                y={layout.scope.y + 24}
                className="text-[11px] font-semibold tracking-normal"
                fill="#64748b"
              >
                {layout.scope.label}
              </text>
            </g>
          )}
          {layout.edges.map((edge, idx) => {
            const source = nodeById.get(edge.source);
            const target = nodeById.get(edge.target);
            if (!source || !target) return null;
            const isHot = source.id === activeNode || target.id === activeNode;
            return (
              <EdgeLine
                key={`${edge.source}-${edge.target}-${idx}`}
                source={source}
                target={target}
                route={edge.route}
                channel={edge.channel}
                optional={edge.optional}
                isHot={isHot}
              />
            );
          })}
          {layout.nodes.map((node) => (
            <GraphNodeRect
              key={node.id}
              node={node}
              status={graphActivity.get(node.id)}
              isActive={node.id === activeNode}
            />
          ))}
        </g>
      </svg>
    </div>
  );
}

function EdgeLine({
  source,
  target,
  route,
  channel,
  optional,
  isHot,
}: {
  source: LayoutNode;
  target: LayoutNode;
  route: LayoutEdge["route"];
  channel: number;
  optional?: boolean;
  isHot: boolean;
}) {
  const radius = 10;
  let d = "";

  if (route === "self") {
    const sx = source.x + source.width;
    const sy = source.y + source.height / 2;
    const loopX = sx + 34 + channel * 12;
    d = [
      `M ${sx} ${sy}`,
      `C ${loopX} ${sy - 22}, ${loopX} ${sy + 22}, ${sx} ${sy + 16}`,
    ].join(" ");
  } else if (route === "to_tool") {
    const sx = source.x + source.width / 2;
    const sy = source.y + source.height;
    const tx = target.x + target.width / 2;
    const ty = target.y;
    const midY = Math.max(sy + 26, (sy + ty) / 2);
    d = [
      `M ${sx} ${sy}`,
      `L ${sx} ${midY}`,
      `L ${tx} ${midY}`,
      `L ${tx} ${ty}`,
    ].join(" ");
  } else if (route === "from_tool") {
    const sx = source.x + source.width;
    const sy = source.y + source.height / 2;
    const tx = target.x;
    const ty = target.y + target.height / 2;
    const laneX = Math.max(source.x + source.width + 40, target.x - 48);
    d = [
      `M ${sx} ${sy}`,
      `L ${laneX} ${sy}`,
      `L ${laneX} ${ty}`,
      `L ${tx} ${ty}`,
    ].join(" ");
  } else if (route === "feedback") {
    const sx = source.x + source.width;
    const sy = source.y + source.height / 2;
    const tx = target.x + target.width;
    const ty = target.y + target.height / 2;
    const laneX = Math.max(source.x + source.width, target.x + target.width) + 42 + channel * 18;
    d = [
      `M ${sx} ${sy}`,
      `L ${laneX - radius} ${sy}`,
      `Q ${laneX} ${sy} ${laneX} ${sy - radius}`,
      `L ${laneX} ${ty + radius}`,
      `Q ${laneX} ${ty} ${laneX - radius} ${ty}`,
      `L ${tx} ${ty}`,
    ].join(" ");
  } else if (route === "lateral") {
    const leftToRight = source.x <= target.x;
    const sx = leftToRight ? source.x + source.width : source.x;
    const tx = leftToRight ? target.x : target.x + target.width;
    const sy = source.y + source.height / 2;
    const ty = target.y + target.height / 2;
    const midX = (sx + tx) / 2;
    d = [
      `M ${sx} ${sy}`,
      `L ${midX - Math.sign(tx - sx) * radius} ${sy}`,
      `Q ${midX} ${sy} ${midX} ${sy + Math.sign(ty - sy) * radius}`,
      `L ${midX} ${ty - Math.sign(ty - sy) * radius}`,
      `Q ${midX} ${ty} ${midX + Math.sign(tx - sx) * radius} ${ty}`,
      `L ${tx} ${ty}`,
    ].join(" ");
  } else {
    const sx = source.x + source.width / 2;
    const sy = source.y + source.height;
    const tx = target.x + target.width / 2;
    const ty = target.y;
    const midY = Math.max(sy + 22, (sy + ty) / 2);
    d = [
      `M ${sx} ${sy}`,
      `L ${sx} ${midY - radius}`,
      `Q ${sx} ${midY} ${sx + Math.sign(tx - sx) * radius} ${midY}`,
      `L ${tx - Math.sign(tx - sx) * radius} ${midY}`,
      `Q ${tx} ${midY} ${tx} ${midY + radius}`,
      `L ${tx} ${ty}`,
    ].join(" ");
  }

  return (
    <path
      d={d}
      fill="none"
      stroke={isHot ? "#3b82f6" : "#cbd5e1"}
      strokeWidth={isHot ? 2.2 : 1.35}
      strokeDasharray={optional ? "5 5" : undefined}
      markerEnd="url(#arrowhead)"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  );
}

function GraphNodeRect({
  node,
  isActive,
  status,
}: {
  node: LayoutNode;
  isActive: boolean;
  status?: "running" | "completed" | "error";
}) {
  const rx = 8;
  const palette = graphNodePalette(node.kind, status, isActive);
  return (
    <g transform={`translate(${node.x}, ${node.y})`}>
      <title>{node.id}</title>
      <rect
        width={node.width}
        height={node.height}
        rx={rx}
        fill={palette.fill}
        stroke={palette.stroke}
        strokeWidth={isActive ? 2 : 1}
      />
      {status === "running" && (
        <circle cx={12} cy={12} r={3.5} fill="#2563eb" className="animate-pulse" />
      )}
      <text
        x={node.width / 2}
        y={node.height / 2}
        dominantBaseline="middle"
        textAnchor="middle"
        className="text-[11px] font-medium tracking-normal"
        fill={palette.text}
      >
        {node.labelLines.map((line, index) => (
          <tspan
            key={`${node.id}-${index}`}
            x={node.width / 2}
            dy={index === 0 ? (node.labelLines.length > 1 ? "-0.45em" : "0") : "1.15em"}
            className={index > 0 ? "text-[9px] font-normal" : undefined}
            fill={index > 0 ? palette.subtext : palette.text}
          >
            {line}
          </tspan>
        ))}
      </text>
    </g>
  );
}

function graphNodePalette(kind: string, status: "running" | "completed" | "error" | undefined, isActive: boolean) {
  if (status === "error") return { fill: "#fef2f2", stroke: "#ef4444", text: "#991b1b", subtext: "#b91c1c" };
  if (isActive || status === "running") return { fill: "#eff6ff", stroke: "#3b82f6", text: "#1d4ed8", subtext: "#2563eb" };
  if (status === "completed") return { fill: "#f0fdf4", stroke: "#86efac", text: "#166534", subtext: "#15803d" };
  if (kind === "middleware") return { fill: "#faf5ff", stroke: "#e9d5ff", text: "#6b21a8", subtext: "#7e22ce" };
  if (kind === "tool") return { fill: "#f8fafc", stroke: "#cbd5e1", text: "#334155", subtext: "#64748b" };
  if (kind === "model") return { fill: "#eef2ff", stroke: "#c7d2fe", text: "#3730a3", subtext: "#4f46e5" };
  if (kind === "agent") return { fill: "#f8fafc", stroke: "#cbd5e1", text: "#334155", subtext: "#64748b" };
  return { fill: "#ffffff", stroke: "#dbe3ef", text: "#334155", subtext: "#64748b" };
}

function computeGraphLayout(graph: GraphStructure): {
  nodes: LayoutNode[];
  edges: LayoutEdge[];
  width: number;
  height: number;
  scope?: LayoutScope;
} {
  const nodeHeight = 52;
  const levelGap = 92;
  const siblingGap = 56;

  const nodeIds = new Set(graph.nodes.map((n) => n.id));
  const rawEdges = graph.edges.filter(
    (e) => nodeIds.has(e.source) && nodeIds.has(e.target)
  );

  const levels = computeTopologicalLevels(Array.from(nodeIds), rawEdges);
  const orderedIdsByLevel = orderGraphLevels(Array.from(nodeIds), rawEdges, levels);

  const nodesByLevel = new Map<number, string[]>();
  let maxLevel = 0;
  for (const id of orderedIdsByLevel) {
    const level = levels.get(id) || 0;
    if (!nodesByLevel.has(level)) nodesByLevel.set(level, []);
    nodesByLevel.get(level)!.push(id);
    maxLevel = Math.max(maxLevel, level);
  }

  const layoutNodes: LayoutNode[] = [];
  let maxWidth = 0;
  let maxHeight = 0;

  for (let level = 0; level <= maxLevel; level++) {
    const ids = nodesByLevel.get(level) || [];
    const levelNodes: LayoutNode[] = [];
    let rowWidth = 0;
    let x = 0;
    for (const id of ids) {
      const labelInfo = graphLabelInfo(id);
      const longestLine = Math.max(...labelInfo.lines.map((line) => line.length));
      const width = Math.max(128, Math.min(260, longestLine * 7 + 44));
      levelNodes.push({
        id,
        label: labelInfo.label,
        labelLines: labelInfo.lines,
        x,
        y: level * (nodeHeight + levelGap),
        width,
        height: nodeHeight,
        kind: labelInfo.kind,
        hook: labelInfo.hook,
      });
      x += width + siblingGap;
    }
    rowWidth = Math.max(0, x - siblingGap);
    levelNodes.forEach((node) => layoutNodes.push(node));
    maxWidth = Math.max(maxWidth, rowWidth);
    maxHeight = Math.max(maxHeight, level * (nodeHeight + levelGap) + nodeHeight);
  }

  for (let level = 0; level <= maxLevel; level++) {
    const nodes = layoutNodes.filter((node) => (levels.get(node.id) || 0) === level);
    const rowWidth = nodes.length
      ? Math.max(...nodes.map((node) => node.x + node.width))
      : 0;
    const offset = Math.max(0, (maxWidth - rowWidth) / 2);
    nodes.forEach((node) => {
      node.x += offset;
    });
  }

  const toolNode = layoutNodes.find((node) => node.id === "tools");
  const modelNode = layoutNodes.find((node) => node.id === "model");
  const afterModelNodes = layoutNodes.filter((node) => node.id.includes(".after_model"));
  if (toolNode && modelNode) {
    const anchor = afterModelNodes[0] || modelNode;
    toolNode.x = Math.max(0, anchor.x - toolNode.width - 70);
    toolNode.y = anchor.y + Math.max(70, anchor.height + 34);
    maxWidth = Math.max(maxWidth, toolNode.x + toolNode.width);
    maxHeight = Math.max(maxHeight, toolNode.y + toolNode.height);
  }

  const layoutEdges = rawEdges.map((edge, index) => {
    const sourceLevel = levels.get(edge.source) || 0;
    const targetLevel = levels.get(edge.target) || 0;
    return {
      ...edge,
      route:
        edge.source === edge.target
          ? "self"
          : edge.target === "tools"
            ? "to_tool"
          : edge.source === "tools"
            ? "from_tool"
          : targetLevel === sourceLevel
            ? "lateral"
          : targetLevel <= sourceLevel
            ? "feedback"
            : "forward",
      channel: index % 6,
      optional: edge.source === "tools" || edge.target === "tools",
    } satisfies LayoutEdge;
  });

  const scopeNodes = layoutNodes.filter(
    (node) => node.id !== "__start__" && !node.id.includes(".before_agent")
  );
  const scope = buildLayoutScope(scopeNodes);

  return {
    nodes: layoutNodes,
    edges: layoutEdges,
    width: Math.max(maxWidth + 120, scope ? scope.x + scope.width : 112),
    height: Math.max(maxHeight, scope ? scope.y + scope.height : nodeHeight),
    scope,
  };
}

function buildLayoutScope(nodes: LayoutNode[]): LayoutScope | undefined {
  if (nodes.length === 0) return undefined;
  const minX = Math.min(...nodes.map((node) => node.x));
  const minY = Math.min(...nodes.map((node) => node.y));
  const maxX = Math.max(...nodes.map((node) => node.x + node.width));
  const maxY = Math.max(...nodes.map((node) => node.y + node.height));
  return {
    x: Math.max(0, minX - 42),
    y: Math.max(0, minY - 58),
    width: maxX - minX + 84,
    height: maxY - minY + 96,
    label: "Agent runtime",
  };
}

function computeTopologicalLevels(
  ids: string[],
  edges: { source: string; target: string }[]
): Map<string, number> {
  const levels = new Map<string, number>();
  ids.forEach((id) => levels.set(id, id === "__start__" ? 0 : semanticGraphLevel(id)));

  for (let pass = 0; pass < ids.length; pass++) {
    let changed = false;
    for (const edge of edges) {
      if (edge.source === edge.target) continue;
      if (edge.source === "tools" || edge.target === "tools") {
        continue;
      }
      if (semanticGraphLevel(edge.target) <= semanticGraphLevel(edge.source) && edge.source !== "__start__") {
        continue;
      }
      const sourceLevel = levels.get(edge.source) || 0;
      const targetLevel = levels.get(edge.target) || 0;
      if (edge.target === "__start__") continue;
      if (edge.source === "__end__") continue;
      const nextLevel = sourceLevel + 1;
      if (nextLevel > targetLevel && nextLevel < ids.length + 6) {
        levels.set(edge.target, nextLevel);
        changed = true;
      }
    }
    if (!changed) break;
  }

  const endLevel = Math.max(...Array.from(levels.entries()).map(([id, level]) => (id === "__end__" ? 0 : level)), 0) + 1;
  if (levels.has("__end__")) levels.set("__end__", endLevel);
  return compactGraphLevels(ids, levels);
}

function compactGraphLevels(ids: string[], levels: Map<string, number>): Map<string, number> {
  const unique = Array.from(new Set(ids.map((id) => levels.get(id) || 0))).sort((a, b) => a - b);
  const remap = new Map(unique.map((level, index) => [level, index]));
  const compacted = new Map<string, number>();
  ids.forEach((id) => compacted.set(id, remap.get(levels.get(id) || 0) || 0));
  return compacted;
}

function orderGraphLevels(
  ids: string[],
  edges: { source: string; target: string }[],
  levels: Map<string, number>
): string[] {
  const idsByLevel = new Map<number, string[]>();
  ids.forEach((id) => {
    const level = levels.get(id) || 0;
    if (!idsByLevel.has(level)) idsByLevel.set(level, []);
    idsByLevel.get(level)!.push(id);
  });
  idsByLevel.forEach((levelIds) => {
    levelIds.sort((a, b) => graphNodeRank(a) - graphNodeRank(b) || a.localeCompare(b));
  });

  const predecessors = new Map<string, string[]>();
  edges.forEach((edge) => {
    if (!predecessors.has(edge.target)) predecessors.set(edge.target, []);
    predecessors.get(edge.target)!.push(edge.source);
  });

  const maxLevel = Math.max(...Array.from(idsByLevel.keys()), 0);
  for (let level = 1; level <= maxLevel; level++) {
    const prev = idsByLevel.get(level - 1) || [];
    const prevIndex = new Map(prev.map((id, index) => [id, index]));
    const current = idsByLevel.get(level) || [];
    current.sort((a, b) => {
      const aPred = predecessors.get(a) || [];
      const bPred = predecessors.get(b) || [];
      const aScore = aPred.length
        ? aPred.reduce((sum, id) => sum + (prevIndex.get(id) ?? graphNodeRank(id)), 0) / aPred.length
        : graphNodeRank(a);
      const bScore = bPred.length
        ? bPred.reduce((sum, id) => sum + (prevIndex.get(id) ?? graphNodeRank(id)), 0) / bPred.length
        : graphNodeRank(b);
      return aScore - bScore || graphNodeRank(a) - graphNodeRank(b) || a.localeCompare(b);
    });
  }

  return Array.from(idsByLevel.keys())
    .sort((a, b) => a - b)
    .flatMap((level) => idsByLevel.get(level) || []);
}

function graphLabelInfo(id: string): {
  label: string;
  lines: string[];
  kind: string;
  hook?: string;
} {
  if (id === "__start__") return { label: "__start__", lines: ["__start__"], kind: "agent" };
  if (id === "__end__") return { label: "__end__", lines: ["__end__"], kind: "agent" };
  if (id === "model") return { label: "Model", lines: ["Model"], kind: "model" };
  if (id === "tools") return { label: "Tools", lines: ["Tools"], kind: "tool" };

  const middlewareMatch = id.match(/^(.+Middleware)\.(before_agent|before_model|after_model|after_agent)$/);
  if (middlewareMatch) {
    const middlewareName = middlewareMatch[1];
    const hook = middlewareMatch[2];
    return {
      label: `${middlewareName} ${hook}`,
      lines: [middlewareName, hook],
      kind: graphNodeKind(id),
      hook,
    };
  }

  const compact = id.length > 28 ? `${id.slice(0, 25)}...` : id;
  return { label: compact, lines: [compact], kind: graphNodeKind(id) };
}

function semanticGraphLevel(id: string): number {
  if (id === "__start__") return 0;
  if (id.includes(".before_agent")) return 1;
  if (id.includes(".before_model")) return 2;
  if (id === "model") return 3;
  if (id.includes(".after_model")) return 4;
  if (id === "tools") return 4;
  if (id.includes(".after_agent")) return 5;
  if (id === "__end__") return 6;
  return 4;
}

function graphNodeKind(id: string): string {
  const lower = id.toLowerCase();
  if (id === "model") return "model";
  if (id === "__start__" || id === "__end__") return "agent";
  if (lower.includes("memory")) return "memory";
  if (lower.includes("skill")) return "skill";
  if (lower.includes("tool")) return "tool";
  if (lower.includes("todo")) return "middleware";
  if (lower.includes("middleware") || lower.includes("before_") || lower.includes("after_")) return "middleware";
  return "node";
}

function graphNodeRank(id: string): number {
  if (id === "__start__") return 0;
  if (id.includes("MemoryMiddleware")) return 1;
  if (id.includes("SkillsMiddleware")) return 2;
  if (id.includes("PatchToolCallsMiddleware")) return 3;
  if (id.includes("TodoListMiddleware")) return 4;
  if (id.toLowerCase().includes("model")) return 5;
  if (id.toLowerCase().includes("tool")) return 6;
  if (id === "__end__") return 99;
  return 10;
}

function buildGraphActivity(trace: AgentTrace | null, activeNode: string | null): GraphActivity {
  const activity: GraphActivity = new Map();
  const visit = (span: TraceSpan) => {
    const graphNode = typeof span.metadata?.graph_node === "string" ? span.metadata.graph_node : null;
    if (graphNode) {
      activity.set(graphNode, span.status === "error" ? "error" : span.status === "running" ? "running" : "completed");
    }
    for (const child of span.children || []) visit(child);
  };
  for (const span of trace?.spans || []) visit(span);
  if (activeNode) activity.set(activeNode, "running");
  return activity;
}
