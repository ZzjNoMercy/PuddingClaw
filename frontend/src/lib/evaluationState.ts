import type { EvalDataset, EvalExperiment } from "./evaluationApi";

export function datasetActions(dataset: EvalDataset) {
  return {
    editable: dataset.status === "draft",
    publishable: dataset.status === "draft" && dataset.cases.length > 0,
    reopenable: dataset.status === "published",
    syncable: dataset.status === "published" && dataset.current_version > 0,
    archivable: dataset.status !== "archived",
  };
}

export function experimentIsTerminal(status: EvalExperiment["status"]): boolean {
  return ["completed", "failed", "cancelled"].includes(status);
}

export function safeRemoteUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.toString() : null;
  } catch { return null; }
}

export function estimateCaseRuns(caseCount: number, repetitions: number): number {
  return Math.max(0, caseCount) * Math.max(1, repetitions);
}
