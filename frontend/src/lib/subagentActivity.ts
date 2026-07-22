export interface SubagentActivityEventData {
  subagent_run_id?: unknown;
  tool_call_id?: unknown;
  tool?: unknown;
  stage?: unknown;
}

export interface SubagentActivityIdentity {
  activityId: string;
  settlePrefix: string;
  terminal: boolean;
  statusOverride?: string;
}

const TERMINAL_EVENTS = new Set([
  "subagent_completed",
  "subagent_blocked",
  "subagent_timed_out",
  "subagent_failed",
  "subagent_cancelled",
]);

const LIFECYCLE_EVENTS = new Set([
  "subagent_started",
  "subagent_completed",
  "subagent_blocked",
  "subagent_timed_out",
  "subagent_failed",
  "subagent_cancelled",
]);

/** Correlate start/end events to one durable UI activity identity. */
export function getSubagentActivityIdentity(
  eventType: string,
  data: SubagentActivityEventData,
): SubagentActivityIdentity {
  const subagentRunId = String(data.subagent_run_id || "current");
  const settlePrefix = `subagent-${subagentRunId}-`;

  if (LIFECYCLE_EVENTS.has(eventType)) {
    return {
      activityId: `${settlePrefix}lifecycle`,
      settlePrefix,
      terminal: TERMINAL_EVENTS.has(eventType),
    };
  }
  if (eventType === "context_mounted") {
    return {
      activityId: `${settlePrefix}context`,
      settlePrefix,
      terminal: false,
    };
  }
  if (eventType === "subagent_stage_changed") {
    return {
      activityId: `${settlePrefix}stage`,
      settlePrefix,
      terminal: false,
    };
  }
  if (eventType === "subagent_tool_started"
    || eventType === "subagent_tool_completed"
    || eventType === "subagent_tool_failed") {
    const toolIdentity = String(data.tool_call_id || data.tool || "current");
    return {
      activityId: `${settlePrefix}tool-${toolIdentity}`,
      settlePrefix,
      terminal: false,
    };
  }
  if (eventType === "subagent_fallback_to_parent") {
    return {
      activityId: `${settlePrefix}fallback`,
      settlePrefix,
      terminal: true,
      // The handoff event itself is complete. Parent progress remains visible
      // in the Run status pill rather than as an immortal subagent spinner.
      statusOverride: "completed",
    };
  }
  return {
    activityId: `${settlePrefix}${eventType || "activity"}`,
    settlePrefix,
    terminal: false,
  };
}
