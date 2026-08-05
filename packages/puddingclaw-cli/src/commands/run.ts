export interface RunInput {
  message: string;
  session_id?: string;
  metadata?: Record<string, unknown>;
}
