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

## Interpret the result

Parse stdout only after the process exits. It contains one JSON object.

- `status == "completed"`: present `final_response` as the Worker answer. Fall back to `reply` only for an older compatible Worker that omits `final_response`.
- `reply`: aggregated visible content that may include intermediate assistant narration. Do not prefer it over `final_response`.
- `needs_input != null`: show the question or approval request, collect the user's answer, and continue with the returned `session_id` according to the payload.
- `status == "error"` or a nonzero exit: summarize the structured error without exposing configuration values.
- Preserve `run_id` and `session_id` in host metadata for tracing and follow-up, not in the main user-facing answer unless useful.

Do not claim success solely because the process returned JSON; check `status` and `outcome`.

## Cancellation and safety

- Forward user cancellation as `SIGINT`; exit code `130` means cancelled.
- Never auto-answer a `needs_input` request that requires user judgment or approval.
- Respect the Worker's own `approval_mode` and interrupt result. Do not bypass it by rewriting the task.
- Use a fresh `request_id` for a new intent and reuse it only when retrying the same submission.
