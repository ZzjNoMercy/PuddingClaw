import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { loadConfig } from "../src/config.js";
import { homePaths } from "../src/home.js";
import { applyProfile, inspectProfile } from "../src/profile-commands.js";

test("desktop profile inspection exposes explicit required and optional dependencies", async () => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "puddingclaw-profile-inspect-"));
  try {
    const result = await inspectProfile("full", homePaths(home), { packaged: false });
    assert.equal(result.profile, "full");
    assert.deepEqual(result.extensions, { knowledge: true, analytics: true, headless_worker: true });
    assert.ok(result.dependencies.some((item) => item.id === "runtime.python" && item.required));
    assert.ok(result.dependencies.some((item) => item.id === "knowledge.milvus" && !item.required));
    assert.ok(result.dependencies.some((item) => item.id === "analytics.datasource" && !item.required));
    assert.ok(result.dependencies.every((item) => item.source === "cli"));
    const userFacingCopy = result.dependencies
      .flatMap((item) => [item.label, item.detail, ...item.remediation])
      .join(" ");
    assert.doesNotMatch(userFacingCopy, /\bCLI\b/);
  } finally {
    await fs.rm(home, { recursive: true, force: true });
  }
});

test("desktop profile apply writes the selected extension set through the CLI config contract", async () => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "puddingclaw-profile-apply-"));
  const paths = homePaths(home);
  try {
    const result = await applyProfile("knowledge", paths);
    const config = await loadConfig(paths.config);
    assert.equal(result.status, "applied");
    assert.equal(config.initialized, true);
    assert.equal(config.profile, "knowledge");
    assert.equal(config.extensions.knowledge.enabled, true);
    assert.equal(config.extensions.analytics.enabled, false);
    assert.equal(config.extensions.headless_worker.enabled, true);
  } finally {
    await fs.rm(home, { recursive: true, force: true });
  }
});
