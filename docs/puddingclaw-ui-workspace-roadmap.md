# PuddingClaw UI 工作台分阶段实施计划

> 目标：把 PuddingClaw 从当前“聊天 + Skills 管理”界面，逐步升级成更接近 Codex 桌面体验的项目工作台。实施原则是先改 UI，不破坏当前 agent/session/tool 协议；再引入本地项目目录能力；最后等 coding agent 能力成熟后，再适配右侧环境/变更面板。

## 总体原则

1. **先视觉后能力**：一阶段只基于当前项目改 UI，不动后端接口。
2. **先兼容后替换**：保留现有 chat/session/skills 数据流，新增工作台壳层和组件样式。
3. **插件化命名**：现有 Skills 与 MCP 不再作为一级“技能/工具”概念散落在导航里，统一收入“插件/扩展”体系。
4. **本地操作有边界**：二阶段再引入项目目录和本地电脑操作能力，必须明确容器部署时的文件系统权限。
5. **右侧环境面板延后**：PuddingClaw 当前还不是完整 coding agent，右侧环境信息、git 变更、提交/推送等可以等 coding agent 能力成熟后再做。

## 阶段一：当前项目内 UI 工作台化

### 目标

在不改后端接口、不改 agent 协议的前提下，重构 PuddingClaw 前端视觉和信息架构：

- 左侧：从普通导航升级为项目/对话/扩展入口的工作台侧栏。
- 中间：保留当前 chat 能力，改成更紧凑的工作台对话区。
- 底部：输入框改成浮动 composer，支持当前已有模式和状态。
- Tool cards：保持当前数据结构，优化折叠、错误态、摘要态展示。
- Skills/MCP：统一收入“插件/扩展”入口，命名上从“技能”扩展为“扩展能力”。

### 不做的事

- 不新增后端 API。
- 不实现真实本地文件夹操作。
- 不实现 git diff / branch / open location 等右侧环境能力。
- 不改 session/tool output/context maintenance 的协议。

### 前端改动范围

当前主要涉及：

```text
frontend/src/app/page.tsx
frontend/src/app/settings/page.tsx
frontend/src/app/skills/page.tsx
frontend/src/app/globals.css
frontend/src/components/layout/Sidebar.tsx
frontend/src/components/layout/Navbar.tsx
frontend/src/components/layout/SkillsBar.tsx
frontend/src/components/layout/ResizeHandle.tsx
frontend/src/components/chat/ChatPanel.tsx
frontend/src/components/chat/ChatMessage.tsx
frontend/src/components/chat/ChatInput.tsx
frontend/src/components/chat/ThoughtChain.tsx
frontend/src/components/chat/RetrievalCard.tsx
frontend/src/components/chat/SlashCommandMenu.tsx
frontend/src/components/settings/MemoryEditor.tsx
frontend/src/components/editor/InspectorPanel.tsx
frontend/src/components/skills/FileTree.tsx
frontend/src/lib/store.tsx
frontend/src/lib/navigation.ts
frontend/src/lib/settingsApi.ts
frontend/src/lib/skillsApi.ts
```

建议新增（可选/延后）：

```text
frontend/src/components/workspace/WorkspaceShell.tsx
frontend/src/components/workspace/WorkspaceSidebar.tsx
frontend/src/components/workspace/WorkspaceHeader.tsx
frontend/src/components/workspace/ExtensionEntry.tsx
frontend/src/components/workspace/StatusStrip.tsx
```

### 信息架构建议

一阶段左侧导航可以改成：

```text
新对话
搜索
扩展
自动化

项目
  PuddingClaw
    当前会话列表

扩展能力
  Skills
  MCP Servers
  插件商店/安装状态

设置
```

说明：

- “Skills” 和 “MCP” 都放进“扩展能力”里。
- UI 文案建议从“插件”扩展为“扩展”，其中 Skills/MCP/未来插件都是扩展类型。
- 当前已有 `/skills` 页面可保留，但入口命名调整为“扩展”或“扩展能力”。

### 组件视觉

一阶段建议先建立工作台视觉 token：

```text
背景：深色工作台背景
侧栏：低对比深灰，选中态轻量高亮
消息区：内容居中，最大宽度限制
输入框：底部浮动，圆角但不夸张
工具卡：扁平折叠条，状态图标 + 工具名 + 成功/失败
context maintenance：短暂状态行，不落库
```

避免一阶段做过度装饰。PuddingClaw 是工作工具，视觉目标应是“安静、清晰、可长期盯着用”。

### 验收标准

- 所有现有会话能正常加载、发送、流式回复。
- tool_start/tool_end、error tool card、context_maintenance 都能正常展示。
- Skills 页面仍可访问，只是入口改名为“扩展”。
- 不新增后端接口。
- `npm run build` 通过。

### 一阶段开发计划与状态

更新时间：2026-06-21

| 状态 | 任务 | 文件范围 | 说明 |
| --- | --- | --- | --- |
| [x] | 持久化一阶段执行清单 | `docs/puddingclaw-ui-workspace-roadmap.md` | 将本阶段拆成可跟踪任务，并在完成后回写状态。 |
| [x] | 顶部导航改为工作台语义 | `frontend/src/components/layout/Navbar.tsx`, `frontend/src/lib/navigation.ts` | 将 Skills 入口统一命名为“扩展”，保留原 `/skills` 路由。 |
| [x] | 左侧侧栏工作台化 | `frontend/src/components/layout/Sidebar.tsx` | 增加项目、快捷入口、扩展能力分组并保留会话列表。Raw Messages 从主界面隐藏，底层调试 API 与状态能力保留。 |
| [x] | 隐藏 Raw Messages 调试界面 | `frontend/src/components/layout/Sidebar.tsx` | 移除侧栏折叠区、token 请求和全屏弹层，保留会话、RAG 状态及后端消息接口。 |
| [x] | RAG 开关迁移至设置页 | `frontend/src/app/settings/page.tsx` | 在 RAG 设置分区提供即时生效的标准开关，并复用全局 RAG 状态。 |
| [x] | 工作台统一为浅色界面 | `frontend/src/app/globals.css`, `frontend/src/components/layout/Navbar.tsx`, `frontend/src/components/layout/Sidebar.tsx` | 保留工作台结构，顶部、侧栏、内容外壳和菜单统一使用白色与浅灰层级。 |
| [x] | 区分项目与普通对话 | `frontend/src/components/layout/Sidebar.tsx` | 当前 session 均无项目归属，统一放入“对话”；“项目”独立展示空状态，等待二阶段 workspace 绑定。 |
| [x] | 修正宽屏对话主列对齐 | `frontend/src/components/chat/ChatPanel.tsx`, `ChatMessage.tsx`, `ChatInput.tsx` | 消息、状态与输入框统一扩展至 `max-w-5xl`，减少宽屏右侧拥挤感。 |
| [x] | 扩展页增加类型标签切换 | `frontend/src/app/skills/page.tsx` | 顶部提供“MCP / 技能”分段标签；技能保留现有编辑器，MCP 在无配置数据时展示真实空状态。 |
| [x] | 顶部导航收纳至侧栏 | `frontend/src/components/layout/Navbar.tsx`, `Sidebar.tsx` | 顶部仅保留品牌与面板控制；扩展、GitHub 等入口归入侧栏，设置固定为侧栏最后一项。 |
| [x] | 删除重复扩展能力区并校正全局对话轴 | `Sidebar.tsx`, `page.tsx`, chat components | 删除侧栏重复入口；宽屏按左右面板宽度差补偿，使消息与输入框相对整个窗口居中。 |
| [x] | 收紧对话历史密度 | `frontend/src/components/layout/Sidebar.tsx` | 缩小历史行高、字号、图标及分组间距，提高侧栏会话容量。 |
| [x] | 收窄对话内容列 | chat components | 消息、工具卡、状态与输入框统一由 `max-w-5xl` 收至 `max-w-4xl`，保持全局居中。 |
| [x] | 扩展页保持工作台侧栏 | `frontend/src/app/skills/page.tsx` | 进入扩展时保留项目、对话和底部设置侧栏，MCP/技能内容作为右侧二级工作区切换。 |
| [x] | 技能目录居中展示 | `frontend/src/app/skills/page.tsx` | 默认以居中目录展示标题、搜索和已安装技能；选中技能后再进入列表加编辑器工作区。 |
| [x] | 对话列进一步收窄并保持居中 | chat components | 内容宽度由 `max-w-4xl` 收至 `max-w-3xl`，继续使用全局中心补偿。 |
| [x] | 页脚品牌文案全屏居中 | `ChatInput.tsx`, `globals.css` | Powered by 文案始终按左右面板宽度差补偿，相对整个窗口横向居中。 |
| [x] | 对话内容按剩余区域动态居中 | chat components, `page.tsx` | 消息、工具卡和输入框取消全屏补偿，始终在左右面板之间的可用聊天区域内居中。 |
| [x] | MCP 标签接入真实服务器数据 | `frontend/src/app/skills/page.tsx` | 复用全局 MCP Server 状态和现有 API，展示智慧芽等已启用服务器，不再使用静态空状态。 |
| [x] | 修复本地前后端 CORS | `backend/app.py`, `docker-compose.yml` | 用明确的本地前端来源替代通配来源，并支持通过 `CORS_ORIGINS` 扩展部署域名。 |
| [x] | 前端 API 改为同源代理 | `frontend/next.config.mjs`, API clients, Docker 配置 | 浏览器统一请求 `/api`，由 Next 容器转发至 backend，彻底移除跨端口 CORS 依赖。 |
| [x] | 中央聊天区视觉收紧 | `frontend/src/components/chat/ChatPanel.tsx`, `frontend/src/components/chat/ChatMessage.tsx` | 内容宽度、欢迎态、消息气泡向工作台风格调整。 |
| [x] | Composer 工作台化 | `frontend/src/components/chat/ChatInput.tsx` | 输入框改为底部浮动 composer，保留 `/` Skill 调用、停止生成、context usage。 |
| [x] | Tool card 状态优化 | `frontend/src/components/chat/ThoughtChain.tsx` | 增加错误态和摘要态展示，保留现有 toolCalls 数据结构。 |
| [x] | 建立工作台视觉 token | `frontend/src/app/globals.css`, `frontend/src/app/page.tsx` | 统一背景、panel、composer、滚动阴影和布局边界。 |
| [x] | 新对话懒创建 Session | `frontend/src/components/layout/Sidebar.tsx`, `frontend/src/lib/store.tsx` | 点击「新对话」不立即创建 Session，仅切换到占位 `default` 会话；用户发送首消息时再由 `sendMessage` 调用 `createSession` 懒创建，避免产生空会话。 |
| [x] | 会话按最近活动时间排序 | `frontend/src/components/layout/Sidebar.tsx` | 侧边栏历史会话按 `updated_at` 降序排列，最新活跃会话置顶。 |
| [x] | 刷新后自动切换至最近会话 | `frontend/src/lib/store.tsx` | 切换会话时持久化到 `sessionStorage`，刷新后优先恢复上次选中的会话；未命中时自动切换到最近活跃的会话；若处于占位 `default` 会话则保持空对话状态。 |
| [x] | Context Usage 下移至 Composer | `frontend/src/components/chat/ChatInput.tsx` | 将上下文用量从侧栏移除，紧凑展示在 composer 下方，按百分比阈值显示绿/黄/红状态。 |
| [x] | 挂载及切换会话时拉取 token count | `frontend/src/components/chat/ChatInput.tsx` | 进入页面或切换 session 时调用 token 接口，实时更新 context usage。 |
| [x] | 空对话快捷提示词 | `frontend/src/components/chat/ChatPanel.tsx` | 无消息时展示居中欢迎态与若干快捷提示按钮，点击即可发送。 |
| [x] | Slash 命令选择菜单 | `frontend/src/components/chat/SlashCommandMenu.tsx`, `ChatInput.tsx` | 输入 `/` 时弹出可键盘导航的技能选择菜单，选中后自动替换为 `/skill-name `。 |
| [x] | 设置页完整分区实现 | `frontend/src/app/settings/page.tsx`, `frontend/src/lib/settingsApi.ts` | 设置页扩展为 LLM、Embedding、RAG、Memory、Data、Advanced 六大分区，支持测试连接与保存。 |
| [x] | AI 接入管理页设计与实现 | `designs/ai-access-settings/`, `frontend/src/app/settings/page.tsx`, `frontend/src/lib/settingsApi.ts` | 在设置中统一展示实际请求链路，并管理 Higress Gateway、LLM Provider、Embedding Provider 与各自密钥。 |
| [x] | Gateway 与 Provider 配置 API | `backend/config.py`, `backend/api/config_api.py` | Gateway 只管理启用状态、代理 URL、健康检查与回退策略；LLM/Embedding Provider Key 独立保存并始终用于真实模型访问。 |
| [x] | ModelClient 与 Embedding 接入闭环 | `backend/llm/`, `backend/capabilities.py`, LlamaIndex 调用点 | 修复调用身份计量、健康误判、请求期阻塞与首 token 前 fallback；移除全局 `Settings.embed_model` 污染。 |
| [x] | AI 接入端到端验证 | backend tests, frontend build, Docker runtime | 新增 16 项相关测试并通过，前端生产构建通过，后端与前端容器已重建且健康；浏览器受本机既定 localhost 访问限制，未对生产页重复截图。 |
| [x] | 记录直连 / Higress 双模式同步决策 | `docs/adr/ADR-002-dual-mode-provider-sync.md` | Provider Profile 只录入一次并作为凭证事实源，向 Higress 单向幂等同步；Higress Console 负责路由与治理。 |
| [ ] | 实现 Provider Profile 与 Higress 同步状态机 | backend + settings UI | 增加 Secret Store、受管资源 upsert、漂移检测、Key 轮换、Console 跳转和双模式切换前置校验。 |
| [x] | 记忆编辑器 | `frontend/src/components/settings/MemoryEditor.tsx` | 在设置页 Memory 分区提供 MEMORY.md 等记忆文件的 Monaco 编辑与保存能力。 |
| [x] | 右侧 Inspector 文件预览面板 | `frontend/src/components/editor/InspectorPanel.tsx` | 右侧面板支持 Memory/Skills/MCP 标签切换，选中文件后用 Monaco 预览/编辑。 |
| [x] | Skill 编辑器与文件树 | `frontend/src/app/skills/page.tsx`, `FileTree.tsx` | 扩展页支持 Monaco 编辑 SKILL.md、文件树导航、ZIP 导入、重命名与删除。 |
| [x] | 认证失败错误提示 | `frontend/src/components/chat/ChatMessage.tsx` | Assistant 消息检测到 401/API Key 错误时，显示红色认证失败提示并引导检查后端的 `.env` 配置。 |
| [x] | 助手头像渐变风格化 | `frontend/src/components/chat/ChatMessage.tsx` | Assistant 消息头像由纯深色背景改为品牌蓝紫渐变，与顶部品牌图标视觉统一。 |
| [x] | 导航栏去除底部分隔线 | `frontend/src/app/globals.css` | 移除 `glass-nav` 底部边框，使顶部与内容区更柔和地衔接。 |
| [ ] | 二阶段项目目录 API 设计落地 | 后端 API + 前端项目绑定 | 本阶段不做。 |
| [ ] | 三阶段右侧 coding agent 环境面板 | workspace/git/editor 能力 | 本阶段只保留现有 Inspector，不实现 git/diff/commit。 |

一阶段修改边界：

- 不新增后端 API。
- 不修改 agent/session/tool output/context maintenance 协议。
- 不接入真实本地目录选择、git diff、commit/push。
- 保持 `/skills`、`/skills/compare`、`/skills/review` 路由可访问。

## 阶段二：项目目录与本地电脑操作能力

### 目标

引入“项目目录”概念，让 PuddingClaw 可以围绕本地文件夹工作：

- 选择或绑定一个本地项目目录。
- 将 session 与项目关联。
- 允许 agent 读取/写入项目目录内文件。
- 前端展示当前项目、路径、最近会话。
- 为未来 coding agent 铺好 workspace API。

### 容器部署时能操作本地电脑吗？

结论：**不能天然操作整台本地电脑，只能操作被授权暴露给后端的路径。**

如果后端运行在 Docker 容器里：

- 容器只能看到容器文件系统。
- 宿主机目录只有通过 `volumes` 挂载进容器后，后端才能读写。
- 例如：

```yaml
volumes:
  - /Users/pet/Code:/host/Code
```

这样后端可以操作 `/host/Code` 下的项目，但不能访问没有挂载的 `/Users/pet/Desktop`、`Downloads` 或其他任意目录。

因此二阶段有三种部署模式：

| 模式 | 能力 | 优点 | 风险/限制 |
| --- | --- | --- | --- |
| 容器挂载 workspace 根目录 | 操作挂载目录内文件 | 简单、可控、适合私有部署 | 只能操作挂载范围内文件 |
| 后端直接跑在宿主机 | 可按进程权限访问本机路径 | 本地体验最好 | 权限风险更高，需要更强安全策略 |
| 宿主侧 helper/desktop bridge | 容器通过本地 helper 操作文件/打开应用 | 兼顾容器隔离和桌面能力 | 实现复杂，需要鉴权和审计 |

推荐二阶段先采用 **容器挂载 workspace 根目录**：

```text
宿主机：/Users/pet/Code
容器内：/workspace-host
```

再通过后端配置限制允许访问的 project roots，避免容器获得过大的文件系统权限。

### 后端能力建议

二阶段需要新增或整理 API：

```text
GET  /api/workspaces
POST /api/workspaces
GET  /api/workspaces/{id}
GET  /api/workspaces/{id}/files
GET  /api/workspaces/{id}/file?path=
POST /api/workspaces/{id}/file
POST /api/workspaces/{id}/open-location
```

其中 `open-location` 在容器部署下不能直接打开宿主 Finder/VS Code，除非有宿主侧 helper。二阶段可以先返回路径和说明，真正“打开位置”延后。

### 数据结构建议

```json
{
  "id": "workspace_xxx",
  "name": "PuddingClaw",
  "root": "/workspace-host/PuddingClaw",
  "host_root": "/Users/pet/Code/AI/Agent/PuddingClaw",
  "created_at": 0,
  "updated_at": 0
}
```

session 增加：

```json
{
  "workspace_id": "workspace_xxx",
  "workspace_root": "/workspace-host/PuddingClaw"
}
```

### 安全要求

- 所有文件读写必须校验路径在允许的 workspace root 内。
- 禁止 `..` 路径逃逸。
- 写文件要有审计日志。
- 删除、覆盖、批量移动属于高风险操作，先不开放或必须二次确认。
- 容器挂载目录建议从小范围开始，不要直接挂载整个用户 home。

### 验收标准

- 能创建/选择一个项目目录。
- 会话能绑定到项目。
- agent 只能读写项目目录内文件。
- 容器部署下，未挂载目录不可访问。
- 前端能显示当前项目名和路径。

## 阶段三：Coding Agent 环境面板与右侧信息区

### 目标

等 PuddingClaw 完整具备 coding agent 能力后，再适配类似 Codex 右侧环境信息面板：

- 当前项目环境信息。
- Git branch / changed files / diff summary。
- 测试、构建、lint 状态。
- “提交或推送”入口。
- 文件变更卡片。
- open in editor / reveal location。

### 为什么延后

当前 PuddingClaw 还不是完整 coding agent。过早实现右侧环境面板会带来两个问题：

- UI 上暗示了 coding agent 能力，但后端能力不完整，用户预期会错位。
- Git、文件变更、终端命令、编辑器打开都需要更严格的权限和审计模型。

因此三阶段只保留设计空间，一阶段可以在布局上预留右侧 panel slot，但默认不展示或只放空状态。

### 三阶段可能涉及的能力

```text
GET /api/workspaces/{id}/git/status
GET /api/workspaces/{id}/git/diff
POST /api/workspaces/{id}/git/commit
POST /api/workspaces/{id}/git/push
POST /api/workspaces/{id}/commands
POST /api/workspaces/{id}/open-in-editor
```

### 验收标准

- agent 能真实读写项目代码并运行验证命令。
- 前端右侧面板展示的信息来自真实项目状态。
- 用户能区分“环境状态”“工具调用”“assistant 回复”。
- 高风险操作有确认、日志和失败可见性。

## 推荐实施节奏

### Sprint 1：UI Shell

- 新增 `WorkspaceShell`。
- 重构左侧导航为工作台样式。
- 中间聊天区改为暗色工作台视觉。
- Skills/MCP 入口改名并收入“扩展”。

### Sprint 2：Chat 组件 polish

- 优化消息样式。
- 优化 tool card 折叠、错误态、摘要态。
- 优化输入框和 context maintenance 状态。
- 保证现有协议不变。

### Sprint 3：Workspace 基础

- 增加 workspace 数据模型和 API。
- 支持容器挂载目录作为项目根。
- session 绑定 workspace。
- 前端显示当前项目。

### Sprint 4：本地文件能力

- 文件树、读文件、写文件。
- 路径安全校验。
- 审计日志。
- 暂不做 git 提交/推送。

### Sprint 5：Coding Agent 面板（可选）

- Git 状态。
- 变更摘要。
- 运行命令状态。
- 右侧环境信息面板。

## 当前决策

- 一阶段只改 PuddingClaw 当前前端 UI，不动后端接口。
- Skills 和 MCP 统一收入“扩展/插件”入口，文案从“技能”扩展为“扩展能力”。
- 二阶段再做项目目录和本地操作能力。
- 容器部署只能操作挂载目录；如需操作宿主机任意位置，需要宿主侧 helper 或非容器部署。
- 三阶段右侧环境信息面板先不做，等 coding agent 能力成熟后再适配。
