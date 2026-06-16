"use client";

import { useEffect, useRef } from "react";
import { Zap } from "lucide-react";

interface SkillItem {
  name: string;
  description: string;
}

interface SlashCommandMenuProps {
  visible: boolean;
  filteredSkills: SkillItem[];  // Pre-filtered by parent (single source of truth)
  selectedIndex: number;
  onSelect: (skillName: string) => void;
  onClose: () => void;
}

export default function SlashCommandMenu({
  visible,
  filteredSkills,
  selectedIndex,
  onSelect,
  onClose,
}: SlashCommandMenuProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);

  // Auto-scroll to keep selected item visible
  useEffect(() => {
    if (!visible) return;
    const el = itemRefs.current[selectedIndex];
    if (el) {
      el.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, [selectedIndex, visible]);

  // Click-outside to close (I-5 fix)
  useEffect(() => {
    if (!visible) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        onClose();
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [visible, onClose]);

  if (!visible) return null;

  return (
    <div ref={containerRef} className="absolute bottom-full left-0 right-0 mb-2 z-50">
      <div className="bg-white/90 backdrop-blur-lg border border-black/[0.08] rounded-2xl shadow-xl overflow-hidden mx-1">
        {/* Header */}
        <div className="px-3 pt-2.5 pb-1.5 border-b border-black/[0.05]">
          <span className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">
            Skills
          </span>
        </div>

        {/* Skill list */}
        <div className="overflow-y-auto" style={{ maxHeight: "180px" }}>
          {filteredSkills.length === 0 ? (
            <div className="px-4 py-3 text-[13px] text-gray-400 text-center">
              未找到匹配的 Skill
            </div>
          ) : (
            filteredSkills.map((skill, idx) => {
              const isSelected = idx === selectedIndex;
              return (
                <button
                  key={skill.name}
                  ref={(el) => { itemRefs.current[idx] = el; }}
                  onClick={() => onSelect(skill.name)}
                  className={`w-full flex items-center gap-2.5 px-3 py-2 text-left transition-colors ${
                    isSelected
                      ? "bg-blue-50 text-blue-700"
                      : "text-gray-700 hover:bg-gray-50"
                  }`}
                >
                  <Zap
                    className={`w-3.5 h-3.5 shrink-0 ${
                      isSelected ? "text-blue-500" : "text-gray-400"
                    }`}
                  />
                  <span className="font-semibold text-[13px] shrink-0">
                    {skill.name}
                  </span>
                  <span className="text-[12px] text-gray-400 truncate min-w-0">
                    {skill.description}
                  </span>
                </button>
              );
            })
          )}
        </div>

        {/* Footer hint */}
        <div className="px-3 py-1.5 border-t border-black/[0.05] bg-gray-50/60">
          <span className="text-[10px] text-gray-400">
            ↑↓ 选择 · Enter 确认 · Esc 取消
          </span>
        </div>
      </div>
    </div>
  );
}
