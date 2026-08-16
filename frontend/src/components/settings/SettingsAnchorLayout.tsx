"use client";

import { useEffect, useMemo, useState } from "react";
import type { ElementType, ReactNode } from "react";

export type SettingsAnchorSection = {
  id: string;
  label: string;
  description: string;
  icon: ElementType;
  disabled?: boolean;
};

type SettingsAnchorLayoutProps = {
  prefix: string;
  sections: SettingsAnchorSection[];
  children: ReactNode;
  filterable?: boolean;
};

export default function SettingsAnchorLayout({
  prefix,
  sections,
  children,
  filterable = false,
}: SettingsAnchorLayoutProps) {
  const [activeSection, setActiveSection] = useState(sections[0]?.id || "");
  const [filter, setFilter] = useState("");

  const visibleSections = useMemo(() => {
    const query = filter.trim().toLowerCase();
    if (!query) return sections;
    return sections.filter((section) =>
      `${section.label} ${section.description}`.toLowerCase().includes(query),
    );
  }, [filter, sections]);

  useEffect(() => {
    if (!sections.some((section) => section.id === activeSection)) {
      setActiveSection(sections[0]?.id || "");
    }
  }, [activeSection, sections]);

  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((left, right) => left.boundingClientRect.top - right.boundingClientRect.top);
        if (visible[0]) {
          setActiveSection(visible[0].target.id.replace(`${prefix}-section-`, ""));
        }
      },
      { rootMargin: "-10% 0px -70% 0px", threshold: 0 },
    );

    sections.forEach((section) => {
      const element = document.getElementById(`${prefix}-section-${section.id}`);
      if (element) observer.observe(element);
    });
    return () => observer.disconnect();
  }, [prefix, sections]);

  const scrollToSection = (id: string) => {
    setActiveSection(id);
    document.getElementById(`${prefix}-section-${id}`)?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  };

  return (
    <div className="flex flex-col gap-5 lg:flex-row">
      <aside className="flex w-full flex-col gap-3 lg:sticky lg:top-6 lg:w-56 lg:self-start lg:shrink-0">
        {filterable && (
          <input
            type="text"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            placeholder="筛选分类..."
            className="form-input text-[12px]"
          />
        )}
        <nav className="flex gap-1 overflow-x-auto pb-1 lg:flex-col lg:overflow-visible lg:pb-0" aria-label="页面分区">
          {visibleSections.map((section) => {
            const Icon = section.icon;
            const active = activeSection === section.id;
            return (
              <button
                key={section.id}
                type="button"
                onClick={() => !section.disabled && scrollToSection(section.id)}
                disabled={section.disabled}
                className={`flex min-w-[168px] items-start gap-2.5 rounded-xl px-3 py-2.5 text-left transition-all lg:min-w-0 lg:w-full ${
                  section.disabled
                    ? "cursor-not-allowed opacity-50"
                    : active
                      ? "bg-[#002fa7]/[0.07] text-[#002fa7]"
                      : "text-gray-600 hover:bg-black/[0.035] hover:text-gray-900"
                }`}
              >
                <Icon className="mt-0.5 h-4 w-4 shrink-0" />
                <span className="min-w-0">
                  <span className="block text-[12px] font-semibold">{section.label}</span>
                  <span className="block truncate text-[10px] opacity-70">{section.description}</span>
                </span>
              </button>
            );
          })}
          {visibleSections.length === 0 && (
            <p className="px-3 py-2 text-[11px] text-gray-400">无匹配分类</p>
          )}
        </nav>
      </aside>
      <div className="min-w-0 flex-1 space-y-6">{children}</div>
    </div>
  );
}
