import assert from "node:assert/strict";
import test from "node:test";

// @ts-ignore Node's native TypeScript runner requires the source suffix.
import { placeOutputAttachments } from "./artifactPlacement.ts";

test("places a persisted artifact beside its producing tool segment", () => {
  const attachment = {
    id: "att_screen",
    type: "image" as const,
    created_by_tool_call_id: "call_screen",
  };
  const placement = placeOutputAttachments(
    [attachment],
    [
      { toolCalls: [{ id: "call_nav" }] },
      { toolCalls: [{ id: "call_screen" }] },
    ],
  );

  assert.deepEqual(placement.bySegment, [[], [attachment]]);
  assert.deepEqual(placement.unplaced, []);
});

test("infers placement for legacy artifacts from the tool output receipt", () => {
  const attachment = { id: "att_legacy", type: "image" as const };
  const placement = placeOutputAttachments(
    [attachment],
    [{ toolCalls: [{ id: "call_screen", output: '{"artifact":{"id":"att_legacy"}}' }] }],
  );

  assert.deepEqual(placement.bySegment, [[attachment]]);
  assert.deepEqual(placement.unplaced, []);
});

test("keeps unattached artifacts visible as a message-level fallback", () => {
  const attachment = { id: "att_unknown", type: "file" as const };
  const placement = placeOutputAttachments([attachment], [{ toolCalls: [] }]);

  assert.deepEqual(placement.bySegment, [[]]);
  assert.deepEqual(placement.unplaced, [attachment]);
});
