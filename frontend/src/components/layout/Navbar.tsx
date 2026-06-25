"use client";

import { ChevronDown, PanelLeft, PanelRight } from "lucide-react";

interface NavbarProps {
  sidebarOpen?: boolean;
  toggleSidebar?: () => void;
  inspectorOpen?: boolean;
  toggleInspector?: () => void;
  /** Hide sidebar/inspector toggles on non-chat pages */
  showPanelToggles?: boolean;
  /** Optional centered title (e.g. current session name) */
  title?: string;
}

export default function Navbar({
  sidebarOpen,
  toggleSidebar,
  inspectorOpen,
  toggleInspector,
  showPanelToggles = false,
  title,
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
            aria-label="切换侧栏"
          >
            <PanelLeft className="w-[16px] h-[16px]" />
          </button>
        ) : null}
      </div>

      {/* Center — Title */}
      <div className="flex-1 flex items-center justify-center min-w-0 px-4">
        {title ? (
          <button className="flex items-center gap-1.5 text-[14px] font-medium text-gray-800 hover:bg-black/[0.04] px-3 py-1.5 rounded-lg transition-colors">
            <span className="truncate">{title}</span>
            <ChevronDown className="w-[14px] h-[14px] text-gray-400" />
          </button>
        ) : null}
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
            aria-label="切换右侧面板"
          >
            <PanelRight className="w-[16px] h-[16px]" />
          </button>
        ) : null}
      </div>
    </nav>
  );
}
