---
name: puddingclaw
description: Use the local PuddingClaw Worker CLI for enterprise data questions, configured analytics, NL2SQL, knowledge queries, metric analysis, and attribution. Use when the user wants an installed agent such as Pi, OpenClaw, or Codex to delegate a data-analysis task to PuddingClaw. Do not use for public-web research or when the requested data is not configured in PuddingClaw.
---

# PuddingClaw Worker

Delegate enterprise-data work to the local `puddingclaw` Node CLI. The CLI is a Worker boundary, not an LLM command: its `--model` value is a PuddingClaw **analytics model** discovered from the connected PuddingClaw instance.

## Host contract

- Executable: `puddingclaw`
- Distribution: `@pudding/worker-puddingclaw` (Node.js 20+)
- Authentication: Worker Access Key in `PUDDINGCLAW_TOKEN`
- Machine protocol: JSON on stdin and one JSON object on stdout

Treat the CLI as a managed connector when the host supports connectors or adapters. Invoke it with an argv array and stdin; do not construct a shell command from user content.

## Preflight

1. Run `puddingclaw version --json` to detect the executable.
2. If absent, ask before installing. Prefer the host's trusted package installer or the package bundled by a PuddingClaw deployment. Do not guess a registry, URL, or unpublished package source.
3. Run `puddingclaw doctor --json`.
4. If PuddingClaw is unreachable, ask the user to start the local PuddingClaw service.
5. If the Worker Access Key is missing or rejected, follow the credential workflow below and rerun `doctor`.

Do not repeatedly call `doctor` after the same failure.

## Credential workflow

Prefer the hosting Agent or Platform's protected credential store. It should:

1. Explain that a PuddingClaw **Worker Access Key** is required. It is not an LLM provider key.
2. Ask for it through the host's protected secret-input UI.
3. Save it in the host's Connector Credential store, OS keychain, or equivalent encrypted secret store.
4. Inject it only into the `puddingclaw` child process as `PUDDINGCLAW_TOKEN`.

If the host has no credential store, use the local env-file fallback:

1. Locate `.env.example` next to this `SKILL.md`.
2. Copy it to `.env` in the same Skill directory only if `.env` does not exist.
3. Set `.env` to mode `0600` on POSIX systems.
4. Ask the user to enter the Worker Access Key through protected input or edit this local file directly.
5. Use `node <skill-dir>/scripts/run.mjs doctor --json` to verify the connection.

The launcher parses only `PUDDINGCLAW_TOKEN`, injects it into the child-process environment, and executes the real `puddingclaw` binary without a shell. Do not source `.env` as a shell script, and never overwrite it during Skill updates. The CLI remains independent of Skill installation paths.

Never put a Worker Access Key in argv, prompt text, stdout, logs, summaries, or error messages. Never echo it back.

## Select an analytics model

Run:

```text
puddingclaw models list --json
```

When using the Skill-local `.env`, replace `puddingclaw` with `node <skill-dir>/scripts/run.mjs` in this and subsequent command shapes.

Use only an identifier returned by this command. If the host has not already bound a model, show the available analytics models to the user and ask which one to use. Do not call it an LLM model and do not substitute a provider model name.

## Run a task

Invoke exactly this command shape:

```text
argv:  ["puddingclaw", "run", "--input-json", "-", "--json"]
stdin: {"message":"...","model":"<analytics model>"}
```

Optional stdin fields:

- `session_id`: continue a PuddingClaw Worker session.
- `request_id`: idempotency key for a retried submission.
- `metadata`: host-owned correlation metadata; never include secrets.

While the process is running, show a host-side state such as “PuddingClaw 正在处理”. The current CLI is intentionally non-streaming; do not parse partial stdout as progress.

## Preserve task continuity

Treat `session_id` as host-owned state for one logical task or conversation thread:

1. For a new task, omit `session_id` and read it from the completed CLI JSON response.
2. Store the returned `session_id` with the host's Task/Thread record, together with `session_expires_at` when present.
3. For a follow-up, correction, or retry that requires prior context, send that same `session_id` in stdin JSON.
4. For an unrelated task, start a fresh Session. Never derive a default Session from the Worker Key name or share one Session across tasks.
5. Do not store `session_id` in `.env`; `.env` is only a credential fallback and Session identity changes per task.

Headless Sessions expire after the Backend's inactivity TTL, which defaults to 24 hours and is refreshed whenever the Session is updated. If `session_expires_at` has passed, start a new Session and include the relevant prior context in the new message. If CLI JSON returns `outcome=session_expired` (`error_code=session_expired`, HTTP `410`), remove the stale mapping and do the same; never claim that the expired Session's hidden context was preserved.

For direct human use, the equivalent continuation flag is:

```text
puddingclaw run "继续刚才的分析" --model <analytics model> --session <session_id> --json
```

Machine integrations should continue using stdin JSON rather than constructing a shell command.

## Interpret the result

Parse stdout only after the process exits. It contains one JSON object.

- `status == "completed"`: present `final_response` as the Worker answer. Fall back to `reply` only for an older compatible Worker that omits `final_response`.
- `reply`: aggregated visible content that may include intermediate assistant narration. Do not prefer it over `final_response`.
- `needs_input != null`: show the question or approval request, collect the user's answer, and continue with the returned `session_id` according to the payload.
- `outcome == "session_expired"`: remove the Task/Thread mapping and create a new Session with the relevant visible context restated in the message.
- `status == "error"` or a nonzero exit: summarize the structured error without exposing configuration values.
- Preserve `run_id` and `session_id` in host metadata for tracing and follow-up, not in the main user-facing answer unless useful.
- Preserve `session_expires_at` with the mapping when returned; it is lifecycle metadata, not user content.

Do not claim success solely because the process returned JSON; check `status` and `outcome`.

## Cancellation and safety

- Forward user cancellation as `SIGINT`; exit code `130` means cancelled.
- Never auto-answer a `needs_input` request that requires user judgment or approval.
- Respect the Worker's own `approval_mode` and interrupt result. Do not bypass it by rewriting the task.
- Use a fresh `request_id` for a new intent and reuse it only when retrying the same submission.
