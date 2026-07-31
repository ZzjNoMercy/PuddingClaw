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

export function rebindSessionScopedLock(
  lockedSessionIds: ReadonlySet<string>,
  fromSessionId: string,
  toSessionId: string,
): Set<string> {
  const next = new Set(lockedSessionIds);
  if (fromSessionId === toSessionId || !next.delete(fromSessionId)) {
    return next;
  }
  next.add(toSessionId);
  return next;
}

export function releaseOrphanedPlaceholderLock(
  lockedSessionIds: ReadonlySet<string>,
  placeholderSessionId: string,
  options: {
    creationPending: boolean;
    streaming: boolean;
  },
): Set<string> {
  const next = new Set(lockedSessionIds);
  if (!options.creationPending && !options.streaming) {
    next.delete(placeholderSessionId);
  }
  return next;
}
