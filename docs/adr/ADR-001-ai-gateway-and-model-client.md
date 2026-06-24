# ADR-001：AI Gateway + ModelClient 统一模型接入层

| 字段 | 内容 |
|------|------|
| 编号 | ADR-001 |
| 标题 | 引入 AI Gateway 并建立 ModelClient 统一模型接入抽象 |
| 状态 | **Accepted** |
| 日期 | 2026-06-23 |
| 作者 | PuddingClaw Team |
| 相关模块 | `backend/llm/*`, `backend/capabilities.py`, `backend/graph/agent.py`, `backend/api/chat.py`, `docker-compose.yml` |

---

## 1. 背景与问题

### 1.1 当前现状

截至本决策前，后端模型调用存在以下问题：

1. **模型调用散落**：`langchain_deepseek.ChatDeepSeek` 在 `backend/graph/agent.py` 和 `backend/api/chat.py` 中多次直接实例化（标题生成、记忆补偿、中段摘要）。
2. **统计不完整**：`backend/graph/token_usage_store.py` 只记录了主 Agent 流中的 token 用量，辅助 LLM 调用成了黑盒。
3. **无统一路由**：切换模型、多 API key 轮询、限流降级都需要在业务代码中处理。
4. **全局配置污染**：`backend/graph/memory_indexer.py` 直接修改 LlamaIndex 全局 `Settings.embed_model`，与知识库多模型 embedding 规划冲突。
5. **DeepAgents 迁移成本高**：如果未来从 `ChatDeepSeek` 切换到 `langchain-deepagents`，需要改 N 处业务代码。

### 1.2 业务驱动

- 需要按用户/会话/任务维度精确统计 token 用量。
- 需要支持多模型（DeepSeek / OpenAI / Qwen / 本地模型）并按场景路由。
- 知识库双管道方案要求 embedding 模型可替换、不污染全局配置。
- 本地全量部署需要 Higress + Milvus + MinerU，但 core 模式要能在它们不可用时继续运行。

---

## 2. 决策

我们决定：

1. **引入 AI Gateway（默认 Higress）作为可选基础设施层**，负责模型路由、token 计量、限流、审计。
2. **建立 `backend/llm/model_client.py` 统一 LLM 调用抽象**，业务代码只依赖 `ModelClient`，不直接实例化任何具体模型。
3. **建立 `backend/capabilities.py` 能力探测机制**，启动时检测 Higress / Milvus / MinerU 是否可用，失败时自动 fallback。
4. **统一 Embedding 入口 `backend/llm/embed_client.py`**，禁止业务代码设置 LlamaIndex 全局 `Settings.embed_model`。
5. **在 `ModelClient` 调用层统一拦截并记录 token 用量**，覆盖主 Agent 和所有辅助调用。

### 2.1 决策后的调用链

```text
业务代码
   │
   ▼
ModelClient.get_chat_model()
   │
   ├─ AI Gateway 可用 ──→ Higress ──→ DeepSeek / Qwen / OpenAI
   │
   └─ AI Gateway 不可用 ──→ 直连 DeepSeek / Qwen / OpenAI
   │
   ▼
token_usage_store 记录用量（按 role 区分）
```

---

## 3. 为什么不强制 AI Gateway

我们明确**不把 AI Gateway 作为强制依赖**，原因如下：

1. **开发体验**：本地 core 模式应该能秒起，不需要等 Higress/Milvus/MinerU 全部就绪。
2. **测试友好**：单元测试和 CI 不应该依赖网关。
3. **运维成本**：小规模部署没必要维护网关。
4. **渐进升级**：已有用户可以继续直连模型，等新架构稳定后再切网关。

但**默认推荐 full 模式**，通过 `docker compose --profile full up -d` 启动全部服务。探测到网关不可用则静默 fallback 到直连。

---

## 4. 具体实现

### 4.1 ModelClient 接口

```python
# backend/llm/model_client.py
from langchain_core.language_models.chat_models import BaseChatModel

class ModelClient:
    def __init__(
        self,
        role: str = "default",
        temperature: float | None = None,
        streaming: bool = False,
    ) -> None:
        self.role = role
        self.cfg = get_llm_config()
        self.temperature = temperature if temperature is not None else self.cfg.get("temperature", 0.7)
        self.streaming = streaming

    def get_chat_model(self) -> BaseChatModel:
        gateway_url = os.getenv("AI_GATEWAY_URL")
        if gateway_url and capabilities.ai_gateway.available:
            return ChatOpenAI(
                model=self.cfg["model"],
                api_key=self.cfg["api_key"],
                base_url=gateway_url,
                temperature=self.temperature,
                streaming=self.streaming,
            )

        provider = self.cfg.get("provider", "deepseek")
        if provider == "deepseek":
            from langchain_deepseek import ChatDeepSeek
            return ChatDeepSeek(
                model=self.cfg["model"],
                api_key=self.cfg["api_key"],
                base_url=self.cfg["base_url"],
                temperature=self.temperature,
                streaming=self.streaming,
                stream_usage=True,
            )

        raise ValueError(f"Unknown provider: {provider}")
```

### 4.2 Capability 探测

```python
# backend/capabilities.py
@dataclass
class CapabilityStatus:
    available: bool
    reason: str | None = None

@dataclass
class Capabilities:
    ai_gateway: CapabilityStatus
    milvus: CapabilityStatus
    mineru: CapabilityStatus

_capabilities: Capabilities | None = None

async def detect_capabilities() -> Capabilities:
    global _capabilities
    if _capabilities is not None:
        return _capabilities
    _capabilities = Capabilities(
        ai_gateway=await _check_http(os.getenv("AI_GATEWAY_URL"), "/health"),
        milvus=await _check_milvus(),
        mineru=await _check_http(os.getenv("MINERU_URL"), "/health"),
    )
    return _capabilities
```

### 4.3 Token 用量统一记录

扩展 `token_usage_store.record_token_usage` 增加 `role` 字段：

```python
def record_token_usage(
    user_id: str,
    session_id: str,
    round_num: int,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    start_time: float,
    role: str = "agent",  # agent / title / summary / compensation / embedding
) -> None:
    ...
```

`ModelClient` 包装 `ainvoke` / `astream`：

```python
async def ainvoke_with_usage(self, messages, *, user_id, session_id, round_num):
    start = time.time()
    resp = await self._llm.ainvoke(messages)
    usage = getattr(resp, "usage_metadata", {}) or {}
    record_token_usage(
        user_id=user_id,
        session_id=session_id,
        round_num=round_num,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        total_tokens=usage.get("total_tokens", 0),
        start_time=start,
        role=self.role,
    )
    return resp
```

### 4.4 Embedding 收口

```python
# backend/llm/embed_client.py
from llama_index.embeddings.openai import OpenAIEmbedding

def get_embedding_model() -> OpenAIEmbedding:
    cfg = get_embedding_config()
    gateway_url = os.getenv("AI_GATEWAY_URL")
    return OpenAIEmbedding(
        model=cfg["model"],
        api_key=cfg["api_key"],
        api_base=gateway_url or cfg["api_base"],
    )
```

---

## 5. 影响

### 5.1 对现有代码的影响

| 文件 | 改动 |
|------|------|
| `backend/graph/agent.py` | 用 `ModelClient(role="agent", streaming=True)` 替换 `ChatDeepSeek` |
| `backend/api/chat.py` | 标题生成、记忆补偿、中段摘要统一走 `ModelClient` |
| `backend/graph/memory_indexer.py` | 移除 `Settings.embed_model`，显式传 `embed_model` |
| `backend/graph/token_usage_store.py` | 增加 `role` 字段 |
| `backend/capabilities.py` | 新增 |
| `backend/llm/model_client.py` | 新增 |
| `backend/llm/embed_client.py` | 新增 |
| `docker-compose.yml` | 可选服务加 `profiles: ["full"]`；镜像钉版本 |
| `scripts/start-macos-linux.sh` | 支持 `core|full` 参数 |
| `backend/pyproject.toml` | 新增；MinerU 作为 optional dependency；uv 管理 |
| `scripts/setup-mineru.py` | 新增 MinerU 自动部署脚本 |
| `mineru/Dockerfile` | 改用 MinerU 自带 `mineru-api` |
| `mineru/app.py` | 已删除；改用 MinerU 自带 `mineru-api` |
| `backend/requirements.txt` | 逐步迁移到 `uv.lock`，可保留作为过渡 |

### 5.2 对部署的影响

- 新增 `higress` service（profile full）。
- backend 环境变量增加 `AI_GATEWAY_URL`。
- 后端依赖管理迁移到 `uv` + `backend/pyproject.toml`。
- MinerU 作为 optional dependency，由 `scripts/setup-mineru.py` 统一处理，支持原生/Docker、CPU/GPU、macOS/WSL2 自动检测。
- 原有直连模式继续可用，无需强制迁移。

### 5.3 对测试的影响

- 单元测试可以直接 mock `ModelClient`。
- 不需要在 CI 中启动 Higress。
- 能力探测模块需要单独测试 fallback 路径。

---

## 6. 备选方案与未选择原因

### 方案 A：每个业务点继续直连模型

- **优点**：简单，无额外抽象。
- **缺点**：切换模型、统计用量、限流降级都要散落修改；DeepAgents 迁移成本高。
- **未选择**：不符合长期治理目标。

### 方案 B：使用 LiteLLM Proxy

- **优点**：开箱即用的多模型路由，社区活跃。
- **缺点**：治理能力（多租户、审计、自定义插件）弱于 Higress；与阿里云生态（千问、DashScope）集成不如 Higress 顺畅。
- **未选择**：Higress 更适合国内部署和企业级治理，且 WASM 插件可定制。

### 方案 C：自己用 FastAPI 写一个模型代理

- **优点**：完全可控。
- **缺点**：重复造轮子，需要自行实现限流、熔断、多 key 轮询、可观测性。
- **未选择**：投入产出比低。

### 方案 D：强制所有调用必须过 Higress

- **优点**：统一入口，治理最彻底。
- **缺点**：core 模式无法运行；测试和开发成本增加。
- **未选择**：与"一套代码、多档部署"的原则冲突。

---

## 7. 向 DeepAgents 迁移的路径

> **重要澄清**：`DeepAgents` 是 Agent 编排框架，与 `LangGraph` 处于同一层级，不是 model provider。它仍然需要通过 `ModelClient` 获取 LLM。

`ModelClient` 的设计已经为 DeepAgents 预留了 LLM 接入点：

```python
# DeepAgents 使用 ModelClient 获取 LLM（伪代码）
from llm import ModelClient

llm = ModelClient(role="agent", streaming=True).get_chat_model()
agent = DeepAgent(llm=llm, tools=tools, ...)
```

迁移时只需：

1. 保留 `ModelClient` 不变，确认其返回的模型兼容 DeepAgents 的 LLM 接口。
2. 新增 `DeepAgentsAgentManager`，与现有 `LangGraphAgentManager` 并列。
3. 在 `config.json` 中增加 `agent.framework: "deepagents"`（而不是改 `llm.provider`）。
4. 验证工具调用协议（tool_calls / ToolMessage）是否一致。
5. 保持 SSE 事件格式、会话持久化、来源引用协议不变。

---

## 8. 相关决策

- 本 ADR 与 `docs/知识库双管道技术方案与实施计划.md` 中的 embedding 策略一致：统一 provider 接口，显式传参。
- 本 ADR 与 `docs/开源项目结构与可选基础设施方案.md` 的部署拆分一致：可选基础设施不强制耦合 core。
- Provider 凭证只录入一次、双模式切换及向 Higress 的同步规则由 `ADR-002-dual-mode-provider-sync.md` 进一步约束。

---

## 9. 状态变更记录

| 日期 | 版本 | 变更 | 负责人 |
|------|------|------|--------|
| 2026-06-23 | 1.0 | 初稿，Accepted | PuddingClaw Team |

---

## 10. 参考

- Higress 官方文档：https://higress.io/
- LangChain Chat Models：https://python.langchain.com/docs/concepts/chat_models/
- LangGraph Agents：https://langchain-ai.github.io/langgraph/
- `docs/ARCHITECTURE.md`
- `docs/知识库双管道技术方案与实施计划.md`
