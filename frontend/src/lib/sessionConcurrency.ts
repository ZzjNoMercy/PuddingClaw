export function mergeRunningSessionIds(
  localSessionIds: ReadonlySet<string>,
  sharedSessionIds: ReadonlySet<string>,
): Set<string> {
  const merged = new Set<string>();
  localSessionIds.forEach((sessionId) => merged.add(sessionId));
  sharedSessionIds.forEach((sessionId) => merged.add(sessionId));
  return merged;
}

export function isSessionSubmitting(
  submittingSessionIds: ReadonlySet<string>,
  sessionId: string,
): boolean {
  return submittingSessionIds.has(sessionId);
}
