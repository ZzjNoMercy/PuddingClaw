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
frontend/src/app/globals.css
frontend/src/components/layout/Sidebar.tsx
frontend/src/components/layout/Navbar.tsx
frontend/src/components/layout/SkillsBar.tsx
frontend/src/components/chat/ChatPanel.tsx
frontend/src/components/chat/ChatMessage.tsx
frontend/src/components/chat/ChatInput.tsx
frontend/src/components/chat/ThoughtChain.tsx
frontend/src/components/chat/RetrievalCard.tsx
frontend/src/lib/store.tsx
frontend/src/lib/navigation.ts
```

建议新增：

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
