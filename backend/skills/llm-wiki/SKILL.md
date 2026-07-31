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

For Query, prefer the allowlisted gbrain MCP tools when available. Otherwise
use `llm_wiki_query`. Cite Wiki slugs and their `sources`; report a knowledge
gap instead of reading raw files.

For Lint, call `llm_wiki_lint`. Use `llm_wiki_compile` when the user asks to
verify gbrain compatibility. Neither operation repairs files.
