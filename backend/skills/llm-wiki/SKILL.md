---
name: llm-wiki
description: Compile immutable raw knowledge snapshots into the Schema-bound LLM Wiki, query published Wiki knowledge, or lint the Wiki.
toolsets:
  - llm_wiki
  - gbrain_query
---

# LLM Wiki

Always call `llm_wiki_context` first with exactly one operation: `ingest`,
`query`, or `lint`. Treat the returned `AGENTS.md` as the operation contract and
the returned Schema Bundle as authoritative.

For Ingest, read only the selected raw snapshots returned by the context tool.
Generate complete Wiki pages with the required frontmatter, then call
`llm_wiki_publish` with the unchanged bundle hash and raw paths. Do not write
`raw/`, `wiki/`, `index.md`, or `log.md` through generic filesystem tools.

For Query, prefer the allowlisted gbrain MCP tools when available. Otherwise
use `llm_wiki_query`. Cite Wiki slugs and their `sources`; report a knowledge
gap instead of reading raw files.

For Lint, call `llm_wiki_lint`. Use `llm_wiki_compile` when the user asks to
verify gbrain compatibility. Neither operation repairs files.
