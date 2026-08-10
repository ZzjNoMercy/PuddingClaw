import type { AgentAttachment } from "./api";

interface ToolCallLike {
  id?: string;
  output?: string;
}

interface TimelineToolLike {
  type: string;
  toolCall?: ToolCallLike;
}

interface SegmentLike {
  toolCalls?: ToolCallLike[];
  timeline?: TimelineToolLike[];
}

export interface OutputAttachmentPlacement {
  bySegment: AgentAttachment[][];
  unplaced: AgentAttachment[];
}

function segmentToolCalls(segment: SegmentLike): ToolCallLike[] {
  const calls = [...(segment.toolCalls || [])];
  for (const item of segment.timeline || []) {
    if (item.type !== "tool" || !item.toolCall) continue;
    if (!calls.some((call) => call.id && call.id === item.toolCall?.id)) {
      calls.push(item.toolCall);
    }
  }
  return calls;
}

/** Keep generated artifacts beside the model/tool segment that produced them. */
export function placeOutputAttachments(
  attachments: AgentAttachment[] = [],
  segments: SegmentLike[] = [],
  messageToolCalls: ToolCallLike[] = [],
): OutputAttachmentPlacement {
  const bySegment = segments.map(() => [] as AgentAttachment[]);
  const callsBySegment = segments.map(segmentToolCalls);
  const allCalls = [...messageToolCalls, ...callsBySegment.flat()];
  const unplaced: AgentAttachment[] = [];

  for (const attachment of attachments) {
    let toolCallId = attachment.created_by_tool_call_id || "";
    if (!toolCallId && attachment.id) {
      toolCallId = allCalls.find(
        (call) => call.id && String(call.output || "").includes(attachment.id || "")
      )?.id || "";
    }
    const segmentIndex = toolCallId
      ? callsBySegment.findIndex((calls) => calls.some((call) => call.id === toolCallId))
      : -1;
    if (segmentIndex >= 0) {
      bySegment[segmentIndex].push(attachment);
    } else {
      unplaced.push(attachment);
    }
  }

  return { bySegment, unplaced };
}
