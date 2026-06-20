/**
 * Skills API client for PuddingClaw backend.
 */

const API_BASE = "/api";

export interface SkillInfo {
  name: string;
  path: string;
  description: string;
}

export interface SkillFileInfo {
  name: string;
  size: number;
  modified: string;
}

export interface FileTreeNode {
  path: string;
  type: "file" | "directory";
  size?: number;
  modified?: string;
  children?: FileTreeNode[];
}

export interface SkillTree {
  name: string;
  files: FileTreeNode[];
}

export interface SkillDetail {
  name: string;
  description: string;
  path: string;
  files: SkillFileInfo[];
  content: string;
}

export async function listSkills(): Promise<SkillInfo[]> {
  const resp = await fetch(`${API_BASE}/skills`);
  if (!resp.ok) throw new Error(`Failed to list skills: ${resp.status}`);
  const data = await resp.json();
  return data.skills ?? data;
}

export async function getSkill(name: string): Promise<SkillDetail> {
  const resp = await fetch(`${API_BASE}/skills/${encodeURIComponent(name)}`);
  if (!resp.ok) throw new Error(`Failed to get skill: ${resp.status}`);
  return resp.json();
}

export async function createSkill(
  name: string,
  description: string
): Promise<{ name: string; path: string }> {
  const resp = await fetch(`${API_BASE}/skills`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, description }),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to create skill: ${resp.status}`);
  }
  return resp.json();
}

export async function deleteSkill(name: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/skills/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to delete skill: ${resp.status}`);
  }
}

export async function renameSkill(oldName: string, newName: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/skills/${encodeURIComponent(oldName)}/rename`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ new_name: newName }),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to rename skill: ${resp.status}`);
  }
}

export async function getSkillFiles(name: string): Promise<SkillFileInfo[]> {
  const resp = await fetch(`${API_BASE}/skills/${encodeURIComponent(name)}/files`);
  if (!resp.ok) throw new Error(`Failed to get skill files: ${resp.status}`);
  return resp.json();
}

export async function getSkillFile(
  name: string,
  filePath: string
): Promise<{ path: string; content: string; language: string }> {
  const resp = await fetch(
    `${API_BASE}/skills/${encodeURIComponent(name)}/file?path=${encodeURIComponent(filePath)}`
  );
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to get file: ${resp.status}`);
  }
  return resp.json();
}

export async function getSkillTree(name: string): Promise<SkillTree> {
  const resp = await fetch(`${API_BASE}/skills/${encodeURIComponent(name)}/tree`);
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to get skill tree: ${resp.status}`);
  }
  return resp.json();
}

export async function saveSkillContent(
  name: string,
  fileName: string,
  content: string
): Promise<void> {
  // Use the new /api/skills/{name}/file endpoint
  const resp = await fetch(`${API_BASE}/skills/${encodeURIComponent(name)}/file`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: fileName, content }),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to save file: ${resp.status}`);
  }
}
