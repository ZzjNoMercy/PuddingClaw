import assert from "node:assert/strict";
import test from "node:test";

// @ts-expect-error Node's native TypeScript runner requires the source suffix.
import { larkAuthorizationDetails } from "./larkAuthorization.ts";

const qr = Array.from({ length: 21 }, (_, index) => (
  index % 2 === 0 ? "████ ▄▄▄▄▄ ████" : "████ █   █ ████"
)).join("\n");

test("extracts a QR and clean URL from the managed-cli JSON envelope", () => {
  const url = "https://open.feishu.cn/page/cli?user_code=ZVTU-VBEF&lpv=1.0.78&from=cli";
  const output = JSON.stringify({
    managed_by: "managed_cli",
    status: "awaiting_user_browser",
    output: [
      "Managed browser authorization started.",
      "Status: awaiting_user_browser",
      qr,
      `  ${url}`,
      "等待配置应用...",
    ].join("\n\n"),
  });

  const details = larkAuthorizationDetails(output);

  assert.equal(details?.url, url);
  assert.equal(details?.qr.split("\n").length, 21);
  assert.equal(details?.url.includes("等待配置应用"), false);
  assert.equal(details?.url.includes("\\n"), false);
});

test("rejects a lookalike authorization origin", () => {
  const output = [
    "Status: awaiting_user_browser",
    qr,
    "https://evil.invalid/page/cli?user_code=ZVTU-VBEF",
  ].join("\n");

  assert.equal(larkAuthorizationDetails(output), null);
});
