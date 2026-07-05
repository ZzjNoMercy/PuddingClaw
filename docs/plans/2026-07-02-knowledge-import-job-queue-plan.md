# 2026-07-02 知识库导入任务队列实施计划

## 背景

同步导入大 PDF 时，前端请求会经过 Next dev proxy，再由 backend 同步读取文件、调用 MinerU、写 Markdown、刷新向量索引。150MB 级 PDF 很容易触发代理断开、后端超时或用户重复点击。

## 目标

- 上传动作只负责保存原文件并创建导入任务。
- 后台 worker 消费任务，执行 PDF/MinerU、Markdown、Excel/CSV 等分流处理。
- 前端显示任务列表、状态、进度、失败原因。
- 单独提供导入任务中心，后续承载 pipeline 预览：Markdown、原始文件、图片、结构化、切片和检索测试。
- PostgreSQL 是正式任务状态存储；SQLite fallback 仍可用于本地开发。

## 实施拆解

- [x] 新增 `KnowledgeImportJob` / `KnowledgeImportEvent` catalog 模型。
- [x] 新增任务创建、查询、重试 API。
- [x] 新增 backend 内置 worker，启动后自动轮询 queued job。
- [x] 前端统一导入入口改为创建任务，展示任务列表和状态。
- [x] 新增独立任务列表页 `/knowledge/imports`。
- [x] 新增任务详情页 `/knowledge/imports/[jobId]`，接入任务信息、事件流、Markdown 预览和切片预览；解析完成后即展示 LlamaIndex `MarkdownNodeParser` 生成的真实 chunk，未生成 chunk 前才保留 Markdown 标题临时预览。
- [x] 保留旧同步导入接口兼容，但前端不再使用。
- [ ] 后续增强：取消任务、并发数配置、独立 worker 进程、细粒度 MinerU 进度、PDF 内嵌预览、图片缩略图 API、结构化 blocks API、LlamaIndex 检索测试、失败任务自动重试策略。

## 状态流转

```text
queued -> running -> succeeded
                 -> failed
queued/running -> cancelled   # 后续增强
failed -> queued               # retry
```

## 文件落盘

上传先进入用户知识库目录：

```text
<knowledge_root>/.tasks/<job_id>/source/<原始文件名>
```

worker 成功后继续沿用当前规则：

```text
<knowledge_root>/originals/YYYYMMDD/...
<knowledge_root>/imported/YYYYMMDD/...
<knowledge_root>/assets/YYYYMMDD/...
```

## 前端页面分工

- `/knowledge`：用户入口，只负责选择文件、填写可选标题、投递导入任务；下方只展示最近几条任务。
- `/knowledge/imports`：导入任务中心，查看所有任务、状态、进度、失败原因和重试。
- `/knowledge/imports/[jobId]`：单任务详情页，承载 pipeline 预览。当前已展示：
  - 概览：文件信息、解析统计、处理方式、任务事件。
  - 解析结果：Markdown 预览、原始文件路径、图片资源占位、结构化占位。
  - 切片预览：优先读取 `vector_index.multimodal.chunks` 中的真实入库 chunk；若尚未导入向量，则读取文档 metadata 的 `llamaindex_chunks`，这是解析完成后自动生成的真实 LlamaIndex chunk；仅在任务未生成 chunk 时基于 Markdown 标题做临时预览。
  - 检索测试：预留给 LlamaIndex 多模态查询接口。
- Milvus 向量发布不是独立队列阶段；按当前架构由任务详情页按钮触发，调用 LlamaIndex 多模态发布逻辑写入 text/image 双 collection。发布链路采用 notebook 方式：`MarkdownNodeParser` text nodes + `ImageDocument` image nodes + Qwen-VL embedding + `MultiModalVectorStoreIndex` 双 collection。
