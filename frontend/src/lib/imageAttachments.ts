import type { AgentAttachment } from "./api";

export interface AttachmentPreviewSelection {
  sessionId: string;
  attachmentId: string;
}

export interface AttachmentMessageLike {
  attachments?: AgentAttachment[];
  outputAttachments?: AgentAttachment[];
}

export function collectSessionArtifacts(
  messages: AttachmentMessageLike[],
): Array<AgentAttachment & { id: string }> {
  const byId = new Map<string, AgentAttachment & { id: string }>();
  for (const message of messages) {
    for (const attachment of [
      ...(message.attachments || []),
      ...(message.outputAttachments || []),
    ]) {
      if (!attachment.id) continue;
      byId.set(attachment.id, { ...byId.get(attachment.id), ...attachment, id: attachment.id });
    }
  }
  return Array.from(byId.values());
}

export function isPreviewableImageAttachment(
  attachment: AgentAttachment,
): attachment is AgentAttachment & { id: string; preview_url: string } {
  return (
    attachment.type === "image" &&
    Boolean(attachment.id) &&
    Boolean(attachment.preview_url)
  );
}

export function collectPreviewableImageAttachments(
  messages: AttachmentMessageLike[],
): Array<AgentAttachment & { id: string; preview_url: string }> {
  return collectSessionArtifacts(messages).filter(isPreviewableImageAttachment);
}

export function resolveActiveImageAttachment(
  messages: AttachmentMessageLike[],
  selection: AttachmentPreviewSelection | null,
  sessionId: string,
): (AgentAttachment & { id: string; preview_url: string }) | null {
  if (!selection || selection.sessionId !== sessionId) return null;
  return (
    collectPreviewableImageAttachments(messages).find(
      (attachment) => attachment.id === selection.attachmentId,
    ) || null
  );
}

export function resolveActiveArtifact(
  messages: AttachmentMessageLike[],
  selection: AttachmentPreviewSelection | null,
  sessionId: string,
): (AgentAttachment & { id: string }) | null {
  if (!selection || selection.sessionId !== sessionId) return null;
  return (
    collectSessionArtifacts(messages).find(
      (artifact) => artifact.id === selection.attachmentId,
    ) || null
  );
}

export function isQrImageAttachment(attachment: AgentAttachment): boolean {
  return /(?:^|[\s_.-])(qr|qrcode)\d*(?:[\s_.-]|$)|二维码/i.test(
    attachment.name || "",
  );
}
