import assert from "node:assert/strict";
import test from "node:test";

// @ts-ignore Node's native TypeScript runner requires the source suffix.
import { collectPreviewableImageAttachments, collectSessionArtifacts, isQrImageAttachment, resolveActiveArtifact, resolveActiveImageAttachment } from "./imageAttachments.ts";

const screenshot = {
  id: "att_screen",
  type: "image" as const,
  name: "screen.png",
  preview_url: "/api/attachments/att_screen/preview?session_id=a",
};

test("collects only verified structured image previews and deduplicates by id", () => {
  assert.deepEqual(
    collectPreviewableImageAttachments([
      { attachments: [screenshot, { id: "file", type: "file", name: "a.txt" }] },
      { outputAttachments: [{ ...screenshot, width: 1200 }] },
    ]),
    [{ ...screenshot, width: 1200 }],
  );
});

test("collects the complete Session artifact inventory", () => {
  assert.deepEqual(
    collectSessionArtifacts([
      { attachments: [screenshot] },
      { outputAttachments: [{ id: "att_md", type: "markdown", name: "report.md", download_url: "/download" }] },
    ]).map((artifact) => artifact.name),
    ["screen.png", "report.md"],
  );
});

test("preview selection cannot escape its session", () => {
  const messages = [{ outputAttachments: [screenshot] }];
  const selection = { sessionId: "a", attachmentId: "att_screen" };
  assert.equal(resolveActiveImageAttachment(messages, selection, "b"), null);
  assert.equal(resolveActiveImageAttachment(messages, selection, "a")?.id, "att_screen");
});

test("artifact detail resolves Markdown within the owning Session", () => {
  const markdown = { id: "att_md", type: "markdown" as const, name: "report.md", download_url: "/download" };
  const messages = [{ outputAttachments: [markdown] }];
  assert.equal(resolveActiveArtifact(messages, { sessionId: "a", attachmentId: "att_md" }, "a")?.name, "report.md");
  assert.equal(resolveActiveArtifact(messages, { sessionId: "a", attachmentId: "att_md" }, "b"), null);
});

test("recognizes QR filenames without treating every square image as a QR code", () => {
  assert.equal(isQrImageAttachment({ type: "image", name: "login_qr-code.png" }), true);
  assert.equal(isQrImageAttachment({ type: "image", name: "ai_qr2.png" }), true);
  assert.equal(isQrImageAttachment({ type: "image", name: "二维码.png" }), true);
  assert.equal(isQrImageAttachment({ type: "image", name: "avatar.png", width: 256, height: 256 }), false);
});
