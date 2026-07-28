import type { TimelineItem, ToolCall } from "@/lib/store";

export interface ManagedAuthorizationRequest {
  type: "managed_authorization_request";
  flow_id: string;
  revision?: number;
  attempt?: number;
  provider: string;
  profile_id?: string;
  status: string;
  phase: {
    id: string;
    step: number;
    total: number;
    title: string;
    description: string;
  };
  verification_url?: string;
  user_code?: string;
  qr_ascii?: string;
  expires_at?: number;
  completion_hint: string;
}

function trustedAuthorizationUrl(raw: unknown, phaseId: string): string | null {
  if (typeof raw !== "string") return null;
  try {
    const url = new URL(raw);
    const host = url.hostname.toLowerCase();
    const appConfiguration = phaseId === "app_configuration";
    const trusted = appConfiguration
      ? ["open.feishu.cn", "open.larksuite.com"].includes(host) && url.pathname === "/page/cli"
      : ["accounts.feishu.cn", "accounts.larksuite.com"].includes(host)
        && url.pathname.startsWith("/oauth/v1/device/verify");
    return url.protocol === "https:" && trusted ? url.toString() : null;
  } catch {
    return null;
  }
}

export function managedAuthorizationFromToolCall(toolCall: ToolCall): ManagedAuthorizationRequest | null {
  if (toolCall.tool !== "execute" || !toolCall.output) return null;
  let envelope: unknown;
  try {
    envelope = JSON.parse(toolCall.output);
  } catch {
    return null;
  }
  if (!envelope || typeof envelope !== "object") return null;
  const request = (envelope as Record<string, unknown>).authorization_request;
  if (!request || typeof request !== "object") return null;
  const value = request as Record<string, unknown>;
  const phase = value.phase;
  if (!phase || typeof phase !== "object") return null;
  const phaseValue = phase as Record<string, unknown>;
  const phaseId = typeof phaseValue.id === "string" ? phaseValue.id : "";
  const status = typeof value.status === "string" ? value.status : "";
  const awaiting = status === "awaiting_user";
  const terminal = ["failed", "expired", "cancelled", "completed"].includes(status);
  const url = awaiting ? trustedAuthorizationUrl(value.verification_url, phaseId) : null;
  if (
    value.type !== "managed_authorization_request"
    || typeof value.flow_id !== "string"
    || value.provider !== "lark"
    || (!awaiting && !terminal)
    || (awaiting && !url)
    || !Number.isInteger(phaseValue.step)
    || !Number.isInteger(phaseValue.total)
    || typeof phaseValue.title !== "string"
    || typeof phaseValue.description !== "string"
    || typeof value.completion_hint !== "string"
  ) {
    return null;
  }
  return {
    type: "managed_authorization_request",
    flow_id: value.flow_id,
    revision: typeof value.revision === "number" ? value.revision : undefined,
    attempt: typeof value.attempt === "number" ? value.attempt : undefined,
    provider: value.provider,
    profile_id: typeof value.profile_id === "string" ? value.profile_id : undefined,
    status,
    phase: {
      id: phaseId,
      step: phaseValue.step as number,
      total: phaseValue.total as number,
      title: phaseValue.title,
      description: phaseValue.description,
    },
    verification_url: url ?? undefined,
    user_code: typeof value.user_code === "string" ? value.user_code : undefined,
    qr_ascii: typeof value.qr_ascii === "string" ? value.qr_ascii : undefined,
    expires_at: typeof value.expires_at === "number" ? value.expires_at : undefined,
    completion_hint: value.completion_hint,
  };
}

export function managedAuthorizationRequests(timeline: TimelineItem[]): ManagedAuthorizationRequest[] {
  const latest = new Map<string, ManagedAuthorizationRequest>();
  timeline
    .flatMap((item) => item.type === "tool" && item.toolCall
      ? [managedAuthorizationFromToolCall(item.toolCall)]
      : [])
    .filter((value): value is ManagedAuthorizationRequest => value !== null)
    .forEach((value) => {
      const key = `${value.flow_id}:${value.phase.id}`;
      const previous = latest.get(key);
      const previousOrder = [previous?.attempt ?? 0, previous?.revision ?? 0];
      const nextOrder = [value.attempt ?? 0, value.revision ?? 0];
      if (!previous || nextOrder[0] > previousOrder[0] || (
        nextOrder[0] === previousOrder[0] && nextOrder[1] >= previousOrder[1]
      )) {
        latest.set(key, value);
      }
    });
  return Array.from(latest.values());
}

export interface ManagedAuthorizationTimelineSlice {
  timeline: TimelineItem[];
  authorization?: TimelineItem;
}

/**
 * Split a model segment at each structured authorization tool result.
 *
 * One saved segment can contain pre-tool reasoning, the tool result, and
 * post-tool reasoning. Rendering a card after the whole segment therefore
 * puts the post-tool reasoning above the QR code. Each returned slice keeps
 * the authorization tool in its preceding trace and exposes that same item as
 * an out-of-trace card boundary; subsequent reasoning becomes the next slice.
 */
export function splitTimelineAtManagedAuthorizations(
  timeline: TimelineItem[],
): ManagedAuthorizationTimelineSlice[] {
  if (timeline.length === 0) return [];
  const slices: ManagedAuthorizationTimelineSlice[] = [];
  let start = 0;
  timeline.forEach((item, index) => {
    const authorization = item.type === "tool" && item.toolCall
      ? managedAuthorizationFromToolCall(item.toolCall)
      : null;
    if (!authorization) return;
    slices.push({
      timeline: timeline.slice(start, index + 1),
      authorization: item,
    });
    start = index + 1;
  });
  if (start < timeline.length) {
    slices.push({ timeline: timeline.slice(start) });
  }
  return slices.length > 0 ? slices : [{ timeline }];
}
