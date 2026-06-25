# PuddingClaw 后端架构总览

> 状态：编写中，2026-06-23
> 适用范围：backend / ingestion-worker / AI Gateway / Milvus / MinerU
> 目标读者：所有后端开发者、DevOps、想了解调用链的 AI 工程师

---

## 1. 总体架构

PuddingClaw 后端采用**分层 + 可选基础设施**的设计：

- 核心层（core）永远可运行：FastAPI + PostgreSQL + 本地文件系统。
- 增强层（full）按需加载：AI Gateway、Milvus、MinerU，启动时自动探测，失败则降级。

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                              前端 (Next.js)                                   │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │ HTTP/SSE
┌──────────────────────────────────▼──────────────────────────────────────────┐
│                          backend-api (FastAPI)                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │   /api/chat  │  │ /api/config │  │ /knowledge  │  │   /api/sessions     │  │
│  └──────┬──────┘  └─────────────┘  └──────┬──────┘  └─────────────────────┘  │
│         │                                   │                                  │
│         └───────────────────┬───────────────┘                                  │
│                             ▼                                                  │
│            ┌─────────────────────────────────────┐                             │
│            │         Agent 编排层                 │                             │
│            │  ┌──────────────┐  ┌──────────────┐ │                             │
│            │  │ AgentManager │  │  DeepAgents  │ │                             │
│            │  │  (LangGraph) │  │   (预留)     │ │                             │
│            │  └──────┬───────┘  └──────┬───────┘ │                             │
│            └─────────┼─────────────────┼─────────┘                             │
│                      │                 │                                       │
│                      └────────┬────────┘                                       │
│                               ▼                                                │
│                  ┌─────────────────────┐                                       │
│                  │     ModelClient     │                                       │
│                  │  统一 LLM 调用入口   │                                       │
│                  └──────────┬──────────┘                                       │
└─────────────────────────────┼─────────────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
      ┌──────────────┐              ┌────────────┐
      │ AI Gateway   │              │   直连     │
      │  (Higress)   │              │ DeepSeek/  │
      │              │              │ OpenAI/    │
      │ 路由/计量/限流│              │ Qwen       │
      └──────────────┘              └────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
┌───────────────┐    ┌─────────────────┐    ┌───────────────┐
│  PostgreSQL   │    │   Milvus        │    │   MinerU      │
│  业务事实源    │    │  向量检索(可选)  │    │  PDF 解析(可选)│
│  + jsonl 日志  │    │                 │    │               │
└───────────────┘    └─────────────────┘    └───────────────┘
        │                     │                     │
        └─────────────────────┼─────────────────────┘
                              ▼
                    ┌─────────────────┐
                    │  Artifact 存储   │
                    │  backend/data/  │
                    └─────────────────┘
```

核心设计原则：

1. **一套代码，多档部署**：业务代码不感知基础设施是否在线，通过 `ModelClient` 和 `Capabilities` 自动选择实现。
2. **本地永远是真相源**：向量库、网关、解析服务都只保存可重建的加速数据，原始文件和 Markdown Artifact 必须落盘。
3. **显式传参，不污染全局**：LlamaIndex 的 `Settings` 不在业务代码中使用；模型、embedding、检索器都显式注入。
4. **失败降级，不叫停核心服务**：Milvus 挂了 → 本地 grep；MinerU 挂了 → PyMuPDF；Higress 挂了 → 直连模型。

---

## 2. 部署模式

### 2.1 Core 模式（最小可用）

只启动业务核心，适合开发、低配机器、快速验证：

```bash
docker compose up -d
```

包含：
- `backend-api`
- `ingestion-worker`
- `postgres`
- `frontend`

能力：
- 聊天 Agent（直连 DeepSeek/OpenAI）
- 本地 Markdown 长期记忆
- 本地文件知识库（glob/grep）
- PyMuPDF / pypdf 解析

### 2.2 Full 模式（推荐本地全量）

启动全部可选基础设施：

```bash
# 1. 部署 MinerU（自动检测 GPU/CPU/macOS，下载模型，启动 API）
python scripts/setup-mineru.py

# 2. 启动其余核心 + 可选服务
docker compose --profile full up -d
```

额外启动：
- `higress`：AI Gateway
- `milvus` + `etcd` + `minio`：向量库
- `mineru`：高质量 PDF 解析（由 `scripts/setup-mineru.py` 负责部署）

能力：
- 网关路由与 token 计量
- 向量语义检索 + 图文多模态
- MinerU 复杂版面解析

### 2.3 Compose 设计

`docker-compose.yml` 中所有可选服务加 `profiles: ["full"]`，**镜像必须钉版本，禁止用 `latest`**：

```yaml
services:
  higress:
    image: higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/all-in-one:v2.1.0
    profiles: ["full"]
    ...

  milvus:
    image: milvusdb/milvus:v2.5.4
    profiles: ["full"]
    ...

  etcd:
    image: quay.io/coreos/etcd:v3.5.16
    profiles: ["full"]
    ...

  minio:
    image: minio/minio:RELEASE.2026-01-01T00-00-00Z
    profiles: ["full"]
    ...

  # mineru 由 setup-mineru.py 管理，可原生可 Docker
  # 如需在 compose 内统一启动，可引用本地构建镜像
  mineru:
    build:
      context: ./mineru
      dockerfile: Dockerfile
    profiles: ["full"]
    ...
```

### 2.4 MinerU 部署策略

MinerU 与 Milvus 都作为 **optional dependency** 管理在 `backend/pyproject.toml` 中：

```toml
[project.optional-dependencies]
# base 包，跨平台，不包含 vllm/lmdeploy GPU 后端，避免与 backend 核心依赖冲突
mineru = ["mineru>=3.0"]

# Milvus 向量存储（llama-index-vector-stores-milvus 当前要求 pymilvus<3）
milvus = [
    "pymilvus==2.6.15",
    "llama-index-vector-stores-milvus==1.1.0",
]
```

不解析 PDF 的用户可以不装 MinerU，core 模式用 PyMuPDF/pypdf 即可；不使用语义检索的用户可以不装 Milvus。

MinerU 比较特殊：

- **模型文件 10GB+**，不会打包进镜像，首次运行时自动下载到 `~/.mineru/models`。
- **macOS 不建议 Docker**，官方推荐原生安装。
- **Linux 推荐 Docker**，GPU 版需要 NVIDIA Container Toolkit；Docker 内可独立使用 `mineru[all]`，不受 backend 依赖约束。
- **Windows 推荐 WSL2 + Docker**。

因此后端依赖统一用 **uv** 管理，由 `scripts/setup-mineru.py` 统一处理部署：

| 环境 | 部署方式 | 说明 |
|------|---------|------|
| macOS | `uv sync --extra mineru` + `uv run mineru-api` | 推荐原生 |
| Linux + NVIDIA GPU | Docker 官方镜像 + `--gpus all` | CUDA 加速 |
| Linux 无 GPU | Docker 官方镜像 | CPU / pipeline |
| Windows WSL2 | Docker 官方镜像 | 同 Linux |
| 任意（fallback） | `uv sync --extra mineru` | CPU 模式 |

脚本会自动：
1. 检测 OS、GPU、Docker、Python 版本。
2. 推荐合适的部署方式（用户可覆盖）。
3. 确保 `uv` 可用（未安装时尝试自动安装）。
4. 用 `uv sync --extra mineru` 安装依赖。
5. 设置/校验 `MINERU_MODEL_SOURCE`（默认 `modelscope`，国内镜像）。
6. **预下载 pipeline 模型到 `~/.mineru/models`**（约 10GB+；已存在则跳过）。
7. 启动 `mineru-api` 服务。
8. 将 `MINERU_URL` 写回 `backend/.env`。

常用选项：

```bash
python scripts/setup-mineru.py --dry-run              # 预览执行步骤
python scripts/setup-mineru.py --skip-model-download  # 跳过模型预下载
python scripts/setup-mineru.py --foreground           # 前台运行，实时显示日志（按 Ctrl+C 停止）
```

默认行为是后台启动并立即返回终端；开发调试时推荐 `--foreground`。

首次调用解析接口时，MinerU 也会按需下载模型；预下载只是避免首次请求等待。

业务容器通过环境变量知道可选服务地址，但**不强制依赖其健康状态**：

```yaml
backend-api:
  environment:
    - AI_GATEWAY_URL=http://localhost:8080/v1
    - MILVUS_URL=http://localhost:19530
    - MINERU_URL=http://localhost:8002
```

---

## 3. 能力探测（Capabilities Registry）

### 3.1 为什么需要

不同部署模式服务能力不同，业务代码不能假设所有服务都可用。启动时统一探测，结果缓存，后续逻辑据此选择实现。

### 3.2 数据结构

```python
# backend/capabilities.py
from dataclasses import dataclass

@dataclass
class CapabilityStatus:
    available: bool
    reason: str | None = None

@dataclass
class Capabilities:
    ai_gateway: CapabilityStatus
    milvus: CapabilityStatus
    mineru: CapabilityStatus
```

### 3.3 探测行为

| 能力 | 探测方式 | 超时 |
|------|---------|------|
| `ai_gateway` | `GET {AI_GATEWAY_URL}/health` | 2s |
| `milvus` | `connections.connect()` + list collections | 3s |
| `mineru` | `GET {MINERU_URL}/health` | 2s |

探测失败不阻塞启动，只记录 warning，后续调用自动 fallback。

### 3.4 对外暴露

```http
GET /api/health/capabilities
```

返回示例：

```json
{
  "ai_gateway": {"available": true, "reason": null},
  "milvus": {"available": false, "reason": "connection refused"},
  "mineru": {"available": true, "reason": null}
}
```

---

## 4. 模型接入层

### 4.1 ModelClient 抽象

所有 LLM 调用必须走 `backend/llm/model_client.py`，禁止业务代码直接实例化 `ChatDeepSeek`、`ChatOpenAI` 等。

```python
class ModelClient:
    def __init__(
        self,
        role: str = "default",      # agent / title / summary / vision
        temperature: float | None = None,
        streaming: bool = False,
    ):
        ...

    def get_chat_model(self) -> BaseChatModel:
        """返回 LangChain BaseChatModel，网关/直连自动选择。"""
        ...
```

### 4.2 网关优先，直连兜底

```python
async def get_chat_model(self):
    gateway_url = os.getenv("AI_GATEWAY_URL")

    if gateway_url and capabilities.ai_gateway.available:
        # Gateway 模式：PuddingClaw 不持有 Provider key，由 Higress 管理
        return ChatOpenAI(
            model=self.cfg["model"],
            api_key="dummy",  # Higress 负责上游鉴权
            base_url=gateway_url,
            temperature=self.temperature,
            streaming=self.streaming,
        )

    # fallback / direct 模式：使用本地 Fallback Provider
    fallback = self.cfg.get("fallback_provider", {})
    provider = fallback.get("provider", "deepseek")
    if provider == "deepseek":
        return ChatDeepSeek(
            model=fallback["model"],
            api_key=fallback["api_key"],
            base_url=fallback["base_url"],
            temperature=self.temperature,
            streaming=self.streaming,
        )
    if provider == "openai":
        return ChatOpenAI(...)
    # 注意：DeepAgents 不是 model provider，属于 Agent 编排层，不在此处处理
```

### 4.3 Token 用量统计

`ModelClient` 在调用层统一拦截 `usage_metadata`：

```python
async def ainvoke_with_usage(self, messages, *, user_id, session_id, role):
    start = time.time()
    resp = await self._llm.ainvoke(messages)
    usage = getattr(resp, "usage_metadata", {}) or {}
    record_token_usage(
        user_id=user_id,
        session_id=session_id,
        role=role,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
    )
    return resp
```

这样标题生成、中段摘要、记忆补偿等辅助调用都能被记录，不再只有主 Agent 流有数据。

### 4.4 Embedding 统一入口

Embedding 同样收口：

```python
# backend/llm/embed_client.py
from llama_index.embeddings.openai import OpenAIEmbedding

def get_embedding_model():
    cfg = get_embedding_config()
    gateway_url = os.getenv("AI_GATEWAY_URL")
    return OpenAIEmbedding(
        model=cfg["model"],
        api_key=cfg["api_key"],
        api_base=gateway_url or cfg["api_base"],
    )
```

**禁止**在业务代码中设置 `Settings.embed_model`。

---

## 5. Agent 层

### 5.1 当前实现

- `backend/graph/agent.py` 中的 `AgentManager` 基于 `LangGraph` + `langchain_deepseek.ChatDeepSeek`。
- 工具通过 `backend/tools/__init__.py` 注册，MCP 工具通过 `langchain-mcp-adapters` 动态加载。
- 中间件链：cache_boundary → tail_trim → tool_clear → summarization → compaction → skills_router → write。

### 5.2 向 DeepAgents 迁移预留

`DeepAgents` 是 **Agent 编排框架**，与 `LangGraph` 处于同一层级，不是模型 provider。它仍然需要通过 `ModelClient` 调用底层 LLM。

未来切换到 `langchain-deepagents` 时：

1. 保留 `ModelClient` 不变，继续提供 `BaseChatModel` 给 DeepAgents 使用。
2. 在 Agent 层新增 `DeepAgentsAgentManager`，与现有 `LangGraphAgentManager` 并列。
3. 工具注册、SSE 事件格式、会话持久化协议尽量保持一致。

```python
# 伪代码：DeepAgents 使用 ModelClient
from llm import ModelClient

llm = ModelClient(role="agent", streaming=True).get_chat_model()
agent = DeepAgent(
    llm=llm,
    tools=tools,
    ...
)
```

---

## 6. 知识库层

详见 `docs/知识库双管道技术方案与实施计划.md`。本总览只说明架构位置。

### 6.1 双管道

| 管道 | 实现 | 依赖 | 适用场景 |
|------|------|------|---------|
| Local | 完整 Markdown + glob/ripgrep | 仅本地文件 | 隐私、低成本、精确字符串检索 |
| Indexed | LlamaIndex + Milvus 多模态索引 | Milvus、embedding | 语义问答、图文检索 |

发布模式：`local` / `indexed` / `both`。默认 `both`（如果 Milvus 不可用则降级为 `local`）。

### 6.2 多模态 Embedding

文本和图片使用同一多模态 provider（推荐 DashScope/Qwen），或双模型方案。通过 `MultiModalEmbeddingProvider` 接口隔离，LlamaIndex 中显式传入 `embed_model` 和 `image_embed_model`。

### 6.3 检索路由

```python
async def retrieve(query, kb_id, mode="auto"):
    caps = await detect_capabilities()

    if mode in ("multimodal", "indexed") and caps.milvus.available:
        return await _retrieve_vector(query, kb_id, mode)

    logger.warning("Milvus unavailable, fallback to local retrieval")
    return await _retrieve_local(query, kb_id)
```

---

## 7. 解析层

### 7.1 解析器降级链

```text
PDF
 ├─ MinerU（rich，需要 mineru-api 服务）
 ├─ PyMuPDF（text_only + 原图）
 └─ pypdf（纯文本兜底）

DOCX → python-docx / unstructured
PPTX → python-pptx
XLSX → openpyxl
MD/TXT → 规范化适配器
图片 → OCR / VLM 适配器
```

### 7.2 MinerU 自带 API

MinerU 3.x 自带 `mineru-api` 服务，提供：

- `POST /file_parse`：同步解析（兼容旧接口）
- `POST /tasks`：异步任务提交、状态查询、结果获取

**不再维护自定义的 `mineru/app.py` FastAPI 包装层**。该文件已删除，新的解析调用直接请求 MinerU 自带的 `mineru-api`。

`mineru/` 目录仅保留一个可选的本地构建 `Dockerfile`，用于官方镜像不可用时的 fallback。详见 [`mineru/README.md`](../mineru/README.md)。

MinerU 通过 `backend/pyproject.toml` 的 optional dependency 安装：

```bash
cd backend
uv sync --extra mineru
uv run --extra mineru mineru-api --host 0.0.0.0 --port 8002
```

MinerU 部署由 `scripts/setup-mineru.py` 自动完成，详见 [2.4 MinerU 部署策略](#24-mineru-部署策略)。

### 7.3 运行时选择

```python
async def parse_pdf(file_path):
    caps = await detect_capabilities()
    if caps.mineru.available:
        return await _parse_with_mineru(file_path)
    logger.warning("MinerU unavailable, fallback to PyMuPDF")
    return await _parse_with_pymupdf(file_path)
```

---

## 8. 数据流

### 8.1 聊天请求

```text
用户消息
  → backend-api /api/chat
  → AgentManager.astream()
  → ModelClient.get_chat_model() → [Higress] → DeepSeek
  ← SSE token / tool_start / tool_end / sources
  → session_manager 保存会话
  → token_usage_store 记录用量
```

### 8.2 知识库上传

```text
文件上传
  → /knowledge-bases/{kb_id}/documents
  → SHA-256 去重，原件落盘
  → PostgreSQL 创建 ingestion_job
  → 202 返回 job_id
  
ingestion-worker 领取任务
  → 解析 → Artifact 规范化
  → local 发布（复制镜像）
  → 如果 Milvus 可用：切块 → embedding → upsert
  → PostgreSQL 更新 publication / index_run 状态
```

### 8.3 知识库检索

```text
用户查询
  → /knowledge/retrieve
  → 查询分类（text / visual / mixed）
  → 如果 Milvus 可用：text/image dense top-k
  → 本地 rg/BM25 lexical top-k
  → RRF/加权融合
  → rerank
  → 父段扩展
  → answer_context + sources[]
```

---

## 9. 配置约定

### 9.1 优先级

业务配置读取优先级（从高到低）：

1. 运行时环境变量
2. `backend/config.json` 持久化配置
3. `_DEFAULT_CONFIG` 默认值

### 9.2 关键环境变量

| 变量 | 含义 | 不设置时的行为 |
|------|------|--------------|
| `AI_GATEWAY_URL` | Higress 地址 | 直连模型 |
| `MILVUS_URL` | Milvus gRPC/HTTP 地址 | 知识库只走 local |
| `MINERU_URL` | MinerU 服务地址 | PDF 解析走 PyMuPDF/pypdf |
| `DEEPSEEK_API_KEY` | DeepSeek API Key | 从 config.json 读取 |
| `OPENAI_API_KEY` | OpenAI API Key | embedding / mem0 使用 |
| `DASHSCOPE_API_KEY` | 灵积 API Key | 多模态 embedding / 千问 |

### 9.3 网关模式下的模型名与鉴权

走 Higress 时，`config.json` 里的 `llm.model` 保持不变（如 `deepseek-chat`），由 Higress 根据 model name 路由到真实 provider。`ModelClient` 用 `ChatOpenAI` 只是因为它实现了 OpenAI 兼容协议。

Higress 在这里不提供模型；它提供统一 Base URL、请求代理、Token 统计、限流和按 model 切换 Provider。Gateway 模式下，PuddingClaw **不传递 Provider API Key**，上游鉴权由 Higress 中配置的 Provider key 负责。Fallback / direct 模式才使用 PuddingClaw 本地 Secret Store 中的 Provider key。

设置 API 永不返回 Provider Key 明文。

---

## 10. 改造路线图

> **总体原则：先横向基础设施，后纵向业务功能。**
>
> 模型接入层（ModelClient、Capability Registry、embed_client）是知识库 indexed/multimodal 管道的依赖，必须先落地。

### Phase 0：架构文档与骨架（当前）

- [x] 确定 core/full 部署模式
- [x] 确定 ModelClient + Capability Registry 抽象
- [x] 确定知识库双管道与多模态 embedding 方向
- [x] 编写 `docs/ARCHITECTURE.md` 与 `docs/adr/ADR-001-ai-gateway-and-model-client.md`

---

### 第一阶段：模型接入层基础设施（必须先做）

#### Phase 1.1：统一 LLM 调用

- [x] 新建 `backend/llm/model_client.py`
- [x] 替换 `backend/graph/agent.py` 中的 `ChatDeepSeek`
- [x] 替换 `backend/api/chat.py` 中的所有 `ChatDeepSeek`（标题生成、记忆补偿、中段摘要）
- [x] 在 `ModelClient` 层统一拦截并记录辅助调用 token 用量（增加 `role` 维度）；LangGraph 主流仍在事件流记录

#### Phase 1.2：统一 Embedding 调用

- [x] 新建 `backend/llm/embed_client.py`
- [x] 修复 `backend/graph/memory_indexer.py` 的 `Settings.embed_model` 全局污染
- [x] 所有 LlamaIndex 使用处显式传入 `embed_model`

#### Phase 1.3：能力探测

- [x] 新建 `backend/capabilities.py`
- [x] 启动时异步探测 Higress / Milvus / MinerU
- [x] 增加 `GET /api/capabilities`
- [x] 启动日志打印能力状态

**第一阶段验收**：业务代码不再直接实例化 `ChatDeepSeek`；网关/Milvus/MinerU 任一不可用时，系统自动 fallback；token 用量按 role 完整记录。

---

### 第二阶段：部署与基础设施（与第一阶段并行收尾）

#### Phase 2.1：MinerU 部署自动化

- [x] 创建 `scripts/setup-mineru.py`（检测 OS/GPU/Docker，自动下载模型，启动 mineru-api）
- [x] 简化 `mineru/Dockerfile` 为使用 MinerU 自带 API
- [x] 删除 `mineru/app.py` 和 `mineru/requirements.txt`
- [x] 创建 `mineru/README.md` 说明目录用途
- [ ] 在 CI/本地测试中验证 setup-mineru.py 各路径

#### Phase 2.2：Docker Compose 改造

- [ ] `docker-compose.yml` 给可选服务加 `profiles: ["full"]`
- [ ] 新增 `deploy/compose.ai-gateway.yaml` 作为 Higress overlay 备选
- [ ] 更新 `scripts/start-macos-linux.sh` 支持 `core|full` 参数

#### Phase 2.3：文档与启动体验

- [ ] README 更新 core/full 启动说明
- [ ] 启动脚本打印能力探测结果
- [ ] 前端 `/health/capabilities` 对接（可选）

**第二阶段验收**：`python scripts/setup-mineru.py` 能在 macOS/Linux/WSL2 上成功部署 MinerU；`docker compose --profile full up -d` 能启动其余全部服务；core 模式不依赖 Higress/Milvus/MinerU 也能运行。

---

### 第三阶段：知识库本地管道（可并行开始 schema 设计）

> 本地管道主要依赖 PostgreSQL 和文件系统，不依赖网关，但应使用第一阶段的 `capabilities.milvus` 探测结果做发布决策。

- [ ] 引入 PostgreSQL、SQLAlchemy async、asyncpg 与 Alembic
- [ ] 实现 knowledge base / document / revision / parse_run / artifact / publication 表
- [ ] 上传 API、SHA-256 去重、原件落盘
- [ ] MinerU → PyMuPDF → pypdf 解析器路由（接入 Capability 探测）
- [ ] Artifact 规范化：`full.md`、`manifest.json`、`pages/`、`images/`、`tables/`
- [ ] local 发布镜像与 `search_local_knowledge` / `read_knowledge_document`

**第三阶段验收**：上传 PDF 后能看到稳定 Markdown/图片；local 模式可精确搜索并引用原页；MinerU 不可用时自动降级到 PyMuPDF。

---

### 第四阶段：知识库向量管道（依赖第一阶段）

> indexed/multimodal 管道必须等 `embed_client` 和 `capabilities.milvus` 完成后才能稳定落地。

#### Phase 4.1：文本索引

- [ ] Markdown 结构切分、确定性 node ID、父子节点
- [ ] text collection upsert/delete/rebuild（走 `embed_client`）
- [ ] `search_knowledge_base` 切换到统一 retrieval service

#### Phase 4.2：图片索引与多模态检索

- [ ] 生成 `ImageNode`，补齐 caption、页码、bbox
- [ ] image collection 与 `MultiModalVectorStoreIndex`
- [ ] text/visual/mixed 路由、短视觉 query、融合排序
- [ ] 来源面板增加缩略图

#### Phase 4.3：检索降级

- [ ] 知识库 retrieval 根据 `capabilities.milvus.available` 自动降级到 local
- [ ] 用户选择 `indexed/both` 但 Milvus 不可用时给出明确提示或自动降级

**第四阶段验收**：增量上传、重试、删除无重复/幽灵节点；图文问题能召回正确图片；Milvus 不可用时自动回退到 local grep。

---

### 第五阶段：产品化与运维

- [ ] `/knowledge` 管理页、批量上传、状态展示
- [ ] 会话级知识库范围选择
- [ ] publication mode 切换、重解析、重索引
- [ ] 指标、Tracing、备份恢复
- [ ] 根据队列吞吐决定是否迁移任务系统到 Redis/Celery/Dramatiq

---

### 第六阶段：向 DeepAgents 迁移（远期）

> DeepAgents 是 Agent 编排框架，与 LangGraph 同级，不是 model provider。

- [ ] 评估 `langchain-deepagents` API 稳定性
- [ ] 新增 `DeepAgentsAgentManager`，与 `LangGraphAgentManager` 并列
- [ ] `DeepAgentsAgentManager` 复用现有 `ModelClient` 获取 LLM
- [ ] 保持工具注册、SSE 事件格式、会话持久化协议一致

---

## 相关文档

- `docs/adr/ADR-001-ai-gateway-and-model-client.md`：AI Gateway + ModelClient 决策记录
- `docs/adr/ADR-002-dual-mode-provider-sync.md`：直连 / Higress 双模式与 Provider 单次录入、单向同步规则
- `docs/知识库双管道技术方案与实施计划.md`：知识库详细设计
- `docs/开源项目结构与可选基础设施方案.md`：部署拆分说明
- `docs/context-engineering-design.md`：Agent 上下文压缩策略
