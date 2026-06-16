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
    const API_BASE = `http://${window.location.hostname}:9100/api`;
    fetch(`${API_BASE}/skills/active`)
      .then(res => res.json())
      .then(data => setLoadedSkills(data.skills || []))
      .catch(() => setLoadedSkills([]));
  }, []);

  const handleUnloadSkill = async (skillName: string) => {
    try {
      const API_BASE = `http://${window.location.hostname}:9100/api`;
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
    <div className="h-10 flex items-center gap-2 px-4 bg-amber-50/50 border-b border-amber-200/50">
      <Sparkles className="w-4 h-4 text-amber-600" />
      <span className="text-xs font-medium text-gray-600">已加载 Skills:</span>
      <div className="flex gap-2">
        {loadedSkills.map(skill => (
          <div
            key={skill.name}
            className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-amber-500/10 text-amber-700 border border-amber-400/20"
            title={skill.description}
          >
            <span className="text-xs font-medium">{skill.name}</span>
            <button
              onClick={() => handleUnloadSkill(skill.name)}
              className="w-3.5 h-3.5 flex items-center justify-center rounded hover:bg-amber-500/20 transition-colors"
            >
              <X className="w-2.5 h-2.5" />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
