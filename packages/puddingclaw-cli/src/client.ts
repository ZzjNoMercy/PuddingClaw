export type WorkerResponse = Record<string, unknown>;
export interface WorkerClientConfig { endpoint: string; token: string; timeoutMs: number }
export interface WorkerClient { request(path: string, options?: unknown): Promise<WorkerResponse> }
export interface ResumeDecision { request_id: string; decision: "approve" | "reject"; scope?: "once" | "session"; message?: string }
export interface ResumeInput { continuation_token: string; decisions: ResumeDecision[]; request_id?: string }
