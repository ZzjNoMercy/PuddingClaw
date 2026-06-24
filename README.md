# PuddingClaw

一个轻量级的 AI Agent 对话系统，支持工具调用、技能扩展、长期记忆和 RAG 检索。

## 核心功能

- **AI Agent 对话** - 基于 LangChain + DeepSeek 的智能对话系统
- **工具调用可视化** - 实时展示 Agent 的思考过程和工具执行轨迹
- **技能扩展系统** - 通过 Markdown 文件定义新技能，Agent 自动学习
- **长期记忆管理** - 支持传统文件注入和 RAG 向量检索两种模式
- **会话管理** - 多会话支持、历史压缩、自动标题生成
- **文件编辑器** - 内置 Monaco Editor，支持在线编辑配置文件
- **流式输出** - SSE 实时返回生成结果和中间事件

## 技术栈

| 层 | 技术 |
|---|------|
| 前端 | Next.js 14 + React 18 + TypeScript + Tailwind CSS |
| 后端 | FastAPI + Python 3.10+ |
| Agent | LangChain + DeepSeek |
| 记忆检索 | LlamaIndex + OpenAI Embedding |
| 实时通信 | SSE (Server-Sent Events) |

## 知识库与数据库演进

项目正在设计独立于长期记忆的知识库能力。目标架构采用：

- **PostgreSQL（核心依赖）**：保存用户、会话、知识库目录、文档版本、摄取任务、发布状态和引用元数据。
- **本地 Artifact 存储（核心能力）**：保存原始文档、完整 Markdown、图片和表格；支持 glob/ripgrep 精确检索。
- **Milvus（可选）**：保存 LlamaIndex 生成的文本/图片向量，用于语义和多模态检索。
- **MinerU（可选）**：作为独立解析服务处理扫描件、中文 OCR 和复杂图文 PDF；未启用时降级到 PyMuPDF/pypdf。

上传与解析只执行一次，解析结果可以发布到 `local`、`indexed` 或 `both` 两条管道。PostgreSQL 是业务事实源，Milvus 不替代关系型数据库，MinerU 也不会作为重量级依赖安装进后端进程。

> 当前状态：该部分处于方案设计阶段，根目录现有 Compose 和后端尚未完成 PostgreSQL/worker/可选依赖改造。请勿将下述目标结构当作已经可用的启动命令。

详细设计：

- [后端架构总览](docs/ARCHITECTURE.md)
- [ADR-001：AI Gateway + ModelClient 统一模型接入层](docs/adr/ADR-001-ai-gateway-and-model-client.md)
- [ADR-002：直连 / Higress 双模式与 Provider 配置同步](docs/adr/ADR-002-dual-mode-provider-sync.md)
- [知识库双管道技术方案与实施计划](docs/知识库双管道技术方案与实施计划.md)
- [开源项目结构与可选基础设施方案](docs/开源项目结构与可选基础设施方案.md)

### 面向开源的目标发行方式

Core 发行只强制启动 PostgreSQL、backend、worker 和 frontend。Milvus 使用独立 Compose overlay 按需叠加；MinerU 作为后端 optional dependency 管理，通过 uv 安装，并使用独立脚本 `scripts/setup-mineru.py` 处理部署：

```text
backend/pyproject.toml                 # 后端依赖，MinerU 为 optional
compose.yaml                           # Core
deploy/compose.milvus.yaml             # 可选 Milvus + etcd + MinIO
deploy/compose.ai-gateway.yaml         # 可选 Higress AI Gateway
scripts/setup-mineru.py                # 自动部署 MinerU（原生 / Docker / CPU / GPU）
```

目标启动矩阵：

```bash
# Core 模式（只依赖 PostgreSQL）
docker compose up -d

# 部署 MinerU（自动检测环境，下载模型，启动 mineru-api）
python scripts/setup-mineru.py

# 前台运行 mineru-api（开发调试，实时日志）
# python scripts/setup-mineru.py --foreground

# Full 模式（Higress + Milvus + 其余服务）
docker compose --profile full up -d
```

开源前还需要完成：根目录真实 `LICENSE`、`CONTRIBUTING.md`、`SECURITY.md`、第三方许可证说明、脱敏配置、CI 测试矩阵，以及移除当前 Compose 中个人机器的绝对 MinerU 模型路径。

## 项目结构

```text
PuddingClaw/
├── backend/                 # 后端 (FastAPI)
│   ├── app.py              # FastAPI 入口
│   ├── graph/              # Agent 核心逻辑
│   │   ├── agent.py        # Agent 管理器
│   │   ├── prompt_builder.py  # System Prompt 构建
│   │   ├── session_manager.py # 会话管理
│   │   └── memory_indexer.py  # RAG 记忆索引
│   ├── api/                # REST API 路由
│   │   ├── chat.py         # 聊天接口
│   │   ├── sessions.py     # 会话管理
│   │   ├── files.py        # 文件操作
│   │   ├── compress.py     # 历史压缩
│   │   └── config_api.py   # 配置管理
│   ├── tools/              # Agent 工具集
│   │   ├── terminal_tool.py
│   │   ├── python_repl_tool.py
│   │   ├── fetch_url_tool.py
│   │   ├── read_file_tool.py
│   │   ├── search_knowledge_tool.py
│   │   └── skills_scanner.py
│   ├── workspace/          # Agent 人格配置
│   │   ├── SOUL.md         # 核心设定
│   │   ├── IDENTITY.md     # 身份标识
│   │   ├── USER.md         # 用户画像
│   │   └── AGENTS.md       # 操作规范
│   ├── memory/             # 长期记忆存储
│   │   └── MEMORY.md
│   ├── skills/             # 可扩展技能库
│   │   └── */SKILL.md
│   ├── sessions/           # 会话持久化
│   ├── pyproject.toml      # 项目依赖（uv 管理）
│   ├── uv.lock             # uv 锁定文件
│   ├── requirements.txt    # 保留作为 fallback（将逐步迁移）
│   └── .env.example
└── frontend/               # 前端 (Next.js)
    ├── src/
    │   ├── app/            # 页面路由
    │   ├── components/     # UI 组件
    │   │   ├── chat/       # 聊天组件
    │   │   ├── layout/     # 布局组件
    │   │   └── editor/     # 编辑器组件
    │   └── lib/            # 状态管理 + API 客户端
    ├── package.json
    └── .env.example
```

上述是当前仓库结构。知识库实现会先在现有 `backend/` 内建立 `domain/application/infrastructure` 边界，再渐进迁移到 `apps/ + packages/ + services/ + deploy/`；不会为追求目录外观进行一次性大搬迁。

## 快速开始

### 环境要求

- Python 3.10+
- Node.js 18+

### 一键启动（推荐）

#### macOS / Linux

```bash
# 首次运行添加执行权限
chmod +x scripts/start-macos-linux.sh

# 启动服务
./scripts/start-macos-linux.sh
```

#### Windows

```bash
# 双击运行或在命令行执行
scripts\start-windows.bat
```

启动后访问：
- **前端界面**: http://127.0.0.1:3000
- **后端 API**: http://127.0.0.1:8002
- **API 文档**: http://127.0.0.1:8002/docs

> 提示：首次启动后，请先配置 `backend/.env` 文件中的 API Key，Agent 才能正常工作。

### 手动分步启动

如需开发调试，可手动启动前后端。

#### 后端

推荐使用 [uv](https://docs.astral.sh/uv/) 管理依赖：

```bash
cd backend
uv sync                         # 安装核心依赖
# 如需 MinerU 解析能力：
# uv sync --extra mineru
# 如需 Milvus 向量存储：
# uv sync --extra milvus

uv run python -m uvicorn app:app --reload --host 0.0.0.0 --port 8002
```

如未安装 uv：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

（保留 `requirements.txt` 作为传统 pip 用户的 fallback，但不保证与 `uv.lock` 完全同步。）

#### 前端

```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0 --port 3000
```

## 环境变量

### 后端 `backend/.env`

可参考 `backend/.env.example`：

```env
# DeepSeek API 配置
DEEPSEEK_API_KEY=your_deepseek_api_key_here
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat

# OpenAI API 配置（用于 Embedding）
OPENAI_API_KEY=your_openai_api_key_here
OPENAI_BASE_URL=https://ai.devtool.tech/proxy/v1
EMBEDDING_MODEL=text-embedding-3-small
```

### 前端 `frontend/.env`

可参考 `frontend/.env.example`：

```env
VITE_API_BASE_URL=http://127.0.0.1:8002
```

## 使用流程

1. **配置 API Key** - 编辑 `backend/.env` 填写 DeepSeek 和 OpenAI API Key
2. **启动服务** - 运行一键启动脚本
3. **开始对话** - 在前端界面输入问题，查看 Agent 的流式回复
4. **查看工具调用** - 展开思维链查看 Agent 的工具调用过程
5. **切换 RAG 模式** - 在右侧面板切换记忆检索模式
6. **编辑配置** - 使用内置编辑器修改 Agent 人格和技能定义

## 自定义端口

脚本支持通过环境变量覆盖默认端口：

```bash
BACKEND_PORT=9000 FRONTEND_PORT=4000 ./scripts/start-macos-linux.sh
```

Windows 可先设置环境变量后再运行：

```bat
set BACKEND_PORT=9000
set FRONTEND_PORT=4000
scripts\start-windows.bat
```

## API 端点

| 方法 | 路径 | 描述 |
|------|------|------|
| POST | `/api/chat` | 流式聊天接口（SSE） |
| GET | `/api/sessions` | 列出所有会话 |
| POST | `/api/sessions` | 创建新会话 |
| PUT | `/api/sessions/{id}` | 重命名会话 |
| DELETE | `/api/sessions/{id}` | 删除会话 |
| GET | `/api/sessions/{id}/history` | 获取会话历史 |
| POST | `/api/sessions/{id}/compress` | 压缩会话历史 |
| GET | `/api/files?path=...` | 读取文件 |
| POST | `/api/files` | 保存文件 |
| GET | `/api/config/rag-mode` | 获取 RAG 模式状态 |
| PUT | `/api/config/rag-mode` | 切换 RAG 模式 |
| GET | `/health` | 健康检查 |

## 技能扩展

在 `backend/skills/` 目录下创建新的技能文件夹，添加 `SKILL.md`：

```markdown
---
name: 技能名称
description: 技能描述
---

# 技能使用说明

详细的使用步骤和示例...
```

重启后端，Agent 会自动扫描并学习新技能。

## 常见问题

### 1. 启动脚本首次运行较慢

这是正常现象。脚本会自动创建 `backend/.venv` 并安装 Python / Node 依赖。

### 2. Agent 无法正常回复

请确认已经正确配置：
- `DEEPSEEK_API_KEY`
- `OPENAI_API_KEY`（用于 RAG 模式的 Embedding）

### 3. 前端无法连接后端

请检查：
- 后端是否已成功启动在 `0.0.0.0:8002`
- `frontend/.env` 中的 `VITE_API_BASE_URL` 是否正确
- 防火墙是否阻止了端口访问

### 4. RAG 检索不工作

请确认：
- 已安装 LlamaIndex 相关依赖
- 已配置 `OPENAI_API_KEY`
- `memory/MEMORY.md` 文件存在且有内容

## License

MIT
