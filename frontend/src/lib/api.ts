/**
 * API client for PuddingClaw backend.
 * Custom SSE parser for POST requests (native EventSource only supports GET).
 */

const API_BASE = "/api";

export interface SSEEvent {
  event: string;
  data: Record<string, unknown>;
}

/**
 * Stream chat messages via POST SSE.
 * Yields parsed SSE events as they arrive.
 */
export async function* streamChat(
  message: string,
  sessionId: string,
  signal?: AbortSignal,
  userId?: string
): AsyncGenerator<SSEEvent> {
  const response = await fetch(`${API_BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message,
      session_id: sessionId,
      user_id: userId || "default_user",
      stream: true
    }),
    signal,
  });

  if (!response.ok) {
    throw new Error(`Chat API error: ${response.status}`);
  }

  const reader = response.body?.getReader();
  if (!reader) throw new Error("No response body");

  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    // SSE uses an empty line as the event boundary. Parsing complete frames
    // keeps event/data association correct even when a network chunk splits
    // between the two lines.
    buffer = buffer.replace(/\r\n/g, "\n");
    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";

    for (const frame of frames) {
      const parsed = parseSSEFrame(frame);
      if (!parsed) continue;

      if (parsed.event === "token" && typeof parsed.data.content === "string") {
        // Consume upstream chunks immediately. The HTTP trace proves the proxy
        // already delivers data incrementally; adding rAF/timer pacing here can
        // only create a client-side queue and delayed "replay" on long runs.
        yield parsed;
        continue;
      }

      yield parsed;
    }
  }
}

function parseSSEFrame(frame: string): SSEEvent | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) {
      event = line.slice(6).trim() || "message";
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (dataLines.length === 0) return null;
  try {
    const data = JSON.parse(dataLines.join("\n"));
    return { event, data };
  } catch {
    return null;
  }
}

/**
 * Read a file from the backend.
 */
export async function readFile(path: string): Promise<string> {
  const resp = await fetch(`${API_BASE}/files?path=${encodeURIComponent(path)}`);
  if (!resp.ok) throw new Error(`Failed to read file: ${resp.status}`);
  const data = await resp.json();
  return data.content;
}

/**
 * Save a file to the backend.
 */
export async function saveFile(path: string, content: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/files`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, content }),
  });
  if (!resp.ok) throw new Error(`Failed to save file: ${resp.status}`);
}

/**
 * List all sessions.
 */
export async function listSessions(): Promise<
  Array<{ id: string; title: string; updated_at: number }>
> {
  const resp = await fetch(`${API_BASE}/sessions`);
  if (!resp.ok) throw new Error(`Failed to list sessions: ${resp.status}`);
  const data = await resp.json();
  return data.sessions;
}

/**
 * Create a new session.
 */
export async function createSession(): Promise<{ id: string; title: string }> {
  const resp = await fetch(`${API_BASE}/sessions`, { method: "POST" });
  if (!resp.ok) throw new Error(`Failed to create session: ${resp.status}`);
  return resp.json();
}

/**
 * Rename a session.
 */
export async function renameSession(id: string, title: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!resp.ok) throw new Error(`Failed to rename session: ${resp.status}`);
}

/**
 * Delete a session.
 */
export async function deleteSession(id: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!resp.ok) throw new Error(`Failed to delete session: ${resp.status}`);
}

/**
 * Get raw messages for a session (including system prompt).
 */
export async function getRawMessages(
  sessionId: string
): Promise<{ session_id: string; title: string; messages: Array<{ role: string; content: string }> }> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/messages`
  );
  if (!resp.ok) throw new Error(`Failed to get raw messages: ${resp.status}`);
  return resp.json();
}

/**
 * Get session conversation history (no system prompt, includes tool_calls).
 */
export async function getSessionHistory(
  sessionId: string
): Promise<{
  session_id: string;
  messages: Array<{ role: string; content: string; tool_calls?: Array<{ tool: string; input?: string; output?: string }> }>;
}> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/history`
  );
  if (!resp.ok) throw new Error(`Failed to get session history: ${resp.status}`);
  return resp.json();
}

/**
 * List available skills.
 */
export async function listSkills(): Promise<
  Array<{ name: string; path: string; description: string }>
> {
  const resp = await fetch(`${API_BASE}/skills`);
  if (!resp.ok) throw new Error(`Failed to list skills: ${resp.status}`);
  const data = await resp.json();
  return data.skills;
}

/**
 * List enabled MCP servers.
 */
export async function listMcpServers(): Promise<
  Array<{ key: string; name: string; url: string; transport: string }>
> {
  const resp = await fetch(`${API_BASE}/mcp/servers`);
  if (!resp.ok) throw new Error(`Failed to list MCP servers: ${resp.status}`);
  const data = await resp.json();
  return data.servers;
}

/**
 * Load a skill into the current session.
 */
export async function loadSkill(skillName: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/skills/load`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skill_name: skillName }),
  });
  if (!resp.ok) throw new Error(`Failed to load skill: ${resp.status}`);
}

/**
 * Generate a title for a session using AI.
 */
export async function generateTitle(
  sessionId: string
): Promise<{ title: string }> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/generate-title`,
    { method: "POST" }
  );
  if (!resp.ok) throw new Error(`Failed to generate title: ${resp.status}`);
  return resp.json();
}

/**
 * Get token count for a session (system + messages).
 */
export async function getSessionTokenCount(
  sessionId: string
): Promise<{ system_tokens: number; message_tokens: number; total_tokens: number; compaction_trigger: number; percentage: number }> {
  const resp = await fetch(
    `${API_BASE}/tokens/session/${encodeURIComponent(sessionId)}`
  );
  if (!resp.ok) throw new Error(`Failed to get token count: ${resp.status}`);
  return resp.json();
}

/**
 * Get token counts for a list of files.
 */
export async function getFileTokenCounts(
  paths: string[]
): Promise<{ files: Array<{ path: string; tokens: number }> }> {
  const resp = await fetch(`${API_BASE}/tokens/files`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths }),
  });
  if (!resp.ok) throw new Error(`Failed to get file token counts: ${resp.status}`);
  return resp.json();
}

/**
 * Compress a session's conversation history.
 */
export async function compressSession(
  sessionId: string
): Promise<{ archived_count: number; remaining_count: number }> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/compress`,
    { method: "POST" }
  );
  if (!resp.ok) throw new Error(`Failed to compress session: ${resp.status}`);
  return resp.json();
}

/**
 * Clear all messages in a session (like Claude Code /clear).
 */
export async function clearSession(
  sessionId: string
): Promise<{ status: string; session_id: string }> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/clear`,
    { method: "POST" }
  );
  if (!resp.ok) throw new Error(`Failed to clear session: ${resp.status}`);
  return resp.json();
}

/**
 * Get current RAG mode status.
 */
export async function getRagMode(): Promise<{ rag_mode: boolean }> {
  const resp = await fetch(`${API_BASE}/config/rag-mode`);
  if (!resp.ok) throw new Error(`Failed to get RAG mode: ${resp.status}`);
  return resp.json();
}

/**
 * Set RAG mode enabled/disabled.
 */
export async function setRagMode(
  enabled: boolean
): Promise<{ rag_mode: boolean }> {
  const resp = await fetch(`${API_BASE}/config/rag-mode`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  if (!resp.ok) throw new Error(`Failed to set RAG mode: ${resp.status}`);
  return resp.json();
}
