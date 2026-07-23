import assert from "node:assert/strict";
import test from "node:test";

// @ts-expect-error Node's native TypeScript runner requires the source suffix.
import {
  settleRunningVerificationActivities,
  verificationFailureActivity,
} from "./verificationActivity.ts";

test("terminal verification settles every older repair spinner", () => {
  const settled = settleRunningVerificationActivities(
    [
      {
        type: "activity",
        id: "verification-completion-run-1",
        label: "正在自动继续修复",
        status: "running",
      },
      {
        type: "activity",
        id: "subagent-run-1-lifecycle",
        label: "子代理执行中",
        status: "running",
      },
      {
        type: "activity",
        id: "verification-completion-run-0",
        label: "完成条件检查通过",
        status: "satisfied",
      },
    ],
    "failed",
  );

  assert.equal(settled[0].status, "failed");
  assert.equal(settled[1].status, "running");
  assert.equal(settled[2].status, "satisfied");
});

test("deterministic repair copy only promises continuation when event says so", () => {
  assert.deepEqual(
    verificationFailureActivity("deterministic_checks_completed", "needs_revision", true),
    {
      willContinue: true,
      label: "发现完成条件缺口，正在自动继续修复",
      detail: "无需操作，Agent 会保留当前进度并继续处理。",
      displayStatus: "running",
    },
  );
  assert.deepEqual(
    verificationFailureActivity("deterministic_checks_completed", "failed", false, true),
    {
      willContinue: false,
      label: "完成条件仍有缺口，自动处理已停止",
      detail: "Goal、Todo 和证据已保留。发送“继续完成剩余工作”即可从当前进度继续。",
      summary: [
        "**还有未完成项，但当前进度没有丢失。**",
        "",
        "Goal、Todo、产物和证据均已保留。请发送 **“继续完成剩余工作”**，系统会启动新的 Goal Run 并从当前进度继续。",
      ].join("\n"),
      displayStatus: "failed",
    },
  );
});

test("rubric terminal failure does not claim that another repair will run", () => {
  assert.deepEqual(
    verificationFailureActivity("rubric_evaluation_end", "failed"),
    {
      willContinue: false,
      label: "完成质量仍有缺口，自动处理已停止",
      detail: "请查看右侧验收明细，再发送具体修复要求。",
      summary: [
        "**还有未完成项。**",
        "",
        "请打开右侧 **“验收”** 查看具体缺口，然后发送对应的修复要求。",
      ].join("\n"),
      displayStatus: "failed",
    },
  );
});

test("infrastructure failure asks for a retry without blaming the artifact", () => {
  assert.deepEqual(
    verificationFailureActivity(
      "deterministic_checks_completed",
      "infrastructure_error",
      false,
      true,
    ),
    {
      willContinue: false,
      label: "验收服务异常，自动处理已停止",
      detail: "这不代表业务产物未通过。请发送“重试验收”继续。",
      summary: [
        "**验收流程遇到异常，任务结果尚未被判定失败。**",
        "",
        "当前进度和证据已保留。请发送 **“重试验收”**；如果再次出现，可在右侧“验收”中查看技术明细。",
      ].join("\n"),
      displayStatus: "failed",
    },
  );
});
