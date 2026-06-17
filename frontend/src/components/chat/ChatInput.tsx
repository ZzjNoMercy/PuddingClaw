"use client";

import { useState, useRef, useCallback, useEffect, useMemo } from "react";
import { ArrowUp, Square } from "lucide-react";
import { useApp } from "@/lib/store";
import { listSkills, getSessionTokenCount } from "@/lib/api";

function formatTokens(n: number): string {
  return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k`;
}
import SlashCommandMenu from "./SlashCommandMenu";

export default function ChatInput() {
  const [text, setText] = useState("");
  const { sendMessage, stopStreaming, isStreaming, isCompressing, sessionId, contextUsage, setContextUsage } = useApp();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const disabled = isStreaming || isCompressing;

  // Fetch token count on mount and when session changes
  useEffect(() => {
    getSessionTokenCount(sessionId)
      .then((data) => {
        setContextUsage({
          used: data.total_tokens,
          total: data.compaction_trigger,
          percentage: data.percentage,
        });
      })
      .catch(() => {});
  }, [sessionId, setContextUsage]);

  // Slash command state
  const [showSlashMenu, setShowSlashMenu] = useState(false);
  const [slashQuery, setSlashQuery] = useState("");
  const [selectedMenuIndex, setSelectedMenuIndex] = useState(0);
  const [skills, setSkills] = useState<Array<{ name: string; description: string }>>([]);
  // Track the position of the `/` that triggered the menu, for replacement on select
  const slashStartPosRef = useRef<number>(-1);
  // Pending cursor position to set after React re-render (fixes I-2: rAF race)
  const pendingCursorRef = useRef<number | null>(null);

  // Preload skills on mount
  useEffect(() => {
    listSkills().then(setSkills).catch(() => {});
  }, []);

  // Single source of truth for filtered skills (fixes I-1: dedup filter logic)
  const filteredSkills = useMemo(
    () => skills.filter((s) =>
      s.name.toLowerCase().includes(slashQuery) ||
      s.description.toLowerCase().includes(slashQuery)
    ),
    [skills, slashQuery]
  );

  // Ref to let global Escape handler know if slash menu is open (fixes I-2)
  const showSlashMenuRef = useRef(false);
  useEffect(() => { showSlashMenuRef.current = showSlashMenu; }, [showSlashMenu]);

  // Apply pending cursor position after React re-renders textarea with new text
  useEffect(() => {
    if (pendingCursorRef.current !== null && textareaRef.current) {
      textareaRef.current.setSelectionRange(pendingCursorRef.current, pendingCursorRef.current);
      pendingCursorRef.current = null;
    }
  }, [text]);

  const handleSubmit = useCallback(() => {
    if (!text.trim() || disabled) return;
    sendMessage(text.trim());
    setText("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  }, [text, disabled, sendMessage]);

  const handleSlashSelect = useCallback((skillName: string) => {
    // Use textarea DOM value as source of truth to avoid stale closure (fixes I-1)
    const currentText = textareaRef.current?.value ?? "";
    const startPos = slashStartPosRef.current;
    if (startPos >= 0) {
      const cursorPos = textareaRef.current?.selectionStart ?? currentText.length;
      const before = currentText.slice(0, startPos);
      const after = currentText.slice(cursorPos);
      const inserted = `/${skillName} `;
      const newText = before + inserted + after;
      setText(newText);
      // Schedule cursor placement after React re-render (fixes I-2)
      pendingCursorRef.current = startPos + inserted.length;
    } else {
      setText(`/${skillName} `);
    }
    setShowSlashMenu(false);
    slashStartPosRef.current = -1;
    textareaRef.current?.focus();
  }, []);

  // Escape key to stop streaming (global listener)
  // Skip if slash menu is open — let the local handler close it first (I-2 fix)
  useEffect(() => {
    const handleGlobalKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && isStreaming && !showSlashMenuRef.current) {
        e.preventDefault();
        stopStreaming();
      }
    };
    window.addEventListener("keydown", handleGlobalKeyDown);
    return () => window.removeEventListener("keydown", handleGlobalKeyDown);
  }, [isStreaming, stopStreaming]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (showSlashMenu) {
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedMenuIndex((prev) => Math.max(0, prev - 1));
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedMenuIndex((prev) => Math.min(prev + 1, Math.max(0, filteredSkills.length - 1)));
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        if (filteredSkills.length > 0) {
          const idx = Math.min(selectedMenuIndex, filteredSkills.length - 1);
          handleSlashSelect(filteredSkills[idx].name);
        }
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setShowSlashMenu(false);
        return;
      }
    }
    // Original submit logic
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleInput = () => {
    const el = textareaRef.current;
    if (el) { el.style.height = "auto"; el.style.height = Math.min(el.scrollHeight, 160) + "px"; }
  };

  return (
    <div className="p-4 pb-5">
      <div className="glass-input rounded-2xl flex items-end gap-2 px-4 py-2.5 max-w-2xl mx-auto hover:shadow-md transition-shadow relative">
        <SlashCommandMenu
          visible={showSlashMenu}
          filteredSkills={filteredSkills}
          selectedIndex={selectedMenuIndex}
          onSelect={handleSlashSelect}
          onClose={() => setShowSlashMenu(false)}
        />
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => {
            const val = e.target.value;
            const cursorPos = e.target.selectionStart ?? val.length;
            setText(val);
            handleInput();

            // Slash command detection: scan backwards from cursor for `/`
            // Trigger when `/` is at start of text or preceded by a space/newline,
            // and there's no space between `/` and cursor (i.e. still typing the command name)
            let slashPos = -1;
            for (let i = cursorPos - 1; i >= 0; i--) {
              const ch = val[i];
              if (ch === " " || ch === "\n") break; // hit whitespace before finding `/`
              if (ch === "/") {
                // Valid if at start or preceded by space/newline
                if (i === 0 || val[i - 1] === " " || val[i - 1] === "\n") {
                  slashPos = i;
                }
                break;
              }
            }

            if (slashPos >= 0) {
              const query = val.slice(slashPos + 1, cursorPos).toLowerCase();
              setShowSlashMenu(true);
              setSlashQuery(query);
              setSelectedMenuIndex(0);
              slashStartPosRef.current = slashPos;
            } else {
              setShowSlashMenu(false);
              slashStartPosRef.current = -1;
            }
          }}
          onKeyDown={handleKeyDown}
          placeholder="输入消息... 输入 / 调用 Skill"
          rows={1}
          className="flex-1 resize-none bg-transparent text-[14px] outline-none placeholder:text-gray-400 max-h-40 py-1 leading-relaxed"
        />
        {isStreaming ? (
          <button
            onClick={stopStreaming}
            className="shrink-0 w-8 h-8 flex items-center justify-center rounded-xl bg-red-500 text-white hover:bg-red-600 transition-all active:scale-95"
            title="停止生成 (Esc)"
          >
            <Square className="w-3.5 h-3.5 fill-current" />
          </button>
        ) : (
          <button
            onClick={handleSubmit}
            disabled={!text.trim() || isCompressing}
            className="shrink-0 w-8 h-8 flex items-center justify-center rounded-xl bg-[#002fa7] text-white disabled:opacity-25 hover:bg-[#001f7a] transition-all active:scale-95"
          >
            <ArrowUp className="w-4 h-4" />
          </button>
        )}
      </div>

      {/* Context Usage */}
      <div className="max-w-2xl mx-auto px-4 py-1 flex justify-end">
        <span
          className={`text-[10px] font-medium ${
            contextUsage.percentage >= 90
              ? "text-red-500"
              : contextUsage.percentage >= 70
              ? "text-amber-500"
              : "text-gray-400"
          }`}
        >
          CONTEXT：{contextUsage.percentage.toFixed(1)}%({formatTokens(contextUsage.used)}/{formatTokens(contextUsage.total)})
        </span>
      </div>

      <p className="text-center text-[10px] text-gray-400/70 mt-2">
        Powered by DeepSeek · PuddingClaw v0.1
      </p>
    </div>
  );
}
