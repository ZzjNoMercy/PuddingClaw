"use client";

import { useCallback, useEffect, Suspense, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useApp } from "@/lib/store";
import Navbar from "@/components/layout/Navbar";
import Sidebar from "@/components/layout/Sidebar";
import ChatPanel from "@/components/chat/ChatPanel";
import ResizeHandle from "@/components/layout/ResizeHandle";
import SourcesPanel from "@/components/citations/SourcesPanel";
import TraceDashboard from "@/components/agent/TraceDashboard";

const MIN_SIDEBAR = 200;
const MIN_CHAT = 360;
const MIN_INSPECTOR = 280;

function ChatLayout() {
  const {
    sidebarOpen,
    toggleSidebar,
    sidebarWidth,
    setSidebarWidth,
    triggerSkillCreator,
    inspectorOpen,
    setInspectorOpen,
    toggleInspector,
    inspectorWidth,
    setInspectorWidth,
    sessionId,
    sessions,
    workspaceView,
    setWorkspaceView,
    trace,
  } = useApp();

  const searchParams = useSearchParams();
  const router = useRouter();
  const hasPrefilledRef = useRef(false);
  const [inspectorAvailability, setInspectorAvailability] = useState({
    sessionId: "",
    available: false,
  });

  const handleInspectorAvailabilityChange = useCallback((available: boolean) => {
    setInspectorAvailability({ sessionId, available });
  }, [sessionId]);
  const inspectorAvailable =
    inspectorAvailability.sessionId === sessionId && inspectorAvailability.available;
  const visibleInspectorOpen = inspectorOpen && inspectorAvailable;

  const sessionTitle = useMemo(() => {
    if (sessionId === "default") return "新对话";
    const session = sessions.find((s) => s.id === sessionId);
    return session?.title || "新对话";
  }, [sessionId, sessions]);

  // ── Prefill skill-creator prompt from URL params ───────
  useEffect(() => {
    const trigger = searchParams.get("trigger");
    const action = searchParams.get("action");

    if (trigger === "skill-creator" && action === "create" && !hasPrefilledRef.current) {
      hasPrefilledRef.current = true;
      triggerSkillCreator();
      // Clear URL params so refresh doesn't re-prefill
      router.replace("/", { scroll: false });
    }
  }, [searchParams, triggerSkillCreator, router]);

  useEffect(() => {
    if (!inspectorAvailable) {
      setInspectorOpen(false);
    }
  }, [inspectorAvailable, setInspectorOpen]);

  const handleSidebarResize = (delta: number) => {
    setSidebarWidth((prev: number) => Math.max(MIN_SIDEBAR, prev + delta));
  };

  const handleInspectorResize = (delta: number) => {
    setInspectorWidth((prev: number) => Math.max(MIN_INSPECTOR, prev + delta));
  };

  const toggleTraceDashboard = () => {
    setWorkspaceView(workspaceView === "trace" ? "chat" : "trace");
    if (workspaceView !== "trace") {
      setInspectorOpen(false);
    }
  };

  return (
    <div className="h-screen app-bg">
      <div className="fixed left-3 top-3 z-[80]">
        <Navbar
          sidebarOpen={sidebarOpen}
          toggleSidebar={toggleSidebar}
          showPanelToggles
          compact
        />
      </div>

      <div
        className="flex h-full overflow-hidden"
        style={{
          "--workspace-content-shift": `${(sidebarOpen ? sidebarWidth : 0) / 2}px`,
        } as React.CSSProperties}
      >
        {/* Left sidebar */}
        <div
          className="workspace-sidebar-shell shrink-0 panel-transition overflow-hidden"
          style={{ width: sidebarOpen ? sidebarWidth : 0 }}
        >
          <div style={{ width: sidebarWidth, minWidth: MIN_SIDEBAR }} className="h-full flex flex-col">
            <div className="h-11 shrink-0" />
            <div className="flex-1 min-h-0 overflow-hidden">
              <Sidebar />
            </div>
          </div>
        </div>

        {/* Left resize handle */}
        {sidebarOpen && (
          <ResizeHandle onResize={handleSidebarResize} direction="left" />
        )}

        <div className="workspace-content-frame flex min-w-0 flex-1 flex-col overflow-hidden" style={{ minWidth: MIN_CHAT }}>
          <Navbar
            title={sessionTitle}
            inspectorOpen={visibleInspectorOpen}
            inspectorAvailable={inspectorAvailable}
            toggleInspector={toggleInspector}
            onToggleTrace={toggleTraceDashboard}
            traceSpanCount={trace?.spans?.length || 0}
            traceActive={workspaceView === "trace"}
            showPanelToggles
          />

          <div className="flex min-h-0 flex-1 overflow-hidden">
            {/* Chat — fills remaining space */}
            <div className="flex-1 overflow-hidden workspace-chat-shell" style={{ minWidth: MIN_CHAT }}>
              {workspaceView === "trace" ? <TraceDashboard /> : <ChatPanel />}
            </div>

            {visibleInspectorOpen && (
              <ResizeHandle onResize={handleInspectorResize} direction="right" />
            )}

            <div
              className="workspace-inspector-shell shrink-0 panel-transition overflow-hidden"
              style={{ width: visibleInspectorOpen ? inspectorWidth : 0 }}
            >
              <div style={{ width: inspectorWidth, minWidth: MIN_INSPECTOR }} className="h-full">
                <SourcesPanel onAvailabilityChange={handleInspectorAvailabilityChange} />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function Home() {
  return (
    <Suspense fallback={<div>Loading...</div>}>
      <ChatLayout />
    </Suspense>
  );
}
