export interface RunInput {
  message: string;
  model?: string;
  session_id?: string;
  metadata?: Record<string, unknown>;
}
