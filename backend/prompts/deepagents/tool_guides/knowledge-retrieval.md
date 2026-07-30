## Knowledge Retrieval

Use `llamaindex_knowledge_query` for indexed RAG retrieval over PDF, Markdown, image, and multimodal knowledge artifacts.

Do not use `llamaindex_knowledge_query` for Excel / CSV / TSV row counts, column summaries, statistics, grouping, sorting, or calculations; use `pandas_knowledge_query` instead.

Only use built-in `glob` or `grep` under `/knowledge/` when the user explicitly asks for exact Markdown or file-name lookup. Do not use broad patterns like `**/*` to recover RAG image hits.

If `llamaindex_knowledge_query` returns a `[图片命中]` section with local image paths, do not infer image contents from filenames alone. When the image is relevant, directly call the native `task` tool with `subagent_type=image_analyzer`.

Put the complete request, image path, and retrieved context inside the task `description`; do not rely on a separate `prompt` field. Prefer `/knowledge/...` virtual paths when available. The `image_analyzer` subagent must call `read_resource` inside its own task before visual analysis.
