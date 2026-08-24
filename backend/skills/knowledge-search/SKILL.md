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

For a registered Feishu Bitable source, call `feishu_bitable_list_sources` when
the exact source ID is unknown, then call `feishu_bitable_describe` to read
its live schema and then `feishu_bitable_query` for one bounded page. These
tools do not query arbitrary links or persist returned record values.
