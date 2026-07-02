# 2026-07-02 知识库功能开发执行计划

## 当前目标

把知识库能力从设计推进到可运行 MVP：

1. 后端引入统一知识库 catalog，优先面向 PostgreSQL，保留本地开发 SQLite fallback。
2. 支持用户在前端指定本地 Markdown 文件，由 backend 导入到 DeepAgents 已映射的 `/knowledge/` 后端目录。
3. `llamaindex_knowledge_query` 作为 DeepAgents 的统一 RAG 工具，封装本地 LlamaIndex 索引；旧 `search_knowledge_base` 已移除，避免工具选择混乱。
4. 打通上传 PDF → MinerU 解析 → Markdown artifact → 本地向量索引。
5. 配套实现 Markdown glob/grep 管道，服务完整 md 精确检索，与图文混排 PDF 的解析产物共用 `/knowledge/imported/...`。

## 用户补充约束

- “本地 md 的存储，用户可以在前端指定文件”
- “相应是不是要匹配 deepagents 的 backend？”

结论：要匹配。DeepAgents 当前把虚拟路径 `/knowledge/` 映射到统一知识库物理目录。默认是 `backend/knowledge/`，用户也可以通过 `PUDDINGCLAW_KNOWLEDGE_DIR` 指定到项目外部目录。前端指定的 Markdown 不应只留在浏览器状态里，而应通过 backend 导入/镜像到 `<knowledge_root>/imported/...`，再由 catalog 记录来源、物理路径和虚拟路径。

## 实施状态

- [x] 确认 DeepAgents `/knowledge/` 映射边界。
- [x] 新增数据库连接、模型和初始化逻辑。
- [x] 新增知识库本地 Markdown 导入 API。
- [x] 更新 LlamaIndex 知识库工具，导入后自动刷新索引，并将 Agent 主入口切到 `llamaindex_knowledge_query`。
- [x] 新增前端知识库页面，支持 Electron 文件选择和手动路径输入。
- [x] 增加 PostgreSQL compose 服务和环境变量。
- [x] PostgreSQL 接入 Docker infra compose，并加入后端 capabilities / 桌面 infra 状态检测。
- [x] `scripts/start-local-infra.sh` 纳入 PostgreSQL 启动、地址展示与 backend 环境变量提示。
- [x] 新增 PDF 上传 API，调用 MinerU 解析并保存原始 PDF 与 Markdown artifact。
- [x] PDF/MinerU 解析后的 Markdown 进入 LlamaIndex 索引刷新流程。
- [x] 按 notebook 方案补齐 LlamaIndex 多模态发布结构：统一多模态 embedding、text/image 双路 Milvus collection、`MultiModalVectorStoreIndex`。
- [x] MinerU zip 响应中的图片资产保存到 `/knowledge/assets/...`，与 Markdown 一起构造成 LlamaIndex `Document` / `ImageDocument`。
- [x] 多模态 embedding 独立配置为 `multimodal_embedding`；不复用普通 `/v1/embeddings` 路由。
- [x] 多模态索引发布参数进入 `config.json` 的 `knowledge.multimodal_index`；环境变量仅作为 Docker/CI/临时覆盖。
- [x] 补充 Higress 多模态 embedding native route 说明：`qwen2.5-vl-embedding` 不是 OpenAI-compatible，需要单独 native passthrough 或 direct DashScope SDK。
- [x] 新增 Markdown glob/grep API，支持按 `/knowledge/` 下的 glob 模式列文件和全文命中行。
- [x] 更新知识库前端页面，提供 PDF 上传、md glob/grep 与文档 catalog 列表。
- [x] 支持用户指定知识库物理目录：`PUDDINGCLAW_KNOWLEDGE_DIR` 统一驱动 DeepAgents `/knowledge/`、md 导入、PDF/MinerU artifacts、glob/grep 与 LlamaIndex 索引。
- [x] 跑后端/前端基础校验。

## 校验记录

- `python -m py_compile backend/db.py backend/knowledge/models.py backend/knowledge/service.py backend/api/knowledge.py backend/tests/test_knowledge_service.py backend/tools/search_knowledge_tool.py backend/app.py` 通过。
- `python -m py_compile backend/capabilities.py backend/api/capabilities.py` 通过。
- `pytest backend/tests/test_knowledge_service.py -q` 通过，2 passed。
- `docker compose -f docker-compose.infra.yml config` 通过，已包含 `postgres`。
- `docker compose -f docker-compose.yml config` 通过，backend 已依赖 `postgres: service_healthy`。
- `bash -n scripts/start-local-infra.sh scripts/start-macos-linux.sh` 通过。
- `uv run pytest tests/test_capabilities.py tests/test_knowledge_service.py -q` 通过，12 passed。
- `python -m py_compile backend/knowledge/mineru_client.py backend/knowledge/indexer.py backend/knowledge/service.py backend/api/knowledge.py backend/tools/search_knowledge_tool.py backend/graph/deepagents_manager.py` 通过。
- `uv run pytest tests/test_knowledge_service.py -q` 通过，覆盖本地 md 导入、PDF/MinerU fake ingest、md glob/grep、用户指定知识库目录、索引签名变化。
- `detect_capabilities_sync(force=True)` 可返回 `database / ai_gateway / milvus / mineru` 四类能力；当前 shell 环境缺 `asyncpg` 时会把 database 标为 unavailable reason，而不是抛穿。
- `git diff --check` 通过。
- `npm run --prefix frontend build -- --no-lint` 曾在新增 `/knowledge` 页面后通过；后续复跑时被既有 `frontend/src/components/agent/TraceViewer.tsx` 类型错误阻塞，例如 `isSystemMessage` 未定义。该文件不是本次知识库/PostgreSQL 改动范围。

## MVP API 草案

- `GET /api/knowledge/status`
- `GET /api/knowledge/documents`
- `POST /api/knowledge/documents/import-local-md`
- `POST /api/knowledge/documents/upload-pdf`
- `GET /api/knowledge/markdown/glob?pattern=**/*.md`
- `POST /api/knowledge/markdown/grep`

`POST /api/knowledge/documents/import-local-md` 请求：

```json
{
  "source_path": "/Users/pet/Notes/example.md",
  "title": "可选标题",
  "knowledge_base_id": "default"
}
```

返回核心字段：

```json
{
  "document": {
    "id": "...",
    "title": "...",
    "source_path": "...",
    "storage_path": "...",
    "virtual_path": "/knowledge/imported/20260702/..."
  }
}
```

`POST /api/knowledge/documents/upload-pdf` 使用 multipart form：

- `file`: PDF 文件
- `title`: 可选标题
- `publish_targets`: 默认 `local_markdown,vector`

处理结果：

1. 默认情况下，原始 PDF 保存到 `backend/data/knowledge/originals/YYYYMMDD/...`；若设置 `PUDDINGCLAW_KNOWLEDGE_DIR`，则保存到 `<knowledge_root>/originals/YYYYMMDD/...`。
2. MinerU 解析出的 Markdown 保存到 `<knowledge_root>/imported/YYYYMMDD/...`。
3. Catalog 记录 `source_type=pdf_mineru`、原始 PDF 路径、Markdown 虚拟路径、parser metadata。
4. 若发布目标包含 `vector`，默认使用 Qwen-VL multimodal embedding 构建 `MultiModalVectorStoreIndex`，并写入 `multimodal_manifest.json`。
5. 若 `knowledge.multimodal_index.vector_store=milvus`，按 notebook 方式写入双路 Milvus collection：
   - text: `PUDDINGCLAW_MILVUS_TEXT_COLLECTION`
   - image: `PUDDINGCLAW_MILVUS_IMAGE_COLLECTION`
6. 只有显式设置 `knowledge.multimodal_index.enabled=false` 时，才使用 legacy text-only local index 作为兼容 fallback。
7. 生成的 Markdown 可被 DeepAgents `/knowledge/` 的 `glob` / `grep` 精确检索，也可被 `llamaindex_knowledge_query` 统一 RAG 检索。

前端交互收敛：

- 知识库页只保留一个统一导入入口：上传 PDF / Markdown，后端按扩展名分流。
- 知识库配置进入 Settings -> 知识库；本地知识库目录写入 `config.json` 的 `knowledge.root_dir`。
- `PUDDINGCLAW_KNOWLEDGE_DIR` 仅作为环境覆盖，优先级高于 `config.json`，用于 Docker/CI/临时调试。

多模态配置：

```json
{
  "multimodal_embedding": {
    "provider": "dashscope",
    "model": "qwen2.5-vl-embedding",
    "dimension": 1024,
    "base_url": "",
    "route_path": "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding",
    "api_key": "",
    "prefer_gateway": true
  },
  "knowledge": {
    "multimodal_index": {
      "enabled": true,
      "vector_store": "milvus",
      "milvus_uri": "http://localhost:19530",
      "text_collection": "puddingclaw_knowledge_text",
      "image_collection": "puddingclaw_knowledge_image",
      "overwrite": false
    }
  }
}
```

`DASHSCOPE_API_KEY` 可以继续放环境变量，也可以填入 `multimodal_embedding.api_key`；如果本地 Higress AI Proxy 已配置 qwen / multi-model provider token，也可以不重复填写，后端会自动复用。后续 Secret Store 接入后应迁到密钥存储。

Higress 路由注意：

- `text-embedding-v4` 可以走现有 `/v1/embeddings` OpenAI-compatible 路由。
- `qwen2.5-vl-embedding` / `qwen3-vl-embedding` 不能直接挂到 `/v1/embeddings`。
- 若要经 Higress，需要单独配置 DashScope native route：
  `/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding`
- 具体模板见 `docs/higress-multimodal-embedding-route.md`。

## 下一阶段

- Parser job 队列化，避免大 PDF 同步阻塞请求。
- PyPDF/PyMuPDF fallback。
- Milvus + LlamaIndex 多模态索引发布目标。
- Excel → Pandas Engine 管道。
- PostgreSQL Alembic migrations，替换 MVP `create_all`。
