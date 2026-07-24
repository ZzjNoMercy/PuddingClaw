import assert from "node:assert/strict";
import test from "node:test";

// @ts-expect-error Node's native TypeScript runner requires the source suffix.
import { buildFileTree, fileDirectoryPaths } from "./fileTree.ts";

test("builds directories from the files' real relative paths", () => {
  const files = [
    { path: "产品配置分析/model.md", relative_path: "model.md" },
    {
      path: "产品配置分析/references/report-generation.md",
      relative_path: "references/report-generation.md",
    },
    {
      path: "产品配置分析/references/examples/query.md",
      relative_path: "references/examples/query.md",
    },
  ];

  assert.deepEqual(buildFileTree(files), [
    {
      kind: "file",
      name: "model.md",
      path: "model.md",
      file: files[0],
    },
    {
      kind: "directory",
      name: "references",
      path: "references",
      children: [
        {
          kind: "file",
          name: "report-generation.md",
          path: "references/report-generation.md",
          file: files[1],
        },
        {
          kind: "directory",
          name: "examples",
          path: "references/examples",
          children: [
            {
              kind: "file",
              name: "query.md",
              path: "references/examples/query.md",
              file: files[2],
            },
          ],
        },
      ],
    },
  ]);
});

test("normalizes Windows separators and returns every containing directory", () => {
  assert.deepEqual(fileDirectoryPaths("references\\examples\\query.md"), [
    "references",
    "references/examples",
  ]);
});
