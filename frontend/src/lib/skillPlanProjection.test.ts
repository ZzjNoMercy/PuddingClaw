import assert from "node:assert/strict";
import test from "node:test";

// @ts-expect-error Node's native TypeScript runner requires the source suffix.
import { skillPlanGroupsFromToolCall } from "./skillPlanProjection.ts";

function plan(index: number) {
  return {
    ok: true,
    plan_id: `skill-plan-${index}`,
    plan_sha256: `${index}`.padStart(64, "0"),
    skill_name: `lark-${index}`,
    action: "install",
    status: "prepared",
    phase: "awaiting_confirmation",
    requires_confirmation: true,
    ui_commit_supported: true,
    source: `https://open.feishu.cn/.well-known/skills/lark-${index}`,
    diff: { added: Array.from({ length: 20 }, (_, file) => `references/file-${file}.md`) },
  };
}

test("full npx Skill Manager result projects to one batch card", () => {
  const output = JSON.stringify({
    ok: false,
    managed_by: "skill_management",
    intercepted: true,
    source: "https://open.feishu.cn",
    plans: Array.from({ length: 24 }, (_, index) => plan(index)),
    errors: [{}, {}, {}],
  });
  assert.ok(output.length > 2000);

  const groups = skillPlanGroupsFromToolCall({
    id: "call-feishu",
    tool: "execute",
    input: "{}",
    output,
    status: "done",
  });

  assert.equal(groups.length, 1);
  assert.equal(groups[0].plans.length, 24);
  assert.equal(groups[0].errorCount, 3);
  assert.equal(groups[0].source, "https://open.feishu.cn");
});

test("compact live confirmation envelope projects to the same batch", () => {
  const output = JSON.stringify({
    ok: false,
    managed_by: "skill_management",
    intercepted: true,
    event_kind: "skill_plan_batch_confirmation",
    source: "https://open.feishu.cn",
    error_count: 3,
    plans: Array.from({ length: 24 }, (_, index) => {
      const { diff: _diff, ...compact } = plan(index);
      return compact;
    }),
  });

  const groups = skillPlanGroupsFromToolCall({
    id: "call-feishu-live",
    tool: "execute",
    input: "{}",
    output,
    status: "done",
  });

  assert.equal(groups.length, 1);
  assert.equal(groups[0].plans.length, 24);
  assert.equal(groups[0].errorCount, 3);
});

test("truncated JSON never creates a misleading partial confirmation", () => {
  const full = JSON.stringify({
    managed_by: "skill_management",
    intercepted: true,
    plans: Array.from({ length: 24 }, (_, index) => plan(index)),
  });
  const groups = skillPlanGroupsFromToolCall({
    id: "call-truncated",
    tool: "execute",
    input: "{}",
    output: full.slice(0, 2000),
    status: "done",
  });

  assert.deepEqual(groups, []);
});
