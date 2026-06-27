# PuddingClaw 项目级 AGENTS.md 记忆方案

> 结论：本地个人客户端按 **project 维度**设置 `AGENTS.md`，`user_id` 先不上；记忆通过独立的 `MemoryMiddleware` 注入，不依赖 `StoreBackend`，也不占用主 CompositeBackend 的 `/memories/` 路由。

## 1. 背景

PuddingClaw 是本地个人助理客户端，不是多人 SaaS。DeepAgents 的 `MemoryMiddleware` 正好能让我们为每个项目绑定一份长期记忆（`AGENTS.md`），让同项目下的所有 Agent session 共享同一套约定，不同项目之间互相隔离。

## 2. 三个核心概念

| 概念 | 本质 | 在方案中扮演的角色 |
|---|---|---|
| `MemoryMiddleware` | DeepAgents 中间件 | 在 Agent 启动时读取 `AGENTS.md`，包装成 `<agent_memory>` 注入 system_prompt |
| `FilesystemBackend` | 本地磁盘虚拟文件系统后端 | 把 Agent 视角的路径映射到本地真实目录 |
| `StoreBackend` | 基于 LangGraph Store 的后端 | 跨 thread 持久化，依赖 `namespace` 做隔离 |

关键等价关系：

```python
# 下面两种写法等价
create_deep_agent(memory=["/memories/AGENTS.md"], backend=backend)

create_deep_agent(
    backend=backend,
    middleware=[MemoryMiddleware(backend=backend, sources=["/memories/AGENTS.md"])],
)
```

也就是说，`memory=[...]` 只是 DeepAgents 自动装配 `MemoryMiddleware` 的语法糖。本方案选择**显式构造 `MemoryMiddleware`**，不依赖 `memory=` 参数。

## 3. 四种方案对比

| 方案 | 实现方式 | 优点 | 缺点 | 适用场景 |
|---|---|---|---|---|
| A. AGENTS.md 放项目根 | `memory=["/AGENTS.md"]`，default backend 指向 `project_path` | 最透明，用户可在自己项目里直接看到和编辑 | 可能污染用户项目；Agent 写文件时要用权限保护 | 想让用户感知并手动维护 AGENTS.md |
| B. `/memories/` 路由到项目记忆目录 | CompositeBackend 增加 `/memories/` → `backend/data/deepagents-memory/projects/<project_id>/`，配合 `memory=["/memories/AGENTS.md"]` | 不污染用户项目；路径语义清晰 | 多一个路由；`MemoryMiddleware` 解析 CompositeBackend 可能有 silent failure 风险 | 不介意主 backend 多一个路由 |
| C. 独立 `FilesystemBackend` + `MemoryMiddleware`（本方案） | 显式构造 `MemoryMiddleware`，给 AGENTS.md 单独一个 backend，通过 `middleware=[...]` 传入 | 最稳，彻底避开主 backend 路由冲突；多个记忆源可并行注入 | AGENTS.md 不在 Agent 文件工具视图里 | **本地客户端 project 隔离 + gstack 索引（推荐）** |
| D. `StoreBackend` + namespace | `StoreBackend(store=store, namespace=("project", project_id))` | 官方支持跨 thread 持久化；namespace 隔离规范 | 重；需要 LangGraph Store/Runtime 上下文；本地单用户收益小 | SaaS / 多用户 / 团队版 |

## 4. 官方（LangChain / DeepAgents）倾向

结合 DeepAgents 课件与源码设计：

1. `MemoryMiddleware` 解决的是**跨会话长程记忆**，不是上下文窗口不够（那是 `SummarizationMiddleware` 的职责）。
2. `AGENTS.md` 是项目级记忆卡，遵循 [agents.md](https://agents.md/) 规范。
3. Backend 选择矩阵中，**本地开发优先 `FilesystemBackend`**，跨线程持久化才用 `StoreBackend`。
4. 第七章的 gstack 案例使用「独立 `FilesystemBackend` + `MemoryMiddleware`」，正是因为 gstack 的 `AGENTS.md` 是**技能索引清单**（只读说明书），需要注入主 Agent 但不想和主 CompositeBackend 的路由冲突。

## 5. PuddingClaw 推荐方案：方案 C（显式 MemoryMiddleware）

本地客户端的记忆隔离优先级：

```
project scoped > global assistant scoped > user scoped
```

因此：

- **不上 `user_id` / `user.identity` namespace**：本地默认一个用户，加了复杂度没收益。
- **不用 `StoreBackend`**：太重，本地单用户没必要。
- **用显式 `MemoryMiddleware` + `FilesystemBackend`**：稳、清晰、多个记忆源可共存。

### 5.1 路径设计

```
已选项目时：
  /                → project_path（工作区）
  /workspace/      → project_path（工作区，gstack skill 约定路径）
  /skills/         → backend/skills/
  project memory   → backend/data/deepagents-memory/projects/<project_id>/AGENTS.md
  gstack index     → backend/gstack/AGENTS.md（如果存在）

未选项目时：
  /                → backend/data/agent-workspaces/unscoped/<session_id>/
  /workspace/      → backend/data/agent-workspaces/unscoped/<session_id>/
  /skills/         → backend/skills/
  global memory    → backend/data/deepagents-memory/global/AGENTS.md
  gstack index     → backend/gstack/AGENTS.md（如果存在）
```

这样：

- 每个 project 有独立的 `AGENTS.md`。
- 不同 project 完全隔离。
- 无项目会话回退到全局记忆。
- `AGENTS.md` 真实落盘，便于用户查看、调试和版本控制。
- 不依赖 `StoreBackend` 和复杂的 `namespace`。
- gstack 技能索引作为独立记忆源并行注入。

### 5.2 AGENTS.md 初始模板

```markdown
# Project Memory

<!--
This file is injected into the Agent's system prompt via DeepAgents MemoryMiddleware.
Put stable, long-lived project conventions here (tech stack, coding style, naming rules).
Do NOT put session-specific or frequently changing data here — it hurts prompt caching.
-->

## 技术栈
- 主语言：
- Web 框架：
- 数据库：

## 代码风格
- 

## 命名约定
- 
```

## 6. 代码实现

改动文件：`backend/graph/deepagents_manager.py`

主要变更：

1. 新增 `_memory_dir_for(project_id)`：决定记忆目录。
2. 新增 `_ensure_agents_md(memory_dir)`：自动创建目录和初始 `AGENTS.md`。
3. `_build_backend(workspace_path)`：主 CompositeBackend 只保留 `/skills/` 路由，**不再负责 `/memories/`**。
4. 新增 `_build_memory_middleware(project_id)`：构造一个 `MemoryMiddleware`，同时加载项目/global 记忆和 gstack 索引。
5. `create_deep_agent(...)`：通过 `middleware=[...]` 传入记忆中间件，**不再使用 `memory=`**。

> 注意：DeepAgents 不允许传入多个同类型的中间件实例（会报 `Please remove duplicate middleware instances`），所以项目记忆和 gstack 索引必须合并到**同一个 `MemoryMiddleware`** 里，通过 `sources` 指定多份文件。

关键代码片段：

```python
from deepagents.middleware.memory import MemoryMiddleware


def _memory_dir_for(self, project_id: str | None) -> Path:
    assert self._base_dir is not None
    memory_root = self._base_dir / "data" / "deepagents-memory"
    if project_id:
        return memory_root / "projects" / project_id
    return memory_root / "global"


def _ensure_agents_md(self, memory_dir: Path) -> Path:
    memory_dir.mkdir(parents=True, exist_ok=True)
    agents_md = memory_dir / "AGENTS.md"
    if not agents_md.exists():
        agents_md.write_text(
            "# Project Memory\n\n"
            "<!--\n"
            "This file is injected into the Agent's system prompt...\n"
            "-->\n",
            encoding="utf-8",
        )
    return agents_md


def _build_backend(self, workspace_path: Path):
    assert self._base_dir is not None
    skills_dir = self._base_dir / "skills"
    routes: dict[str, FilesystemBackend] = {}
    if skills_dir.exists():
        routes["/skills/"] = FilesystemBackend(root_dir=skills_dir, virtual_mode=True)
    if routes:
        return CompositeBackend(
            default=FilesystemBackend(root_dir=workspace_path, virtual_mode=True),
            routes=routes,
        )
    return FilesystemBackend(root_dir=workspace_path, virtual_mode=True)


def _build_memory_middleware(self, project_id: str | None) -> list[Any]:
    assert self._base_dir is not None

    # 1) Project-scoped or global AGENTS.md
    memory_dir = self._memory_dir_for(project_id)
    self._ensure_agents_md(memory_dir)

    # 2) Optional gstack skill index
    gstack_path = (self._base_dir / "gstack" / "AGENTS.md").resolve()
    gstack_sources: list[str] = []
    gstack_route: FilesystemBackend | None = None
    if gstack_path.exists():
        gstack_route = FilesystemBackend(
            root_dir=str(gstack_path.parent),
            virtual_mode=True,
        )
        gstack_sources.append("/gstack/AGENTS.md")

    # Use a single MemoryMiddleware with a composite backend to avoid
    # DeepAgents' "duplicate middleware instances" assertion.
    sources = ["/AGENTS.md", *gstack_sources]
    if gstack_route is not None:
        memory_backend: FilesystemBackend | CompositeBackend = CompositeBackend(
            default=FilesystemBackend(root_dir=memory_dir, virtual_mode=True),
            routes={"/gstack/": gstack_route},
        )
    else:
        memory_backend = FilesystemBackend(root_dir=memory_dir, virtual_mode=True)

    return [MemoryMiddleware(backend=memory_backend, sources=sources)]
```

在 `astream` 中组装 Agent：

```python
agent = create_deep_agent(
    model=model,
    tools=self._build_tools(workspace_path),
    skills=["/skills/"],
    middleware=self._build_memory_middleware(project_id),
    backend=self._build_backend(workspace_path),
    system_prompt=(
        "You are PuddingClaw Agent mode. The filesystem tools are scoped to the current workspace. "
        "Project-level memory has been injected via MemoryMiddleware. "
        "Do not claim access to files outside this workspace unless an external-file permission flow grants it."
    ),
)
```

## 7. 关于 gstack 的引入

gstack 的 `SKILL.md` 方法论通过 `skills=["/skills/"]` 注入，gstack 的 `AGENTS.md` 技能索引通过同一个 `MemoryMiddleware` 的第二个 source 注入：

- `skills` 是「怎么做」——菜谱，按需翻阅。
- `memory` 是「是什么」——家规，始终牢记。

同一个 `MemoryMiddleware` 会按 `sources` 顺序读取两份 AGENTS.md 并注入 system_prompt。只要两份 AGENTS.md 内容不同，就不会重复。

## 8. 注意事项

1. **不要把易变内容写进 AGENTS.md**：`MemoryMiddleware` 把整份文件拼进 system_prompt，频繁变化的内容会降低 prompt cache 命中率。
2. **AGENTS.md 内容要稳定、长期、可复用**：例如技术栈、代码风格、命名约定。
3. **如需用户手动编辑 AGENTS.md**：路径是 `backend/data/deepagents-memory/projects/<project_id>/AGENTS.md`。
4. **本方案中 Agent 无法通过文件工具访问 `/memories/AGENTS.md`**：记忆是通过 `MemoryMiddleware` 在启动时注入的。如果后续需要 Agent 自我更新记忆，需要额外设计（例如暴露一个 memory write API 或把记忆文件也挂到主 backend 路由）。
5. **后续做多用户/团队版时**：再引入 `StoreBackend` + `namespace=("project", project_id, "user", user_id)` 也不迟。

## 9. 参考

- 课件 notebook：`notebooks/deepAgents实战_Modelclient版本.ipynb`
  - 第 3 章：MemoryMiddleware 机制
  - 第 4 章：Backend 虚拟文件系统
  - 第 6 章：Memory 作用域
  - 第 7 章：gstack 集成案例
- 相关源码：`backend/graph/deepagents_manager.py`
