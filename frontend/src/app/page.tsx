"use client";

import { useEffect, Suspense, useRef } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useApp } from "@/lib/store";
import Navbar from "@/components/layout/Navbar";
import Sidebar from "@/components/layout/Sidebar";
import ChatPanel from "@/components/chat/ChatPanel";
import ResizeHandle from "@/components/layout/ResizeHandle";

const MIN_SIDEBAR = 200;
const MIN_CHAT = 360;

function ChatLayout() {
  const {
    sidebarOpen,
    toggleSidebar,
    sidebarWidth,
    setSidebarWidth,
    triggerSkillCreator,
  } = useApp();

  const searchParams = useSearchParams();
  const router = useRouter();
  const hasPrefilledRef = useRef(false);

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

  const handleSidebarResize = (delta: number) => {
    setSidebarWidth((prev: number) => Math.max(MIN_SIDEBAR, prev + delta));
  };

  return (
    <div className="h-screen flex flex-col app-bg">
      <Navbar
        sidebarOpen={sidebarOpen}
        toggleSidebar={toggleSidebar}
        showPanelToggles
      />

      {/* Content area — sidebar + chat only */}
      <div
        className="flex-1 flex overflow-hidden p-2 pt-0 gap-0"
        style={{
          "--workspace-content-shift": `${(sidebarOpen ? sidebarWidth : 0) / 2}px`,
        } as React.CSSProperties}
      >
        {/* Left sidebar */}
        <div
          className="workspace-sidebar-shell shrink-0 panel-transition overflow-hidden"
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
        <div className="flex-1 overflow-hidden workspace-chat-shell" style={{ minWidth: MIN_CHAT }}>
          <ChatPanel />
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
