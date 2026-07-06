# higress-group/higress 更新追踪

> 拉取时间: 2026-06-28 UTC | 数据来源: Tavily Search + GitHub 公开页面 (API 限速回退)

---

## 仓库概览

- ⭐ Stars: ~8,700+
- 🍴 Forks: ~2,400+
- 💻 主要语言: Go (基于 Istio + Envoy，Wasm 插件支持 Go/Rust/JS)
- 📝 描述: AI Gateway | AI Native API Gateway — 云原生 API 网关，面向 AI 场景 [^src_c4c59850fe4fc588]

---

## 最新 Release

### 最新版本 (含 48 项更新) [^src_f0f069080ab51a0c]

**AI Gateway 增强：**
- 🔑 **Key Auth 多凭证支持** — 单服务多 credential，简化迁移和多客户端场景
- 🛡️ **ai-security-guard** — 新增 Embedding API 内容检测，增强 AI 安全可观测性
- 🔌 **AI Proxy vLLM passthrough** — Anthropic Messages + 新版 OpenAI 端点
- 📝 修复 `ai-proxy` 中 `basePath` 字段描述
- 🌐 改进 Vertex AI 对 Anthropic Messages 请求兼容性

### Himarket v0.7.0 [^src_88ca804f5cb00098]
- 🧩 Skill 市场 / Worker 模板市场
- 💻 HiCoding 智能体在线编程
- 📊 统一可观测性 + 门户菜单管理

### Himarket v0.6.0
- DashScope 文生图模型支持
- Nacos 商业版 MCP 数据导入

---

## 最近 Pull Request [^src_99ec91d84d0b1137]

| # | 标题 | 日期 |
|---|------|------|
| [#4015](https://github.com/higress-group/higress/pull/4015) | 待确认 | 2026-06-24 |
| [#4011](https://github.com/higress-group/higress/pull/4011) | feat(wasm-go): ai-anthropic-system-fold plugin | 2026-06-24 |
| [#4005](https://github.com/higress-group/higress/pull/4005) | 待确认 | 2026-06-23 |
| [#3995](https://github.com/higress-group/higress/pull/3995) | feat(ai-header-session): 统一 AI 客户端会话头插件 | 2026-06-18 |
| [#3947](https://github.com/higress-group/higress/pull/3947) | fix: init apikey conflict in ai-proxy wasm-go | 2026-06-10 |
| [#3921](https://github.com/higress-group/higress/pull/3921) | feat: session affinity for AI providers | 2026-06-05 |
| [#3888](https://github.com/higress-group/higress/pull/3888) | feat(ai-proxy): Claude Code mode config | 2026-05-27 |
| [#3670](https://github.com/higress-group/higress/pull/3670) | earlier PR | 2026-04-01 |
