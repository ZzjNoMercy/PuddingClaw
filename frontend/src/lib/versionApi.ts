/**
 * Version management API client for PuddingClaw backend.
 */

const API_BASE = "/api";

export interface VersionInfo {
  label: string;
  created_at: string;
  file_count: number;
}

export interface VersionContent {
  label: string;
  content: string;
}

export interface DiffResult {
  version_a: string;
  version_b: string;
  content_a: string;
  content_b: string;
}

/**
 * Create a version snapshot for a skill.
 */
export async function createVersion(
  skillName: string,
  label: string
): Promise<VersionInfo & { status: string }> {
  const resp = await fetch(
    `${API_BASE}/skills/${encodeURIComponent(skillName)}/versions`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label }),
    }
  );
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to create version: ${resp.status}`);
  }
  return resp.json();
}

/**
 * List all version snapshots for a skill, newest first.
 */
export async function listVersions(
  skillName: string
): Promise<VersionInfo[]> {
  const resp = await fetch(
    `${API_BASE}/skills/${encodeURIComponent(skillName)}/versions`
  );
  if (!resp.ok)
    throw new Error(`Failed to list versions: ${resp.status}`);
  const data = await resp.json();
  return data.versions ?? [];
}

/**
 * Get the SKILL.md content of a specific version.
 */
export async function getVersionContent(
  skillName: string,
  label: string
): Promise<VersionContent> {
  const resp = await fetch(
    `${API_BASE}/skills/${encodeURIComponent(skillName)}/versions/${encodeURIComponent(label)}`
  );
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to get version: ${resp.status}`);
  }
  return resp.json();
}

/**
 * Compare two versions of a skill. Use "current" for the working directory.
 */
export async function diffVersions(
  skillName: string,
  versionA: string,
  versionB: string
): Promise<DiffResult> {
  const resp = await fetch(
    `${API_BASE}/skills/${encodeURIComponent(skillName)}/diff`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version_a: versionA, version_b: versionB }),
    }
  );
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to diff versions: ${resp.status}`);
  }
  return resp.json();
}
