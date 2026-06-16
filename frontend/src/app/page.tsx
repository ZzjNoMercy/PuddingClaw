"use client";

import { useCallback, useEffect, Suspense, useRef } from "react";
import { useSearchParams } from "next/navigation";
import { useApp } from "@/lib/store";
import Navbar from "@/components/layout/Navbar";
import Sidebar from "@/components/layout/Sidebar";
import ChatPanel from "@/components/chat/ChatPanel";
import InspectorPanel from "@/components/editor/InspectorPanel";
import ResizeHandle from "@/components/layout/ResizeHandle";

const MIN_SIDEBAR = 200;
const MIN_INSPECTOR = 280;
const MIN_CHAT = 360;

function ChatLayout() {
  const {
    sidebarOpen,
    toggleSidebar,
    inspectorOpen,
    toggleInspector,
    sidebarWidth,
    setSidebarWidth,
    inspectorWidth,
    setInspectorWidth,
    sendMessage,
  } = useApp();

  const searchParams = useSearchParams();
  const hasTriggeredRef = useRef(false);

  // ── Auto-trigger skill-creator from URL params ────────
  useEffect(() => {
    const trigger = searchParams.get("trigger");
    const action = searchParams.get("action");

    if (trigger === "skill-creator" && action === "create" && !hasTriggeredRef.current) {
      hasTriggeredRef.current = true;

      // Step 1: Load skill-creator
      const API_BASE = `http://${window.location.hostname}:9100/api`;
      fetch(`${API_BASE}/skills/load`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skill_name: "skill-creator" })
      })
        .catch((err) => {
          console.error("Failed to load skill-creator:", err);
        })
        .finally(() => {
          // Step 2: Send message after skill loaded (success or fail)
          setTimeout(() => {
            sendMessage("帮我创建一个新的 Skill");
          }, 500);
        });

      // Clear URL params to avoid re-triggering on refresh
      window.history.replaceState({}, "", "/");
    }
  }, [searchParams, sendMessage]);

  const handleSidebarResize = useCallback(
    (delta: number) => {
      setSidebarWidth((prev: number) => Math.max(MIN_SIDEBAR, prev + delta));
    },
    [setSidebarWidth]
  );

  const handleInspectorResize = useCallback(
    (delta: number) => {
      setInspectorWidth((prev: number) => Math.max(MIN_INSPECTOR, prev + delta));
    },
    [setInspectorWidth]
  );

  return (
    <div className="h-screen flex flex-col app-bg">
      <Navbar
        sidebarOpen={sidebarOpen}
        toggleSidebar={toggleSidebar}
        inspectorOpen={inspectorOpen}
        toggleInspector={toggleInspector}
        showPanelToggles
      />

      {/* Content area — flexbox layout with resizable panels */}
      <div className="flex-1 flex overflow-hidden">
        {/* Left sidebar */}
        <div
          className="glass-panel border-r border-black/[0.06] shrink-0 panel-transition overflow-hidden"
          style={{ width: sidebarOpen ? sidebarWidth : 0 }}
        >
          <div style={{ width: sidebarWidth, minWidth: MIN_SIDEBAR }} className="h-full">
            <Sidebar />
          </div>
        </div>

        {/* Left resize handle */}
        {sidebarOpen && (
          <ResizeHandle onResize={handleSidebarResize} direction="left" />
        )}

        {/* Chat — fills remaining space */}
        <div className="flex-1 overflow-hidden" style={{ minWidth: MIN_CHAT }}>
          <ChatPanel />
        </div>

        {/* Right resize handle */}
        {inspectorOpen && (
          <ResizeHandle onResize={handleInspectorResize} direction="right" />
        )}

        {/* Right inspector */}
        <div
          className="glass-panel border-l border-black/[0.06] shrink-0 panel-transition overflow-hidden"
          style={{ width: inspectorOpen ? inspectorWidth : 0 }}
        >
          <div style={{ width: inspectorWidth, minWidth: MIN_INSPECTOR }} className="h-full">
            <InspectorPanel />
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
