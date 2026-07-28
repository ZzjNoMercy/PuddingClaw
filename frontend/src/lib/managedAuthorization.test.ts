import assert from "node:assert/strict";
import test from "node:test";
// @ts-expect-error Node's native TypeScript runner requires the source suffix.
import {
  managedAuthorizationFromToolCall,
  managedAuthorizationRequests,
  splitTimelineAtManagedAuthorizations,
} from "./managedAuthorization.ts";

function toolCall(output: unknown) {
  return {
    id: "call-auth",
    tool: "execute",
    input: "{}",
    output: JSON.stringify(output),
    status: "done" as const,
  };
}

test("parses a structured phase-two authorization request", () => {
  const request = managedAuthorizationFromToolCall(toolCall({
    managed_by: "managed_cli",
    status: "awaiting_user_browser",
    authorization_request: {
      type: "managed_authorization_request",
      flow_id: "auth-123",
      provider: "lark",
      profile_id: "lark_default",
      status: "awaiting_user",
      phase: {
        id: "user_consent",
        step: 2,
        total: 2,
        title: "授权应用访问你的飞书数据",
        description: "这是用户身份授权。",
      },
      verification_url: "https://accounts.feishu.cn/oauth/v1/device/verify",
      user_code: "ABCD-EFGH",
      qr_ascii: "▀▄█\n".repeat(12),
      completion_hint: "完成后告诉我。",
    },
  }));

  assert.equal(request?.phase.step, 2);
  assert.equal(request?.user_code, "ABCD-EFGH");
});

test("rejects an authorization request with a lookalike host", () => {
  const request = managedAuthorizationFromToolCall(toolCall({
    authorization_request: {
      type: "managed_authorization_request",
      flow_id: "auth-123",
      provider: "lark",
      status: "awaiting_user",
      phase: {
        id: "app_configuration",
        step: 1,
        total: 2,
        title: "创建或绑定飞书应用",
        description: "应用配置。",
      },
      verification_url: "https://open.feishu.cn.evil.invalid/page/cli?user_code=BAD",
      completion_hint: "完成后告诉我。",
    },
  }));

  assert.equal(request, null);
});

test("accepts a terminal flow update without keeping its stale URL active", () => {
  const request = managedAuthorizationFromToolCall(toolCall({
    managed_by: "managed_cli",
    status: "failed",
    authorization_completed: false,
    authorization_request: {
      type: "managed_authorization_request",
      flow_id: "auth-123",
      revision: 2,
      provider: "lark",
      status: "failed",
      phase: {
        id: "user_consent",
        step: 2,
        total: 2,
        title: "授权应用访问你的飞书数据",
        description: "这是用户身份授权。",
      },
      verification_url: "https://accounts.feishu.cn/oauth/v1/device/verify?stale=true",
      completion_hint: "完成后告诉我。",
    },
  }));

  assert.equal(request?.status, "failed");
  assert.equal(request?.verification_url, undefined);
});

test("projects only the latest attempt for one stable flow", () => {
  const request = (attempt: number, code: string) => ({
    managed_by: "managed_cli",
    authorization_request: {
      type: "managed_authorization_request",
      flow_id: "auth-stable",
      revision: attempt,
      attempt,
      provider: "lark",
      status: "awaiting_user",
      phase: {
        id: "user_consent",
        step: 1,
        total: 1,
        title: "重新授权访问你的飞书数据",
        description: "只替换用户授权。",
      },
      verification_url: `https://accounts.feishu.cn/oauth/v1/device/verify?attempt=${attempt}`,
      user_code: code,
      completion_hint: "完成后告诉我。",
    },
  });
  const first = toolCall(request(1, "OLD"));
  const second = toolCall(request(2, "NEW"));
  const projected = managedAuthorizationRequests([
    { type: "tool", id: "timeline-old", toolCall: first },
    { type: "tool", id: "timeline-new", toolCall: second },
  ]);

  assert.equal(projected.length, 1);
  assert.equal(projected[0].attempt, 2);
  assert.equal(projected[0].user_code, "NEW");
  assert.equal(projected[0].phase.total, 1);
});

test("splits post-tool reasoning below the authorization card boundary", () => {
  const authorization = toolCall({
    managed_by: "managed_cli",
    authorization_request: {
      type: "managed_authorization_request",
      flow_id: "auth-order",
      provider: "lark",
      status: "awaiting_user",
      phase: {
        id: "user_consent",
        step: 2,
        total: 2,
        title: "授权应用访问你的飞书数据",
        description: "用户授权。",
      },
      verification_url: "https://accounts.feishu.cn/oauth/v1/device/verify",
      completion_hint: "完成后告诉我。",
    },
  });
  const timeline = [
    { type: "reasoning", id: "before", content: "before" } as const,
    { type: "tool", id: "auth", toolCall: authorization } as const,
    { type: "reasoning", id: "after", content: "after" } as const,
  ];

  const slices = splitTimelineAtManagedAuthorizations([...timeline]);

  assert.equal(slices.length, 2);
  assert.deepEqual(slices[0].timeline.map((item) => item.id), ["before", "auth"]);
  assert.equal(slices[0].authorization?.id, "auth");
  assert.deepEqual(slices[1].timeline.map((item) => item.id), ["after"]);
  assert.equal(slices[1].authorization, undefined);
});
