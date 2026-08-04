export type WorkerResponse = Record<string, unknown>;
export interface WorkerClientConfig { endpoint: string; token: string; timeoutMs: number }
export interface WorkerClient { request(path: string, options?: unknown): Promise<WorkerResponse> }
