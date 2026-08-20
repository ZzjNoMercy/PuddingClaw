# 外部 Agent Skills

本目录存放供 Pi、OpenClaw、Pudding Platform、Codex 等**外部 Agent 宿主**安装和使用的 Skills。它们通过公开的 CLI / Worker 协议调用 PuddingClaw，不属于 PuddingClaw Backend 自身的运行时技能库。

## 与 `backend/skills` 的区别

| 目录 | 使用者 | 执行位置 | 主要职责 |
|---|---|---|---|
| `skills/` | 外部 Agent 或 Platform | 外部宿主环境 | 发现并调用 PuddingClaw CLI，解释 Worker 协议和返回结果 |
| `backend/skills/` | PuddingClaw 内部 Agent | PuddingClaw Backend / Sandbox | 指导内部 Agent 使用数据库、知识库、Lark 等 PuddingClaw 工具 |

两类 Skill 可以描述相近业务能力，但生命周期和信任边界不同，不应互相移动或混用。

## 目录约定

- 每个子目录是一个可独立分发的 Skill，以 `SKILL.md` 为入口。
- 外部 Skill 只能依赖公开 CLI / Worker 契约，不导入 Backend 私有模块或宿主绝对路径。
- 本机 CLI 接入不携带 PuddingClaw Token；模型、数据源和工具权限由 PuddingClaw Backend 自己管理。
- 外部 Agent 不得把模型 Provider 凭据或其他 PuddingClaw 内部秘密复制到 Skill 目录。
- 单个 Skill 内不再增加 README；安装、调用和安全规则写在其 `SKILL.md` 中。

## 当前 Skills

- [`puddingclaw`](./puddingclaw/SKILL.md)：通过本地 `puddingclaw` Node CLI 将企业数据分析任务委派给 PuddingClaw Worker。
