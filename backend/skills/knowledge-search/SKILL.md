---
name: knowledge-search
description: Retrieve answers from local document knowledge bases.
toolsets:
  - knowledge_analysis
  - feishu_bitable
---

# Knowledge Search

For document questions, call `llamaindex_knowledge_query` after activating this
Skill. Reading this file only activates the toolset; it does not perform a
retrieval. Markdown LLM Wiki and GBrain are separate knowledge paths and do not
replace this document-index query. Do not use spreadsheet analysis for PDF or
Markdown content.

Treat one successful retrieval as the authoritative result set for the current
question. Do not follow it with exploratory `grep`, `glob`, or `ls`. The tool
reports text and image hit counts and returns exact `/knowledge/...` paths. If
the answer needs more source text, call `read_file` only on the exact linked
Markdown virtual path returned by the retrieval. If a returned image matters,
use its exact virtual path with the image-analysis workflow; never rediscover it
by scanning the knowledge tree.

For a registered Feishu Bitable source, call `feishu_bitable_list_sources` when
the exact source ID is unknown, then call `feishu_bitable_describe` to read
its live schema and then `feishu_bitable_query` for one bounded page. These
tools do not query arbitrary links or persist returned record values.
