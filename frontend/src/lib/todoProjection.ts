export type TodoAuthority = {
  kind: "legacy" | "none" | "goal" | "run";
  goal_id?: string;
  goal_revision?: number;
  run_id?: string;
};

export function todoAuthorityKey(authority?: Partial<TodoAuthority> | null): string {
  if (!authority) return "unknown";
  if (authority.kind === "goal") {
    return `goal:${authority.goal_id || ""}:revision:${authority.goal_revision || 1}`;
  }
  if (authority.kind === "run") return `run:${authority.run_id || ""}`;
  return authority.kind || "unknown";
}

export function shouldApplyTodoSnapshot(
  previousAuthority: Partial<TodoAuthority> | null | undefined,
  previousRevision: number,
  nextAuthority: Partial<TodoAuthority> | null | undefined,
  nextRevision: number | null | undefined,
): boolean {
  if (typeof nextRevision !== "number") return true;
  if (todoAuthorityKey(previousAuthority) !== todoAuthorityKey(nextAuthority)) return true;
  return nextRevision >= previousRevision;
}
