---
name: llm-wiki
description: Compile immutable raw knowledge snapshots into the Schema-bound LLM Wiki, query published Wiki knowledge, or lint the Wiki.
toolsets:
  - llm_wiki
  - gbrain_query
---

# LLM Wiki

For Query or Lint, call `llm_wiki_context` first with the matching operation and
treat the returned `AGENTS.md` and Schema Bundle as authoritative. User-facing
Ingest is queued through the intake tools below; its dedicated background Agent
loads the Ingest context itself.

For a user-facing Ingest request, the main Agent is only an orchestrator:

1. Call `llm_wiki_create_raw` with `source=current_message`, `attachments`, or
   `knowledge_file`. The server resolves the exact current input; never repeat a
   long document as a tool argument.
2. Pass the returned complete `raw_paths` and opaque `intake_id` unchanged to
   `llm_wiki_start_ingest`. This queues (or reuses) the existing durable task
   and returns immediately. Tell the user the task id and that progress is
   available in the task center.
   - If the user only says compile, organize, or turn the material into Wiki,
     omit `import_gbrain` or set it to `false`. The task stops after Wiki
     publish and Lint.
   - Set `import_gbrain=true` only when the user explicitly asks to enter,
     import, or sync gbrain. The same task then continues with the validated
     gbrain PostgreSQL import.

Do not call `llm_wiki_context`, `llm_wiki_publish`, or `llm_wiki_lint` from the
chat Session for Ingest. Those low-level protocol tools belong to the dedicated
background Compiler Agent, which reads only the selected Raw snapshots, applies
the active Schema, publishes complete pages, and runs Lint. It imports gbrain
only when the queued task explicitly requests that second stage.
Do not write `raw/`, `wiki/`, `index.md`, or `log.md` through generic filesystem
tools.

For an explicitly identified duplicate or obsolete published page, call
`llm_wiki_retire_pages` directly with the exact obsolete slug and an existing
replacement slug. This is a deterministic maintenance operation: it does not
queue the Compiler Agent or consume a compiler-model call. Do not infer a
retirement merely from similar titles; the user must explicitly authorize the
obsolete-to-replacement mapping. Do not read raw source files, list `/knowledge`,
or inspect the Wiki before this call: the tool atomically verifies that both the
obsolete page and replacement page satisfy the requested mapping. Keep
`sync_gbrain=false` unless the user also asks to remove the obsolete page from
gbrain. When enabled, gbrain performs a recoverable soft delete.
Treat the tool result as authoritative. `ok=true` with either `retired=true` or
`already_retired=true` completes the request; report the returned Lint and do
not call any generic filesystem tool to verify it.
If the retirement tool fails, report its exact error and stop. Do not use
generic filesystem tools to inspect `/knowledge` or the physical knowledge
root afterward: those tools intentionally have a different sandbox boundary
and their denial is not evidence that the dedicated Wiki service is unreadable.

For Query, treat the published Markdown LLM Wiki as the complete source of
truth and gbrain only as its structured acceleration/index layer. First call
`llm_wiki_context(operation="query")`, then always call `llm_wiki_query` with
the user's retrieval intent before deciding whether another knowledge path is
needed. Do not query gbrain by default. Use allowlisted gbrain MCP tools only
when entity relations, graph traversal, or structured filtering can materially
improve the answer. A gbrain hit never replaces or skips the Markdown Wiki
query; merge and deduplicate it as supplementary structure. Cite Wiki slugs
and their `sources`; report a knowledge gap instead of reading raw files.

For Lint, call `llm_wiki_lint`. Use `llm_wiki_compile` when the user asks to
verify gbrain compatibility. Neither operation repairs files.
