---
name: knowledge-search
description: Retrieve answers from local document knowledge bases.
toolsets:
  - knowledge_analysis
---

# Knowledge Search

For document questions, call `llamaindex_knowledge_query` after activating this
Skill. Reading this file only activates the toolset; it does not perform a
retrieval. Markdown LLM Wiki and GBrain are separate knowledge paths and do not
replace this document-index query. Do not use spreadsheet analysis for PDF or
Markdown content.
