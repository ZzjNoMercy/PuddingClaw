# langchain-ai/langchain 更新追踪

> 拉取时间: 2026-06-28 UTC | 数据来源: Tavily Search + GitHub 公开页面 (API 限速回退)

---

## 仓库概览

- ⭐ Stars: ~140,000
- 🍴 Forks: ~23,300
- 🐛 Open Issues: ~413
- 💻 主要语言: Python
- 📝 描述: The agent engineering platform — 构建 Agent 和 LLM 应用的框架

---

## 最新 Release

### langchain-anthropic==1.4.8 (2026-06-26)
- 发布者: github-actions[bot]
- fix(anthropic): keep initial text on `content_block_start`
- chore: bump langgraph-checkpoint 4.1.0 → 4.1.1
- fix(core): add messages to bare `raise ValueError` calls
- [查看详情](https://github.com/langchain-ai/langchain/releases/tag/langchain-anthropic%3D%3D1.4.8)

### langchain-fireworks==1.4.3 (2026-06-26)
- release(fireworks): 1.4.3
- chore: bump langsmith 0.8.14 → 0.8.18
- [查看详情](https://github.com/langchain-ai/langchain/releases/tag/langchain-fireworks%3D%3D1.4.3)

### langchain-openrouter==0.2.4 (2026-06-23)
- feat(openrouter): surface `parallel_tool_calls` on `bind_tools`
- chore(openrouter): bump `openrouter` floor to 0.9.2
- [查看详情](https://github.com/langchain-ai/langchain/releases/tag/langchain-openrouter%3D%3D0.2.4)

---

## JavaScript SDK 最新 Release

从 langchain-ai/langchainjs 追踪 [^src_6952e9d0367f61e0]:

| 包 | 版本 |
|---|------|
| @langchain/xai | 1.4.2 |
| langchain | 1.5.1 |
| @langchain/openrouter | 0.4.2 |
| @langchain/openai | 1.5.2 |
| @langchain/google | 0.2.1 |
| @langchain/fireworks | 0.2.2 |
| @langchain/deepseek | 1.1.2 |

---

## 生态动态

### langgraph 已知问题
- `langgraph-prebuilt` v1.0.9 与旧版 `langgraph` 存在破坏性变更 [^src_a78da83d665a38c9]
- 影响: langgraph < 1.1.3 用户升级 prebuilt 后报错 `Cannot import ServerInfo`

### langchain-community 归档
- `langchain-community` 仓库已于 **2026-06-19** 被归档为只读 [^src_3fc5fbe3923ef9ea]
