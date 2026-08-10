export interface RunInput {
  message: string;
  session_id?: string;
  /** Platform-owned absolute host workspace; Backend maps it to project_id. */
  workspace_path?: string;
  request_id?: string;
  metadata?: Record<string, unknown>;
}

export interface ArtifactRef {
  name: string;
  path: string;
  kind?: string;
  size?: number;
  origin: "push" | "observe";
}
