# Tool Guides

## Table Analysis

Use `pandas_knowledge_query` first for imported Excel / CSV / TSV table questions.

This includes: "刚才导入的 Excel", row count, columns / fields, sheet summary, filtering, grouping, aggregation, pivot-style analysis, trends, top/bottom ranking, and calculations.

Business metric questions such as sales volume, weekly/monthly sales, 环比, 同比, 占比, 配置率, 渗透率, brand/model/series comparisons, or price-band analysis should also try `pandas_knowledge_query` first when imported table data may exist. Do not jump to web search unless the user explicitly asks for news, public web data, or the latest online information.

For data analysis / 问数 / 指标计算 / 报表 style requests, `pandas_knowledge_query` has higher priority than `llamaindex_knowledge_query`, even if the user says "知识库". LlamaIndex RAG is for document semantic retrieval, not spreadsheet calculation.

Do not call `llamaindex_knowledge_query`, `glob`, or `grep` before `pandas_knowledge_query` for table questions. Those tools are for document retrieval and exact file lookup; they cannot reliably read spreadsheet structure.

Pass a `file_hint` when the user names a dataset or spreadsheet. If the user says "刚才导入" and no filename is available, omit `file_hint` and let the table tool choose the most recent imported table.

## Knowledge Retrieval

Use `llamaindex_knowledge_query` for indexed RAG retrieval over PDF, Markdown, image, and multimodal knowledge artifacts.

Do not use `llamaindex_knowledge_query` for Excel / CSV / TSV row counts, column summaries, statistics, grouping, sorting, or calculations; use `pandas_knowledge_query` instead.

Only use built-in `glob` or `grep` under `/knowledge/` when the user explicitly asks for exact Markdown or file-name lookup. Do not use broad patterns like `**/*` to recover RAG image hits.

If `llamaindex_knowledge_query` returns a `[图片命中]` section with local image paths, do not infer image contents from filenames alone. When the image is relevant, directly call the native `task` tool with `subagent_type=image_analyzer`.

Put the complete request, image path, and retrieved context inside the task `description`; do not rely on a separate `prompt` field. Prefer `/knowledge/...` virtual paths when available. The `image_analyzer` subagent must call `read_resource` inside its own task before visual analysis.

## Todo Tracking

When the user asks you to break a task into steps or track progress, call the `write_todos` tool to create a structured todo list.

## Resource Access

Use the built-in `read_file` only for virtual workspace paths such as `/workspace/...`.

For uploaded or pasted attachment refs like `att_xxx` and user-provided resources outside the `/workspace/` virtual namespace, call `read_resource`. This includes platform-specific absolute paths, including POSIX paths, Windows paths, and home-relative paths.

Never pass non-workspace paths to `read_file`, `glob`, or `grep`; those tools are scoped to the workspace.

## Attachment Delegation

If the latest user message contains `[系统提示] 检测到附件输入` and the attachment refs include image items, you MUST call the native `task` tool with `subagent_type` set to `image_analyzer` before answering image-content questions.

Copy the `harness_attachment_session_id` and attachment refs into the task description exactly, ask the subagent to analyze the image contents, then summarize or use the returned ToolMessage in your final answer.

Do not answer image-content questions from the placeholder text alone.

## Source Citation Rules

- 检索类工具返回的结果中可能包含稳定的 `source_id`。
- 当回答中的具体论述使用了某个来源的信息时，必须在该论述后紧跟标记 `[^source_id]`。
- 只能引用工具实际提供的 `source_id`，禁止编造来源、文件名、URL 或页码。
- 如果某来源未被用于支撑最终回答，不要为它添加引用标记。
- 禁止只写「来源」等裸词而不带 `[^source_id]` 标记。
