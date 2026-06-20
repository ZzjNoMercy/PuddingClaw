"use client";

import { useEffect, useState } from "react";
import { Sparkles, X } from "lucide-react";

interface LoadedSkill {
  name: string;
  description: string;
}

export default function SkillsBar() {
  const [loadedSkills, setLoadedSkills] = useState<LoadedSkill[]>([]);

  useEffect(() => {
    // Fetch loaded skills from backend
    const API_BASE = "/api";
    fetch(`${API_BASE}/skills/active`)
      .then(res => res.json())
      .then(data => setLoadedSkills(data.skills || []))
      .catch(() => setLoadedSkills([]));
  }, []);

  const handleUnloadSkill = async (skillName: string) => {
    try {
      const API_BASE = "/api";
      await fetch(`${API_BASE}/skills/unload`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skill_name: skillName })
      });
      setLoadedSkills(prev => prev.filter(s => s.name !== skillName));
    } catch (err) {
      console.error("Failed to unload skill:", err);
    }
  };

  if (loadedSkills.length === 0) return null;

  return (
    <div className="h-10 flex items-center gap-2 px-4 bg-[#002fa7]/5 border-b border-[#002fa7]/10">
      <Sparkles className="w-4 h-4 text-[#001f7a]" />
      <span className="text-xs font-medium text-gray-600">已加载 Skills:</span>
      <div className="flex gap-2">
        {loadedSkills.map(skill => (
          <div
            key={skill.name}
            className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-[#002fa7]/10 text-[#001f7a] border border-[#002fa7]/20"
            title={skill.description}
          >
            <span className="text-xs font-medium">{skill.name}</span>
            <button
              onClick={() => handleUnloadSkill(skill.name)}
              className="w-3.5 h-3.5 flex items-center justify-center rounded hover:bg-[#002fa7]/20 transition-colors"
            >
              <X className="w-2.5 h-2.5" />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
