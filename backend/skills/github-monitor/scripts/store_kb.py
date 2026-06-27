#!/usr/bin/env python3
"""
GitHub Monitor — Store Script

从 stdin 读取 fetch_github.py 输出的 JSON，格式化为 Markdown 文档，
写入知识库目录。

Usage:
    python3 fetch_github.py --all | python3 store_kb.py
    python3 fetch_github.py --repo langchain-ai/langchain | python3 store_kb.py --kb-path /custom/path/
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Agent 应使用 write_file 工具直接写入 /knowledge/
# 脚本运行时默认写入 /knowledge/（若不可写则回退到 /tmp）
DEFAULT_KB_PATH = Path("/knowledge")


def format_markdown(data):
    """将单个仓库的 JSON 数据格式化为 Markdown 字符串。"""
    repo = data.get("repo", "unknown")
    stats = data.get("stats", {})
    releases = data.get("releases", [])
    commits = data.get("commits", [])
    pulls = data.get("pull_requests", [])
    errors = data.get("errors", [])

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [f"# {repo} 更新追踪", "", f"> 拉取时间: {fetched_at}", ""]

    if errors:
        lines.append(f"> ⚠ 部分数据拉取失败: {', '.join(errors)}")
        lines.append("")

    # ── 仓库概览 ──
    if stats:
        lines.extend([
            "---", "",
            "## 仓库概览", "",
            f"- ⭐ Stars: {stats.get('stars', 0):,}",
            f"- 🍴 Forks: {stats.get('forks', 0):,}",
            f"- 🐛 Open Issues: {stats.get('open_issues', 0):,}",
            f"- 💻 主要语言: {stats.get('language', 'N/A')}",
            f"- 🔄 最后推送: {stats.get('pushed_at', 'N/A')[:10]}",
        ])
        desc = stats.get("description", "")
        if desc:
            lines.append(f"- 📝 描述: {desc}")
        lines.append("")

    # ── Releases ──
    lines.extend(["---", "", "## 最新 Release", ""])
    if releases:
        for r in releases:
            tag = r.get("tag_name", "")
            name = r.get("name", tag)
            display = f"{name} ({tag})" if name and name != tag else tag
            lines.append(f"### {display}")
            lines.append(f"- 发布者: {r.get('author', 'N/A')}")
            lines.append(f"- 发布时间: {r.get('published_at', 'N/A')[:10]}")
            body = r.get("body", "")
            if body:
                lines.append("- 更新说明:")
                lines.append("")
                truncated = body[:2000]
                if len(body) > 2000:
                    truncated += "\n\n...(内容过长已截断)"
                lines.append("```")
                lines.append(truncated)
                lines.append("```")
            if r.get("html_url"):
                lines.append(f"- [查看详情]({r['html_url']})")
            lines.append("")
    else:
        lines.append("暂无 Release 信息。\n")

    # ── Commits ──
    lines.extend(["---", "", "## 最近 Commit", ""])
    if commits:
        lines.append("| SHA | 消息 | 作者 | 日期 |")
        lines.append("|-----|------|------|------|")
        for c in commits:
            sha = c.get("sha", "")
            msg = c.get("message", "").replace("|", "\\|")
            author = c.get("author", "N/A")
            date = c.get("date", "")[:10]
            url = c.get("html_url", "")
            sha_link = f"[`{sha}`]({url})" if url else f"`{sha}`"
            lines.append(f"| {sha_link} | {msg} | {author} | {date} |")
        lines.append("")
    else:
        lines.append("暂无 Commit 信息。\n")

    # ── Pull Requests ──
    lines.extend(["---", "", "## 最近 Pull Request", ""])
    if pulls:
        lines.append("| # | 标题 | 状态 | 作者 | 更新时间 |")
        lines.append("|---|------|------|------|----------|")
        for p in pulls:
            num = p.get("number", 0)
            title = p.get("title", "").replace("|", "\\|")
            state = p.get("state", "unknown")
            state_icon = "🟢" if state == "open" else "🔴" if state == "closed" else "⚪"
            author = p.get("author", "N/A")
            date = p.get("updated_at", "")[:10]
            url = p.get("html_url", "")
            pr_link = f"[#{num}]({url})" if url else f"#{num}"
            lines.append(f"| {pr_link} | {title} | {state_icon} {state} | {author} | {date} |")
        lines.append("")
    else:
        lines.append("暂无 Pull Request 信息。\n")

    lines.append("---")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="GitHub Monitor — Store to KB")
    parser.add_argument("--kb-path", default=str(DEFAULT_KB_PATH),
                        help=f"知识库存储路径 (默认: {DEFAULT_KB_PATH})")
    args = parser.parse_args()

    kb_dir = Path(args.kb_path)
    kb_dir.mkdir(parents=True, exist_ok=True)

    raw = sys.stdin.read()
    if not raw.strip():
        print("❌ stdin 无数据", file=sys.stderr)
        sys.exit(1)

    data_list = json.loads(raw)
    if isinstance(data_list, dict):
        data_list = [data_list]

    written = []
    for data in data_list:
        md = format_markdown(data)
        repo_name = data.get("repo", "unknown").replace("/", "_")
        fname = f"{repo_name}_tracker.md"
        fpath = kb_dir / fname
        fpath.write_text(md)
        written.append(str(fpath))

    print(f"✅ 已写入 {len(written)} 个文件:", file=sys.stderr)
    for w in written:
        print(f"   {w}", file=sys.stderr)


if __name__ == "__main__":
    main()
