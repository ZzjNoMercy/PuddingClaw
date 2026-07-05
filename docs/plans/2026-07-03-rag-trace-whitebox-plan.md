# RAG Trace 白盒监控实施计划

## 原则

- 不接 LlamaIndex 官方 callback，不依赖第三方内部事件。
- 不改现有 Trace 主线语义：tool output、state、model input 的逻辑保持不变。
- RAG 作为 `llamaindex_knowledge_query` tool 内部的额外细节监控，只挂在 tool span 下面。
- 主 Trace 先回答“RAG 返回给 Agent 什么、后续是否进入 state/messages/model input”；RAG 细节只作为可展开剖面。
- 主流程默认折叠 RAG 内部步骤；Tool 详情只显示 RAG step 摘要，完整细节保留在原始 trace 数据里。

## 监控边界

### Trace 主线

- Tool input：用户/模型传给 `llamaindex_knowledge_query` 的 query。
- Tool output：返回给 Agent 的 encoded tool result、sources、chunks、image hits、是否截断。
- 后续上下文：由现有 state / model input trace 继续观察，不重复实现。

### RAG 细节

- `rag.query`：query、top_k、candidate_top_k、hybrid/rerank 配置。
- `rag.embedding`：text/image query embedding 是否成功、维度，不记录原始向量。
- `rag.retrieve.text_vector`：文本向量候选数量和 top candidates。
- `rag.retrieve.image_vector`：图片向量候选数量和 top candidates。
- `rag.retrieve.bm25`：关键词候选数量和 top candidates。
- `rag.fusion.text_hybrid`：text vector + BM25 融合结果。
- `rag.fusion.multimodal`：text/image 候选融合结果。
- `rag.rerank`：rerank 输入候选数、输出排序、分数。
- `rag.select`：最终 selected hits、sources、chunks/image hits 数量。
- `rag.output`：最终 tool result 字符数、sources 数量、是否截断。

## 实施进度

- [x] 记录实施计划。
- [x] `TraceCollector` 增加 `rag` span API。
- [x] `llamaindex_knowledge_query` 多模态路径埋点。
- [x] fallback text index 路径埋点。
- [x] 前端 Trace 类型识别 `rag`。
- [x] 主流程折叠 `rag` 节点，Tool 详情显示 RAG 摘要。
- [x] 后端/前端验证。

## 当前不做

- 不做 RAG 独立面板。
- 不把 RAG 内部候选强行归因到 state/model input。
- 不记录 embedding 原始向量。
- 不修改 LlamaIndex 源码。
