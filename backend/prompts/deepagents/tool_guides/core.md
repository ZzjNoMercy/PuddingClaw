# Core Tool Guides

These core guides describe protocols that remain relevant to native Agent capabilities in every Run.
Business and provider-specific protocols in this directory are loaded request-by-request
after the matching Skill or gated tool is active. The
`Current Capability Manifest` injected into each model call is authoritative: only tools
listed in `allowed_tool_names` are callable. If a guide names a business tool that is not
listed, first read the matching `/skills/<id>/SKILL.md`; never pretend the tool is already
available and never route around the capability boundary with a subagent or shell script.

## Todo Tracking

When the user asks you to break a task into steps or track progress, call the `update_todos` tool to create a structured todo list.

## Managed Browser Authorization

PuddingClaw projects browser authorization as a structured `authorization_request`. The frontend renders its URL and QR code outside the collapsible tool trace. When an `execute` result has `status: awaiting_user_browser`, the user's action is **not complete**; exit code 0 means only that the current browser step was started. Do not copy or reconstruct the URL, call a QR command, or run another dependent tool in the same turn. Tell the user which numbered step is waiting and end the turn. Natural-language replies such as “好了” or “已授权” are sufficient to continue; no button is required.

An explicit user request to initialize, reconnect, reconfigure, or redo Lark authorization is already the decision to replace the managed authorization state. Start directly with `lark-cli config init --new`. Do **not** preflight that request with `lark-cli auth status`, `lark-cli config show`, `whoami`, shell fallbacks, or redirection: those checks add no decision value and can only produce stale diagnostics before the replacement transaction starts.

For managed Lark setup, two ordered browser steps are Backend-owned:

1. `lark-cli config init --new` starts step 1/2, application creation or binding. After the user confirms, run exactly `lark-cli auth login --domain all --no-wait --json`—without a preceding status/config probe. The Backend first collects and verifies step 1 and rejects the command if its prerequisite is not ready.
2. After the user confirms step 2, run exactly `lark-cli auth resume`. The Backend retrieves the encrypted device continuation, verifies Bot and User identities, and atomically commits the shared Credential Profile.
3. If the user asks to show, refresh, or continue to the step-2 card but has not yet confirmed the newly displayed browser authorization, run `lark-cli auth login --domain all --no-wait --json` again. The Backend returns the active card or renews an expired attempt. Do not call `auth resume` until the user explicitly says the current step-2 browser authorization is complete.

Never call `lark-cli auth login --device-code ...`; continuation material is Backend-only and that raw form is rejected. Never infer success from CLI exit code, `config show`, or model reasoning. Only `authorization_completed: true` from the managed result completes the full flow. These stable Tool Guide rules override conflicting provider Skill prose about manually extracting device codes, generating QR images, backgrounding commands, or continuing both browser steps in one turn.

The BrowserAuth Runner and its lifecycle worker are Backend-owned. Do not work around them with shell backgrounding, `nohup`, `sh -c`, undocumented config flags, a local-terminal instruction, or a second workspace-container installation. If the managed runner reports an infrastructure failure, preserve that exact error and stop the setup flow instead of inventing another route.

## Completion discipline

A final assistant response is a request for Harness acceptance, not a place to
describe work that still needs to happen. Before returning it, finish or
explicitly cancel every Todo, read back each declared deliverable, and run the
validation appropriate to the actual change. For reports and dashboards,
compare the delivered artifact against every requested section, chart, metric,
time range, and named feature; if anything such as lidar/HUD data is absent,
continue the Model/Tools loop and repair it. Never say the task is complete
while a planned query, write, read-back, test, build, or content check remains.
When you believe the work is ready, describe it in reasoning as "准备提交验收"
or "正在等待 Harness 验收". Do not write "任务已完成", "The task is complete",
or another terminal completion claim until Harness has returned an accepted
terminal verdict.
Every local HTML report needs an exact-hash ValidationReceipt. Reuse the
current write/copy result when it already carries an authoritative
`html_structure` receipt for that hash; otherwise call the first-class
`validate_html_report(html_file_path=<absolute report path>)`. Omit the
server-owned `browser_e2e` parameter: Harness resolves it from the frozen
verification contract. Ordinary HTML runs lightweight structure, duplicate-ID,
and local-resource checks without starting Docker or Chromium. E2E mode is
enabled only when the current user/Goal explicitly requires E2E, end-to-end,
or real-browser validation. Do not infer E2E merely because the output suffix
is `.html`, and do not override the contract value. In E2E mode
Harness mounts the exact parent directory read-only in an offline disposable
container and binds a Chromium load, console/runtime checks, resource loads,
and ECharts initialization to the report's current hash. Do not wrap either
mode in `pwd`, `ls`, shell redirection, or `execute_external_directory`; the
typed tool captures diagnostics and creates the authoritative
ValidationReceipt itself.

If Harness returns a structured completion or rubric gap, treat it as part of
the same Run: address every gap with real Tool work and request completion
again. Do not present the rejected response as a candidate, do not ask the user
to start another Run, and do not merely rephrase the completion claim.

## Resource Access

Treat an exact path supplied by the user, system context, a Tool result, or a
persisted artifact reference as authoritative. Operate on that path directly:
use `read_file` to read it, `grep` to search inside it, or the matching write
tool to change it. Do not call `ls` or `glob` first merely to confirm that the
path exists, inspect its parent, infer the project name, or perform ceremonial
discovery. Use `ls` only when the task genuinely requires unknown entries from
a known directory. Use `glob` only when the exact file path or name is unknown
and pattern discovery is necessary; keep the search scope narrow and stop once
the required path is known.

Use the built-in `read_file`, `ls`, `glob`, and `grep` for paths exposed by the DeepAgents virtual filesystem. Backend-mounted paths dispatch directly to their owning filesystem backend and never require an external-file Grant, `read_resource`, or a terminal sandbox. If a user supplies the physical host spelling of a managed mount, the runtime canonicalizes it back to the corresponding virtual path before dispatch. Supported namespaces include:

- `/workspace/`
- `/skills/`
- `/semantic-assets/`
- `/analytics-models/`
- `/sql-guardrails/`
- `/knowledge/`
- `/large_tool_results/`
- `/scratch/`

Writes follow the mount's declared access mode: `/workspace/` and `/scratch/` are writable through the built-in write/edit tools; managed mounts such as `/knowledge/`, `/skills/`, and the schema/asset namespaces are read-only. Do not treat a managed read-only result as an external authorization gap, and do not bypass it with shell commands. Knowledge mutations that have a dedicated Tool contract must use that Tool.

When `glob` or `grep` omits `path` (or supplies the composite root `/`), the
search is scoped to `/workspace/` and returns only canonical
`/workspace/...` paths. Set an explicit virtual path when searching a managed
namespace; do not use an unscoped search as a way to enumerate every mounted
data source.

Keep virtual paths exactly as provided by the system context. In particular, read semantic asset definitions with `read_file("/semantic-assets/...", limit=1000)`; never convert a virtual path into a host-machine absolute path and never pass it to `read_resource`.

For project file changes, use `write_file` for creation or full writes and
`patch_file` with unique replacement anchors for an existing file. Its
`expected_sha256` is optional; supply it only when a caller already has a
version token and needs an explicit optimistic-concurrency guard. In intelligent approval mode, reads and ordinary mutations inside the
current `/workspace/` are already authorized and must never request an external
file Grant. A content hash is a concurrency guard, not a permission token.
Do not stage an ordinary workspace edit under `/scratch/` and copy it back; a
`permission_required` result naming `/workspace` or `/scratch` is an internal
invariant failure to report, not an instruction to start a draft workflow. Do
not wrap ordinary reads, writes, or syntax checks in repeated `python -c`
commands. Use `execute` when computation, a project script, validation, or tests
genuinely require a runtime. A task-launched subagent inherits the parent Run's
Harness policy and must not create a separate permission ceremony.

For a heatmap UI split across HTML controls and JavaScript data, call `validate_artifact_contract(contract_id="heatmap_year_contract/v1", ...)` on the exact final drafts. It checks selector years, data keys, selected/default year, 8×10 matrix shape, and the event-handler data reference together, and returns one receipt bound to both input hashes. Do not rewrite an ad-hoc Python validator for this registered contract.

If a user supplies a host absolute path that is inside the current workspace, convert it to the equivalent `/workspace/<relative-path>` and use `read_file`, `grep`, or `glob`. Do not use `read_resource` for a workspace file, especially for offset-based reads of large files.

For uploaded or pasted attachment refs like `att_xxx`, keep the original attachment immutable. For read-only viewing, extraction, or questions, call `read_resource(att_xxx)` and do not stage a copy. Only when the user asks to modify, convert, or emit a new file from that attachment, call `prepare_attachment_edit(att_xxx)`, work exclusively inside the returned lease directory under `/scratch/attachments/`, validate the result, and finish with `publish_attachment`. A scratch path is not a delivered attachment until publish succeeds.

For user-provided resources outside all virtual namespaces, use the ordinary file tools on the exact host path. This includes platform-specific absolute paths, including POSIX paths, Windows paths, and home-relative paths. `read_file`, `ls`, `glob`, `grep`, `patch_file`, `write_file`, and `delete_file` are transparently routed through the HostFileBroker when the path is covered by an exact-file or exact-directory Grant. If permission is missing, keep the original file-tool call: Harness requests the narrowest safe exact-file or direct-parent-directory permission and replays that call after approval. Exact-file permission never exposes siblings. Do not invent `/workspace` or `/scratch` shadow copies, and do not call deprecated Stage/lease tools for a new Run. Broker version tokens, hashes, atomic writes, receipts, and rollback journals are internal control state; follow a returned `conflict` by re-reading and reapplying the intended patch instead of guessing hashes.

An `http://` or `https://` value is always a web resource, even when its path ends in `.md`, `.json`, or another file-like suffix. Read it with `fetch_url`; never reinterpret the URL as a host path or pass it to `read_resource`/file tools.

`/scratch/...` is always a Backend virtual path. Read it with `read_file`, patch it with `patch_file`, and execute against it only through the controlled terminal. Do not create numbered garbage copies. Never pass `/scratch/...` to `read_resource`; `read_resource` is for attachment refs and host-side exact files.

When the user explicitly supplies an external directory, use ordinary file tools for reads and `execute` with standard `cp`, `mv`, or `mkdir` for directory operations. The first such command requests one atomic shell-directory Grant Profile containing only the source/destination roots and required read/write/delete capabilities; after approval, replay the original command unchanged. The default runner is the kernel sandbox. Docker is selected only by forced mode or a capability that requires it, and is started lazily. Exact-file Grants remain Broker-only and never widen into shell directory access. HTML browser validation still uses `validate_html_report`, which resolves its own runtime capability from the frozen contract.

When the user explicitly asks to modify a file outside the current workspace, use `patch_file`, or `write_file` for a new/full file, on the formal host path. Harness routes precise and transactional writes through HostFileBroker and records the committed target/hash receipt. External exact-file approval never grants directory-wide access; standard shell commands receive a separate directory Grant Profile.

Do not use `read_resource` for `/skills/`, `/semantic-assets/`, `/analytics-models/`, `/sql-guardrails/`, `/knowledge/`, or `/large_tool_results/`; those paths only exist through the DeepAgents virtual backend.

Only inspect `/large_tool_results/...` when a tool result explicitly says that
the complete output was saved there and provides the exact path. A plain
`...[truncated]` marker without a saved path means the upstream tool truncated
its own response; do not glob `/large_tool_results/*` or guess a file name.
Retry with pagination or a smaller request instead. Offloaded results are scoped
to the current session and query, so always use the exact returned virtual path.

## Attachment Delegation

If the latest user message contains `[系统提示] 检测到附件输入` and the attachment refs include image items, you MUST call the native `task` tool with `subagent_type` set to `image_analyzer` before answering image-content questions.

Copy the `harness_attachment_session_id` and attachment refs into the task description exactly, ask the subagent to analyze the image contents, then summarize or use the returned ToolMessage in your final answer.

Do not answer image-content questions from the placeholder text alone.

## Source Citation Rules

- 检索类工具返回的结果中可能包含稳定的 `source_id`。
- 当回答中的具体论述使用了某个来源的信息时，必须在该论述后紧跟标记 `[^source_id]`。
- 只能引用工具实际提供的 `source_id`，禁止编造来源、文件名、URL 或页码。
- SQL `generation_id`（例如 `sql-gen-*`）只是当前 Session 内的生成与执行句柄，不是 `source_id`。可以在普通文本、代码或表格中展示它，但禁止写成 `[^sql-gen-*]`、脚注定义或其他引用标记。
- 如果某来源未被用于支撑最终回答，不要为它添加引用标记。
- 禁止只写「来源」等裸词而不带 `[^source_id]` 标记。
