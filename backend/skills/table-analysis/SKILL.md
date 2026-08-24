---
name: table-analysis
description: Analyze registered spreadsheet table assets and logical datasets.
toolsets:
  - knowledge_analysis
  - feishu_bitable
  - logical_dataset
---

# Table Analysis

Use registered table assets or logical datasets for spreadsheet analysis. For a
new attachment that must become reusable, follow the logical-dataset Skill.
Registered Feishu Bitable sources remain live external data: inspect the schema
with `feishu_bitable_describe` and read only the fields/page needed with
`feishu_bitable_query`.
