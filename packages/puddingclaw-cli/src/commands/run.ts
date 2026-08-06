export interface RunInput {
  message: string;
  session_id?: string;
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
