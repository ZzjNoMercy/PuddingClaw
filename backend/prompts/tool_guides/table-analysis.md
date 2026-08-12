## Table Analysis

Use `pandas_knowledge_query` first for imported Excel / CSV / TSV table questions.

This includes: "刚才导入的 Excel", row count, columns / fields, sheet summary, filtering, grouping, aggregation, pivot-style analysis, trends, top/bottom ranking, and calculations.

Do not use `pandas_knowledge_query` to answer catalog questions such as "当前知识库有哪些文件", "有哪些表格文件", "导入了哪些数据集", "列出文件清单", "目录清单", or "资产清单". These are filesystem/catalog questions; use filesystem listing tools such as `ls` / `glob` under `/knowledge` instead.

Business metric questions over explicitly imported Excel/CSV/TSV files, such as sales volume, weekly/monthly sales, 环比, 同比, 占比, brand/model/series comparisons, or spreadsheet price-band analysis, should use `pandas_knowledge_query`. Do not jump to web search unless the user explicitly asks for news, public web data, or the latest online information.

For imported-file data analysis / 问数 / 指标计算 / 报表 style requests, `pandas_knowledge_query` has higher priority than `llamaindex_knowledge_query`, even if the user says "知识库". LlamaIndex RAG is for document semantic retrieval, not spreadsheet calculation.

Do not call `llamaindex_knowledge_query`, `glob`, or `grep` before `pandas_knowledge_query` for table questions. Those tools are for document retrieval and exact file lookup; they cannot reliably read spreadsheet structure.

Pass a `file_hint` when the user names a dataset or spreadsheet. If the user says "刚才导入" and no filename is available, omit `file_hint` and let the table tool choose the most recent imported table.
