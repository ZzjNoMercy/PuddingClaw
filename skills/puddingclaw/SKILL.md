---
name: puddingclaw
description: Use the local PuddingClaw Worker CLI for enterprise data questions, configured analytics, NL2SQL, knowledge queries, metric analysis, and attribution. Use when the user wants an installed agent such as Pi, OpenClaw, or Codex to delegate a data-analysis task to PuddingClaw. Do not use for public-web research or when the requested data is not configured in PuddingClaw.
---

# PuddingClaw Worker

Delegate enterprise-data work to the local `puddingclaw` Node CLI. Keep the CLI thin: send the user's question and task lifecycle metadata only. PuddingClaw Backend chooses an allowed **analytics model** from the question; this is a business capability boundary, not an LLM provider model.

## Host contract

- Executable: `puddingclaw`
- Distribution: `@puddingai/puddingclaw` (Node.js 20+)
- Authentication: none at the CLI boundary; the local PuddingClaw Backend owns model, data, and tool authorization
- Machine protocol: JSON on stdin; choose one final JSON (`--json`) or public event JSONL (`--jsonl`) on stdout

Treat the CLI as a managed connector when the host supports connectors or adapters. Invoke it with an argv array and stdin; do not construct a shell command from user content.

## Preflight

1. Run `puddingclaw version --json` to detect the executable.
2. If absent, ask before installing. Prefer the host's trusted package installer or the package bundled by a PuddingClaw deployment. Do not guess a registry, URL, or unpublished package source.
3. Run `puddingclaw doctor --json`.
4. If PuddingClaw is unreachable, ask the user to start the local PuddingClaw service.
5. Do not ask for or configure a PuddingClaw Token. If the Backend rejects a request, report the local service error and stop.

Do not repeatedly call `doctor` after the same failure.

## Inspect available capabilities

Run:

```text
puddingclaw agent models list --json
```

This command is informational. Do not select a model, pass a model identifier, or ask the user to configure an LLM model. The Backend owns the configured analytics-model catalog and routes the question before starting a new Session.

## Run a task

When the host can consume incremental events, invoke:

```text
argv:  ["puddingclaw", "agent", "run", "--input-json", "-", "--jsonl"]
stdin: {"message":"..."}
```

Parse stdout one complete line at a time. Public events include lifecycle, visible assistant `token`/segment updates, `tool_start`/`tool_end`, permission boundaries, `final_response`, `done`, and exactly one terminal `result`. Do not expect reasoning, provider previews, or internal Trace events. Persist the last processed event sequence or host event ID when the adapter provides one, and keep the host's own durable Run state as the disconnect-recovery source.

If the host only supports a blocking tool result, use the same command with `--json`; stdout then contains one final JSON object. Never mix the two parsers.

Optional stdin fields:

- `session_id`: continue a PuddingClaw Worker session.
- `request_id`: idempotency key for a retried submission.
- `metadata`: host-owned correlation metadata; never include secrets.

Show a host-side “PuddingClaw 正在处理” state immediately after spawn. Then project JSONL tool/content/HITL events as real Worker progress. Do not expose stderr as progress or model context.

## Preserve task continuity

Treat `session_id` as host-owned state for one logical task or conversation thread:

1. For a new task, omit `session_id` and read it from the completed CLI JSON response.
2. Store the returned `session_id` with the host's Task/Thread record, together with `session_expires_at` when present.
3. For a follow-up, correction, or retry that requires prior context, send that same `session_id` in stdin JSON.
4. For an unrelated task, start a fresh Session. Never derive a default Session from the caller name or share one Session across tasks.

Headless Sessions expire after the Backend's inactivity TTL, which defaults to 24 hours and is refreshed whenever the Session is updated. If `session_expires_at` has passed, start a new Session and include the relevant prior context in the new message. If CLI JSON returns `outcome=session_expired` (`error_code=session_expired`, HTTP `410`), remove the stale mapping and do the same; never claim that the expired Session's hidden context was preserved.

For direct human use, the equivalent continuation flag is:

```text
puddingclaw agent run "继续刚才的分析" --session <session_id> --json
```

Machine integrations should continue using stdin JSON rather than constructing a shell command.

## Interpret the result

For `--json`, parse stdout after exit as one object. For `--jsonl`, consume complete lines during execution and interpret the final `event=result` payload with the same rules below.

- `status == "completed"`: present `final_response` as the Worker answer. Fall back to `reply` only for an older compatible Worker that omits `final_response`.
- `analytics_model_id` and `analytics_model_match`: Backend-selected audit output. Preserve them for tracing; never feed them back as CLI input. `analytics_model_match.status == "general"` means the Backend judged the question to be non-analytics (e.g. small talk or general knowledge) and answered it without binding an analytics model; in that case `analytics_model_id` is empty. This is a normal completed answer, not an error.
- `reply`: aggregated visible content that may include intermediate assistant narration. Do not prefer it over `final_response`.
- `outcome == "analytics_model_clarification_required"`: ask the user to clarify the actual business object, metric, time range, or analysis scenario, then submit the clarified question as a new request. Do not ask the user for a model ID and do not start a Session until the Backend finds one unique match.
- `outcome == "analytics_model_unavailable"`: explain that PuddingClaw has no usable configured model or its Session-bound model is no longer available; ask the user to update PuddingClaw configuration. Do not silently switch a continuous Session to another model.
- `needs_input != null`: show the approval request and collect an explicit user decision. Resume the same Run with `puddingclaw agent respond <run_id> --input-json - --jsonl`, passing `continuation_token`, a fresh idempotent `request_id`, and the complete `decisions` array. Use `--json` instead only for a blocking host.
- `outcome == "session_expired"`: remove the Task/Thread mapping and create a new Session with the relevant visible context restated in the message.
- `status == "error"` or a nonzero exit: summarize the structured error without exposing configuration values.
- Preserve `run_id` and `session_id` in host metadata for tracing and follow-up, not in the main user-facing answer unless useful.
- Preserve `session_expires_at` with the mapping when returned; it is lifecycle metadata, not user content.

Do not claim success solely because the process returned JSON; check `status` and `outcome`.

## Cancellation and safety

- Forward user cancellation as `SIGINT` or supervisor `SIGTERM`. Once `run_id` is known, follow process termination with `puddingclaw agent cancel <run_id> --json`; killing the local observer alone does not prove the Backend Run stopped. Exit code `130` means the local CLI was cancelled.
- Never auto-answer a `needs_input` request that requires user judgment or approval.
- Respect the Worker's own `approval_mode` and interrupt result. Do not bypass it by rewriting the task.
- Use a fresh `request_id` for a new intent and reuse it only when retrying the same submission.
