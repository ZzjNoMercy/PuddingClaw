import assert from "node:assert/strict";
import test from "node:test";

// @ts-ignore Node's native TypeScript runner requires the source suffix.
import { normalizeLooseStrongMarkdown } from "./markdown.ts";

test("repairs whitespace before closing strong markers", () => {
  assert.equal(
    normalizeLooseStrongMarkdown("页面以**无障碍树（accessibility tree） **表示"),
    "页面以**无障碍树（accessibility tree）**表示",
  );
  assert.equal(normalizeLooseStrongMarkdown("支持__视觉分析 __能力"), "支持__视觉分析__能力");
});

test("does not change valid emphasis or code", () => {
  const input = [
    "**已经正确**，以及 `**行内代码 **`。",
    "```md",
    "**围栏代码 **",
    "```",
  ].join("\n");
  assert.equal(normalizeLooseStrongMarkdown(input), input);
});

test("repairs only the malformed strong span when valid spans share a line", () => {
  assert.equal(
    normalizeLooseStrongMarkdown("**正确** 和 **需要修复 ** 都保留"),
    "**正确** 和 **需要修复** 都保留",
  );
});
