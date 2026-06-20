"use client";

import Link from "next/link";
import {
  PanelLeft,
  PanelRight,
} from "lucide-react";

interface NavbarProps {
  sidebarOpen?: boolean;
  toggleSidebar?: () => void;
  inspectorOpen?: boolean;
  toggleInspector?: () => void;
  /** Hide sidebar/inspector toggles on non-chat pages */
  showPanelToggles?: boolean;
}

export default function Navbar({
  sidebarOpen,
  toggleSidebar,
  inspectorOpen,
  toggleInspector,
  showPanelToggles = false,
}: NavbarProps) {
  return (
    <nav className="glass-nav sticky top-0 z-50 h-11 flex items-center justify-between px-3">
      {/* Left — Sidebar toggle (chat page only) or spacer */}
      <div className="w-[120px] flex items-center">
        {showPanelToggles && toggleSidebar ? (
          <button
            onClick={toggleSidebar}
            className={`w-8 h-8 flex items-center justify-center rounded-lg transition-all ${
              sidebarOpen
                ? "bg-[#002fa7]/[0.08] text-[#002fa7] shadow-sm"
                : "text-gray-400 hover:text-gray-700 hover:bg-black/[0.04]"
            }`}
            title="切换侧栏"
          >
            <PanelLeft className="w-[16px] h-[16px]" />
          </button>
        ) : null}
      </div>

      {/* Center — Brand */}
      <div className="flex items-center min-w-0">
        {/* Brand */}
        <Link href="/" className="flex items-center gap-2">
          <div className="w-6 h-6 rounded-md bg-gradient-to-br from-[#002fa7] to-[#4070ff] flex items-center justify-center">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
              <path d="M12 2L2 7L12 12L22 7L12 2Z" fill="white" fillOpacity="0.9" />
              <path d="M2 17L12 22L22 17" stroke="white" strokeOpacity="0.7" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
              <path d="M2 12L12 17L22 12" stroke="white" strokeOpacity="0.85" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <span className="font-semibold text-[14px] tracking-tight text-gray-800">
            PuddingClaw
          </span>
        </Link>

      </div>

      {/* Right — Inspector toggle (chat page only) or spacer */}
      <div className="w-[120px] flex justify-end">
        {showPanelToggles && toggleInspector ? (
          <button
            onClick={toggleInspector}
            className={`w-8 h-8 flex items-center justify-center rounded-lg transition-all ${
              inspectorOpen
                ? "bg-[#002fa7]/[0.08] text-[#002fa7] shadow-sm"
                : "text-gray-400 hover:text-gray-700 hover:bg-black/[0.04]"
            }`}
            title="切换右侧面板"
          >
            <PanelRight className="w-[16px] h-[16px]" />
          </button>
        ) : null}
      </div>
    </nav>
  );
}
