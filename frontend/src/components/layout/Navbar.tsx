"use client";

import { useState, useRef, useEffect } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  PanelLeft,
  PanelRight,
  ExternalLink,
  MessageSquare,
  Zap,
  Settings,
  ChevronDown,
  Settings2,
  GitCompareArrows,
  ClipboardCheck,
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
  const pathname = usePathname();
  const [skillsMenuOpen, setSkillsMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // Close skills dropdown on outside click
  useEffect(() => {
    if (!skillsMenuOpen) return;
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setSkillsMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [skillsMenuOpen]);

  // Close dropdown on route change
  useEffect(() => {
    setSkillsMenuOpen(false);
  }, [pathname]);

  const isActive = (href: string) => {
    if (href === "/") return pathname === "/";
    return pathname.startsWith(href);
  };

  const navLinkClass = (href: string) =>
    `flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-[12px] font-medium transition-all ${
      isActive(href)
        ? "bg-[#002fa7]/10 text-[#002fa7]"
        : "text-gray-400 hover:text-gray-600 hover:bg-black/[0.04]"
    }`;

  return (
    <nav className="glass-nav sticky top-0 z-50 h-11 flex items-center justify-between px-3">
      {/* Left — Sidebar toggle (chat page only) or spacer */}
      <div className="w-[120px] flex items-center">
        {showPanelToggles && toggleSidebar ? (
          <button
            onClick={toggleSidebar}
            className={`w-8 h-8 flex items-center justify-center rounded-lg transition-all ${
              sidebarOpen
                ? "bg-[#002fa7] text-white shadow-sm"
                : "text-gray-400 hover:text-gray-600 hover:bg-black/[0.04]"
            }`}
          >
            <PanelLeft className="w-[16px] h-[16px]" />
          </button>
        ) : null}
      </div>

      {/* Center — Brand + Navigation */}
      <div className="flex items-center gap-3">
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

        <span className="text-gray-200">|</span>

        {/* Navigation Tabs */}
        <div className="flex items-center gap-0.5">
          <Link href="/" className={navLinkClass("/")}>
            <MessageSquare className="w-3.5 h-3.5" />
            对话
          </Link>

          {/* Skills with dropdown */}
          <div className="relative" ref={menuRef}>
            <button
              onClick={() => setSkillsMenuOpen((v) => !v)}
              className={`flex items-center gap-1 px-2.5 py-1 rounded-lg text-[12px] font-medium transition-all ${
                isActive("/skills")
                  ? "bg-[#002fa7]/10 text-[#002fa7]"
                  : "text-gray-400 hover:text-gray-600 hover:bg-black/[0.04]"
              }`}
            >
              <Zap className="w-3.5 h-3.5" />
              Skills
              <ChevronDown className={`w-3 h-3 transition-transform ${skillsMenuOpen ? "rotate-180" : ""}`} />
            </button>

            {skillsMenuOpen && (
              <div className="absolute top-full left-0 mt-1 w-40 bg-white/95 backdrop-blur-xl rounded-lg shadow-lg border border-black/[0.06] py-1 z-50 animate-fade-in-scale">
                <Link
                  href="/skills"
                  className={`flex items-center gap-2 px-3 py-1.5 text-[12px] transition-colors ${
                    pathname === "/skills"
                      ? "text-[#002fa7] bg-[#002fa7]/5"
                      : "text-gray-600 hover:bg-gray-50"
                  }`}
                >
                  <Settings2 className="w-3.5 h-3.5" />
                  配置管理
                </Link>
                <Link
                  href="/skills/compare"
                  className={`flex items-center gap-2 px-3 py-1.5 text-[12px] transition-colors ${
                    pathname === "/skills/compare"
                      ? "text-[#002fa7] bg-[#002fa7]/5"
                      : "text-gray-600 hover:bg-gray-50"
                  }`}
                >
                  <GitCompareArrows className="w-3.5 h-3.5" />
                  版本对比
                </Link>
                <Link
                  href="/skills/review"
                  className={`flex items-center gap-2 px-3 py-1.5 text-[12px] transition-colors ${
                    pathname === "/skills/review"
                      ? "text-[#002fa7] bg-[#002fa7]/5"
                      : "text-gray-600 hover:bg-gray-50"
                  }`}
                >
                  <ClipboardCheck className="w-3.5 h-3.5" />
                  评估审核
                </Link>
              </div>
            )}
          </div>

          <Link href="/settings" className={navLinkClass("/settings")}>
            <Settings className="w-3.5 h-3.5" />
            设置
          </Link>
        </div>

        <span className="text-gray-200">|</span>

        <a
          href="https://fufan.ai"
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-1 text-[12px] text-gray-400 hover:text-[#ff6723] transition-colors"
        >
          赋范空间
          <ExternalLink className="w-3 h-3" />
        </a>
      </div>

      {/* Right — Inspector toggle (chat page only) or spacer */}
      <div className="w-[120px] flex justify-end">
        {showPanelToggles && toggleInspector ? (
          <button
            onClick={toggleInspector}
            className={`w-8 h-8 flex items-center justify-center rounded-lg transition-all ${
              inspectorOpen
                ? "bg-[#ff6723] text-white shadow-sm"
                : "text-gray-400 hover:text-gray-600 hover:bg-black/[0.04]"
            }`}
          >
            <PanelRight className="w-[16px] h-[16px]" />
          </button>
        ) : null}
      </div>
    </nav>
  );
}
