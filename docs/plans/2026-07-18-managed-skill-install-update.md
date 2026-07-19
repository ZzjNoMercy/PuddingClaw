# 受管 Skill 安装与更新

> 日期：2026-07-18  
> 状态：已实现

## 目标

Agent 可以安装和更新 Skill，但普通 `execute`、`write_file`、`edit_file` 仍不能写入 `/skills`。管理操作由默认 `skill_management` Toolset 提供，并与通用工作区写权限隔离。

## 工具流程

安装：

1. `prepare_skill_install` 联网下载到 `data/skill-management/plans/` 暂存区；
2. 后端校验来源、文件数、体积、路径、符号链接、危险二进制和 `SKILL.md`；
3. 返回绑定 `plan_id + plan_sha256` 的不可变计划和文件 diff；
4. `install_skill` 触发一次性 HITL，授权卡显示后端验证过的来源、版本、diff 和计划摘要；
5. 授权后原子写入 `/skills/<name>`，已存在时拒绝覆盖。

更新：

1. `prepare_skill_update` 下载并与当前安装内容比较；
2. `update_skill` 触发一次性 HITL；
3. 提交前重新校验暂存摘要和已安装基线，任一变化都会拒绝；
4. 保存旧版本快照后原子替换，替换失败自动恢复旧目录；
5. 每个 Skill 最多保留最近 10 个回滚快照。

安装成功后，管理服务在 `data/skill-management/registry.json` 保存来源、ref、子目录和补充文件列表。以后更新由 `skill_name` 即可复用来源；手工导入、没有来源记录的 Skill 仍需显式传入 `source`。

## 权限边界

- 预检工具需要一次性 `temporary_network` 授权，只能写管理暂存区；
- 提交工具需要一次性 `managed_skill_write` 授权；
- 前端不显示 Session 授权按钮，后端 API 也拒绝把四个 Skill 管理动作授权为 Session scope；
- 提交权限与确切的计划 ID、摘要及后端 diff 绑定；
- Docker 和 Restricted Host 中原有 `/skills` 只读边界保持不变。

## 来源支持

- GitHub 仓库或 `tree/<ref>/<subpath>` URL；
- HTTP(S) ZIP；
- 含 `SKILL.md` 的 HTTP(S) 目录，并受限发现 `scripts/`、`assets/`、`references/`、`templates/` 引用；
- 私网、localhost、非标准端口、带凭证 URL 和重定向后的不安全地址均拒绝。

VPN/代理兼容说明见 [ADR-003：兼容 VPN/代理的 HTTPS Fake-IP DNS](../adr/ADR-003-vpn-fake-ip-https-compatibility.md)。Skill 下载器与 `fetch_url` 支持 Clash/sing-box 常见的 `198.18.0.0/15` Fake-IP，但仅限域名 HTTPS，并继续执行 TLS hostname 校验；不开 VPN 时公网解析行为不变。

## 验证

- 服务测试覆盖安装、防覆盖、更新快照、基线冲突、计划/暂存防篡改、防重放、失败回滚、恶意 ZIP、私网来源和来源复用；
- Harness 测试覆盖网络授权、`managed_skill_write`、真实 diff 预览和禁止 Session scope；
- Toolset 测试覆盖四个工具默认可见；
- `backend/tests` 完整回归通过；
- 前端 TypeScript 检查通过。
