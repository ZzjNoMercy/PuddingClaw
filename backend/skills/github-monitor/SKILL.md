---
name: github-monitor
description: 监控指定 GitHub 开源项目的更新情况（Release、Commit、PR、Star 趋势），拉取最新数据并存入本地知识库供检索。Use when asked to "监控GitHub项目", "查看开源项目更新", "github项目最近有什么变化", "拉取最新release", "追踪仓库动态", or any request about tracking GitHub repo updates.
---

# GitHub Monitor

## Goal

从 GitHub API 拉取指定仓库的最新动态（Releases、Commits、Pull Requests、基础统计），格式化为结构化 Markdown 文档，存入本地知识库目录，供后续检索。

## Workflow

### Primary Path（Agent 直接执行，推荐）

对每个目标仓库，并行调用 `fetch_url` 拉取以下 GitHub API 端点：

| 数据 | API 端点 | per_page |
|------|----------|----------|
| 基础统计 | `https://api.github.com/repos/{owner}/{repo}` | — |
| Releases | `https://api.github.com/repos/{owner}/{repo}/releases` | 5 |
| Commits | `https://api.github.com/repos/{owner}/{repo}/commits` | 10 |
| Pull Requests | `https://api.github.com/repos/{owner}/{repo}/pulls?state=all&sort=updated&direction=desc` | 10 |

然后使用 `write_file` 将格式化后的 Markdown 写入 `/knowledge/{owner}_{repo}_tracker.md`。

### Script Path（备选，终端直接运行）

```bash
python3 scripts/fetch_github.py --repo langchain-ai/langchain | python3 scripts/store_kb.py
```

```
1. 确认目标仓库列表
   ├─ 用户指定 → 使用用户指定的仓库
   └─ 未指定 → 加载 references/repos.yaml 默认列表

2. 逐仓库拉取数据（可并行调用 fetch_url）
   ├─ 基础统计：stars, forks, open_issues, language, pushed_at
   ├─ 最新 Release（最近 5 个）
   ├─ 最近 Commit（最近 5-10 条）
   └─ 最近 Pull Request（最近 5-10 条，含状态）

3. 格式化输出
   └─ 每个仓库生成独立 Markdown 文档
       命名：{owner}_{repo}_tracker.md

4. 存入知识库目录
   └─ 写入 /knowledge/ 目录

5. 返回摘要
   └─ 汇总所有仓库的关键变化
```

## Decision Tree

- **用户请求查看某仓库更新** → 只拉取该仓库
- **用户说"监控这几个项目"** → 使用用户提供的仓库列表
- **用户说"看看开源项目有什么更新"** → 使用默认仓库列表
- **用户只问某个项目的 Star 数/基础信息** → 只拉取基础统计，跳过 Commits/PRs

## Constraints

- GitHub API 未认证请求速率限制 60 次/小时。对 4 个仓库（各 4 类请求 = 16 次调用）安全。
- **重要**: 共享出口 IP（如终端沙箱、fetch_url）可能已被 GitHub 全局限速 → API 返回 403。
  此时应自动降级到 **Tavily Search 回退方案**：
  1. 对每个仓库并行调用 `tavily_search(query="site:github.com {owner}/{repo} stars release")`
  2. 再搜索 `tavily_search(query="{owner}/{repo} github latest release 2026")`
  3. 从搜索结果中提取 star 数、release 版本、commit 信息、PR 动态
  4. 将结构化数据写入 `/knowledge/{owner}_{repo}_tracker.md`
- 每次调用 `scripts/fetch_github.py` 必须指定 `--repo` 参数（格式 `owner/repo`）。
- 拉取到的文档存入 `/knowledge/` 后，提醒用户可通过 `search_knowledge_base` 检索。
- 网络失败时不要重试超过 2 次，返回已有数据并报告失败仓库。
- **终端沙箱 `/knowledge/` 为只读**：脚本输出到 `/tmp/`，再用 `write_file` 工具搬运到 `/knowledge/`。

## Validation

- 每个仓库至少拉取到基础统计（stars >= 0）。
- 生成的 Markdown 文件至少包含 `## 仓库概览` 和 `## 最新 Release` 或 `## 最近 Commit` 之一。
- 文件成功写入知识库目录。

## Resources

- `scripts/fetch_github.py` — GitHub API 客户端，拉取指定仓库的 Release/Commit/PR/统计
- `scripts/store_kb.py` — 将拉取结果格式化并写入知识库
- `references/repos.yaml` — 默认监控仓库列表及配置

## Usage Examples

```
# 拉取所有默认仓库
python3 scripts/fetch_github.py --all | python3 scripts/store_kb.py

# 只拉取单个仓库
python3 scripts/fetch_github.py --repo langchain-ai/langchain | python3 scripts/store_kb.py

# 指定仓库列表
python3 scripts/fetch_github.py --repos "langchain-ai/langchain,alibaba/higress" | python3 scripts/store_kb.py
```
