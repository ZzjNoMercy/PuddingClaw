export const capabilities = ["data.query", "data.analysis", "data.nl2sql", "knowledge.query"] as const;
export const operations = ["run", "continue", "respond", "cancel"] as const;
export const interactionKinds = ["permission_request"] as const;
