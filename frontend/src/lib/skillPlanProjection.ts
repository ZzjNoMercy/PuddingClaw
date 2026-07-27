import type { SkillPlan } from "./api";
import type { ToolCall } from "./store";

export interface SkillPlanGroup {
  id: string;
  plans: SkillPlan[];
  source?: string;
  errorCount?: number;
}

function isSkillPlan(value: Partial<SkillPlan> & { ok?: boolean }): value is SkillPlan {
  return (
    value.ok === true
    && typeof value.plan_id === "string"
    && typeof value.plan_sha256 === "string"
    && typeof value.skill_name === "string"
    && typeof value.phase === "string"
    && typeof value.requires_confirmation === "boolean"
    && value.ui_commit_supported === true
    && (value.action === "install" || value.action === "update")
  );
}

export function skillPlanGroupsFromToolCall(toolCall: ToolCall): SkillPlanGroup[] {
  if (toolCall.status === "running" || !toolCall.output) return [];
  try {
    const value = JSON.parse(toolCall.output) as Record<string, unknown>;
    if (["prepare_skill_install", "prepare_skill_update"].includes(toolCall.tool)) {
      const plan = value as unknown as Partial<SkillPlan> & { ok?: boolean };
      return !toolCall.is_error && isSkillPlan(plan)
        ? [{ id: plan.plan_id, plans: [plan] }]
        : [];
    }
    if (
      toolCall.tool !== "execute"
      || value.managed_by !== "skill_management"
      || value.intercepted !== true
      || !Array.isArray(value.plans)
    ) return [];
    const plans = value.plans.filter((plan): plan is SkillPlan => (
      typeof plan === "object"
      && plan !== null
      && isSkillPlan(plan as Partial<SkillPlan> & { ok?: boolean })
    ));
    return plans.length > 0 ? [{
      id: `npx-skills-add:${toolCall.id || plans[0].plan_id}`,
      plans,
      source: typeof value.source === "string" ? value.source : undefined,
      errorCount: typeof value.error_count === "number"
        ? value.error_count
        : Array.isArray(value.errors)
          ? value.errors.length
          : 0,
    }] : [];
  } catch {
    return [];
  }
}
