# Tool Guides

These guides describe protocols for capabilities that may be activated at runtime. The
`Current Capability Manifest` injected into each model call is authoritative: only tools
listed in `allowed_tool_names` are callable. If a guide names a business tool that is not
listed, first read the matching `/skills/<id>/SKILL.md`; never pretend the tool is already
available and never route around the capability boundary with a subagent or shell script.

## Database Analysis

When the Current Capability Manifest lists `database_sql_generate`, use it first for
configured PostgreSQL / NL2SQL business questions. If it is absent, read
`/skills/database-analysis/SKILL.md` before following the database protocol below.

This includes automotive product configuration analysis and metrics such as 配置率, 搭载率, 配备率, 装配率, 空气悬架, 空气悬挂, 激光雷达, 充电倍率, 能源类型, 车型级别, 上市时间, and price-band analysis over configured database tables.

For a direct database question, pass the user's original business question to `database_sql_generate`. For a multi-step Goal, compile each planned query into a focused **business sub-question**: the Agent owns the subject/metric, dimensions, grain, filters, time range, and required output, while the SQL generator owns physical tables, columns, EAV names/values, entity resolution, joins, CTEs, and all other SQL implementation. Never add a physical choice the user did not state, even as a confident assumption or “判断依据”. The UI-selected `model_id` and allowed semantic-asset id range are injected automatically from trusted runtime state; do not override them. Select only matching ids from the model-scoped semantic metadata index and pass them through `selected_semantic_asset_ids`. Then call `database_sql_validate` with `generation_id`; use the returned `validation_receipt_id` together with the same `generation_id` when calling `database_sql_execute`. Omit `sql`, because all three stages load the authoritative generation from the server-side ledger and Execute rejects a missing or hash-mismatched receipt. Never copy, reformat, or hand-edit generated SQL. If generation, validation, or execution fails, call `database_sql_generate` with the original `parent_generation_id` and describe only the observed error, timeout, empty result, conflict, or anomalous result in `revision_instruction`; do not prescribe a field, table, entity, JOIN/EXISTS/CTE shape, or replacement SQL. The generator classifies and repairs technical defects automatically without business HITL. Only when the business semantics or user-requested metric definition truly needs to change may the same revision path open HITL; the user then chooses 同意、拒绝、 or 修改, and only the generator may produce the resulting SQL. Never start a fresh generation with an Agent-invented physical workaround, and never launch multiple revision requests in parallel. After HITL resumes, treat the returned decision as final: on reject, do not ask for another choice—validate and execute the returned original generation; on agree or modify, validate and execute the new generation. Do not first search the knowledge base, inspect schema, enumerate fields, or call `pandas_knowledge_query`, unless the user explicitly asks for metadata or says the data is from an imported Excel/CSV/TSV file.

Use `database_schema_inspect` only when the user explicitly asks for metadata such as available tables, columns, or EAV `type_name` values.

## Semantic Dimension Builds

When the user explicitly asks to refresh, rebuild, or fully construct a reusable semantic dimension/Crosswalk, use `enqueue_semantic_dimension_build`. These builds may read large assets and must run in the background; do not use `terminal`, `execute_skill`, or `read_file` to run or inspect a full Crosswalk in the chat turn. Reply with the returned job id and state that the build is running.

When the user asks to build or append a cross-source dimension from one or more files/tables, first call `inspect_dimension_build_input` for each candidate. Attachments are valid temporary build inputs and do not need knowledge-base import. For a **new dimension** or explicit **baseline rebuild**, call `request_dimension_build_rule` with `operation="refresh"`; wait for the HITL choice of canonical input and key fields. For **adding an attachment/table to an already published dimension**, call it with `operation="append_source"`: the tool locks the existing `active_crosswalk.json` as the canonical universe, and the HITL card only maps the new source. For every source candidate, provide a stable `suggested_source_id` and `suggested_source_name`: use a reusable business source family such as `insurance_sales` / `乘用车上险量` or `orders` / `终端订单`, never an attachment id or month-specific filename. The card lets the user append a registered source or create a new source. Only after it resumes with a confirmed `build_rule`, enqueue `adapter="entity_crosswalk_v1"` with `input_snapshot={"build_rule": <confirmed rule>}`. An append must never replace the canonical baseline or create baseline-change review.

For the registered request "刷新全部车系维度" (or equivalent "刷新所有品牌车系", including `/build-semantic-dimension`), do not perform discovery. Call `enqueue_semantic_dimension_build` exactly once with `dimension_id="vehicle_series"`, `adapter="vehicle_series_full"`, and `requested_scope={"brands":"all"}`. Do not inspect the dimension directory, read `dimension.md`, list assets, inspect database schema, run Pandas/RAG, or query job progress before enqueueing. The builder owns source-profile loading and validation.

Use `get_semantic_dimension_build_job` only for progress, error review, or when the user explicitly asks to publish a completed build. A `waiting_for_publish_confirmation` result is staged and validated but not active. Only after explicit user approval, call `publish_semantic_dimension_build`; it atomically activates the staged Crosswalk, updates `dimension.md` in Beijing time, and records the published Job status. In the publication reply, use `published_summary` as the only active-runtime fact; `result_summary` describes the staged build and may contain raw file-instance diagnostics. Never compare an attachment-instance count with the UI's logical source-contract count. Never use `write_file` for this publication flow.

## Table Analysis

Use `pandas_knowledge_query` first for imported Excel / CSV / TSV table questions.

This includes: "刚才导入的 Excel", row count, columns / fields, sheet summary, filtering, grouping, aggregation, pivot-style analysis, trends, top/bottom ranking, and calculations.

Do not use `pandas_knowledge_query` to answer catalog questions such as "当前知识库有哪些文件", "有哪些表格文件", "导入了哪些数据集", "列出文件清单", "目录清单", or "资产清单". These are filesystem/catalog questions; use filesystem listing tools such as `ls` / `glob` under `/knowledge` instead.

Business metric questions over explicitly imported Excel/CSV/TSV files, such as sales volume, weekly/monthly sales, 环比, 同比, 占比, brand/model/series comparisons, or spreadsheet price-band analysis, should use `pandas_knowledge_query`. Do not jump to web search unless the user explicitly asks for news, public web data, or the latest online information.

For imported-file data analysis / 问数 / 指标计算 / 报表 style requests, `pandas_knowledge_query` has higher priority than `llamaindex_knowledge_query`, even if the user says "知识库". LlamaIndex RAG is for document semantic retrieval, not spreadsheet calculation.

Do not call `llamaindex_knowledge_query`, `glob`, or `grep` before `pandas_knowledge_query` for table questions. Those tools are for document retrieval and exact file lookup; they cannot reliably read spreadsheet structure.

Pass a `file_hint` when the user names a dataset or spreadsheet. If the user says "刚才导入" and no filename is available, omit `file_hint` and let the table tool choose the most recent imported table.

## Knowledge Retrieval

Use `llamaindex_knowledge_query` for indexed RAG retrieval over PDF, Markdown, image, and multimodal knowledge artifacts.

Do not use `llamaindex_knowledge_query` for Excel / CSV / TSV row counts, column summaries, statistics, grouping, sorting, or calculations; use `pandas_knowledge_query` instead.

Only use built-in `glob` or `grep` under `/knowledge/` when the user explicitly asks for exact Markdown or file-name lookup. Do not use broad patterns like `**/*` to recover RAG image hits.

If `llamaindex_knowledge_query` returns a `[图片命中]` section with local image paths, do not infer image contents from filenames alone. When the image is relevant, directly call the native `task` tool with `subagent_type=image_analyzer`.

Put the complete request, image path, and retrieved context inside the task `description`; do not rely on a separate `prompt` field. Prefer `/knowledge/...` virtual paths when available. The `image_analyzer` subagent must call `read_resource` inside its own task before visual analysis.

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

## Managed Lark personal autonomy

Standalone `lark-cli` commands are Adapter-owned. Non-delete Provider operations have default network access and run without per-command HITL, including message send, document create/update, Base update, upload, and sharing-permission changes. Do not ask for another confirmation merely because lark-cli labels a non-delete write as `high-risk-write`; the Backend validates exit 10 and appends `--yes` once when appropriate.

Deletion, content clearing, `config remove`, and `auth logout` remain explicit-confirmation actions. Never add `--yes` yourself, including `--yes=true` or `-y`; the Backend binds any approved retry to the frozen argv, Credential Profile revision, Toolchain revision, and lark confirmation action.

Payload fidelity for message/report content: keep the payload out of the command string whenever it spans multiple lines or mixes Markdown, quotes, backticks, or `$` fragments. Write the content to a workspace file first (for example `write_file` to `ai-news.md`), then send it with a file reference such as `lark-cli im +messages-send --user-id ou_xxx --content @ai-news.md`. `@file` resolves inside the mounted workspace and reaches lark-cli byte-for-byte. Inline payloads are fine for short single-line text: wrap them in double quotes (escape inner `"` as `\"`) or single quotes, and never use ANSI-C quoting `$'...'` — the managed parser rejects it to protect fidelity. Quoted newlines inside a normal quoted argument are accepted as inert text; shell composition outside quotes is not.

One invocation per managed command: the command string is exactly one `lark-cli` call, optionally preceded by environment-variable assignments such as `LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1`. Never chain commands with `;`, `&&`, `||`, or `|`, never insert `echo "---"` separators, and never append `2>&1` — stderr is captured automatically, and shell plumbing outside quotes gets the whole call rejected (a single trailing `2>&1` is harmlessly stripped, but one followed by more text is not). When you need two commands, issue two separate tool calls.

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

Use the built-in `read_file`, `glob`, and `grep` for paths exposed by the DeepAgents virtual filesystem. Supported namespaces include:

- `/workspace/`
- `/skills/`
- `/semantic-assets/`
- `/analytics-models/`
- `/sql-guardrails/`
- `/knowledge/`
- `/large_tool_results/`

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

`/scratch/...` is always a Backend virtual path. Read it with `read_file`, patch it with `patch_file`, and execute against it only through the controlled terminal. Do not create numbered garbage copies. Never pass `/scratch/...` to `read_resource`; `read_resource` is for attachment refs, managed knowledge, and host-side exact files.

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
