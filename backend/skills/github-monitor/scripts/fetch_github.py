#!/usr/bin/env python3
"""
GitHub Monitor — Fetch Script

从 GitHub REST API 拉取指定仓库的最新动态数据，输出 JSON 到 stdout。

API 端点 (全部公开, 无需认证):
  - /repos/{owner}/{repo}          基础统计
  - /repos/{owner}/{repo}/releases  Releases (最近 N 个)
  - /repos/{owner}/{repo}/commits   Commits (最近 N 条)
  - /repos/{owner}/{repo}/pulls     Pull Requests (最近 N 条)

速率限制: 未认证 60 req/h。每仓库 4 次请求。

Usage:
    python3 fetch_github.py --repo langchain-ai/langchain
    python3 fetch_github.py --all
    python3 fetch_github.py --repos "owner1/repo1,owner2/repo2"
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = "https://api.github.com"
USER_AGENT = "github-monitor-skill/1.0"
DEFAULT_REPOS_YAML = Path(__file__).resolve().parent.parent / "references" / "repos.yaml"

DEFAULT_MAX_RELEASES = 5
DEFAULT_MAX_COMMITS = 10
DEFAULT_MAX_PULLS = 10
MAX_RETRIES = 2


def load_default_repos():
    """从 repos.yaml 加载默认仓库列表（简易 YAML 解析，不依赖 PyYAML）。"""
    repos = []
    try:
        content = DEFAULT_REPOS_YAML.read_text()
        in_repos = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("repos:"):
                in_repos = True
                continue
            if in_repos:
                if stripped.startswith("- "):
                    repos.append(stripped[2:].strip())
                elif stripped and not stripped.startswith("#"):
                    break
    except FileNotFoundError:
        pass
    return repos


def api_get(path, params=None):
    """调用 GitHub REST API，返回解析后的 JSON。"""
    url = f"{API_BASE}{path}"
    if params:
        pairs = [f"{k}={v}" for k, v in params.items()]
        url = f"{url}?{'&'.join(pairs)}"

    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")

    last_error = None
    for attempt in range(1 + MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code == 403:
                print(f"  ⚠ 403 Forbidden (可能触发速率限制)", file=sys.stderr)
                return {"error": "rate_limited"}
            if e.code == 404:
                return {"error": "not_found"}
            if attempt < MAX_RETRIES:
                time.sleep(1 * (attempt + 1))
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES:
                time.sleep(1 * (attempt + 1))

    return {"error": str(last_error)}


def fetch_repo(owner_repo, max_releases, max_commits, max_pulls):
    """拉取单个仓库的全部数据。"""
    result = {"repo": owner_repo, "errors": []}

    # 基础统计
    print(f"  📊 基础统计: {owner_repo}", file=sys.stderr)
    stats = api_get(f"/repos/{owner_repo}")
    if isinstance(stats, dict) and "error" in stats:
        result["errors"].append(f"stats: {stats['error']}")
        result["stats"] = {}
    else:
        result["stats"] = {
            "stars": stats.get("stargazers_count", 0),
            "forks": stats.get("forks_count", 0),
            "open_issues": stats.get("open_issues_count", 0),
            "language": stats.get("language", "N/A"),
            "pushed_at": stats.get("pushed_at", ""),
            "description": stats.get("description", ""),
        }

    # Releases
    print(f"  🏷  Releases: {owner_repo}", file=sys.stderr)
    releases = api_get(f"/repos/{owner_repo}/releases", {"per_page": max_releases})
    if isinstance(releases, dict) and "error" in releases:
        result["errors"].append(f"releases: {releases['error']}")
        result["releases"] = []
    else:
        result["releases"] = [
            {
                "tag_name": r.get("tag_name", ""),
                "name": r.get("name", ""),
                "author": r.get("author", {}).get("login", "N/A"),
                "published_at": r.get("published_at", ""),
                "body": r.get("body", ""),
                "html_url": r.get("html_url", ""),
            }
            for r in (releases if isinstance(releases, list) else [])
        ]

    # Commits
    print(f"  📝 Commits: {owner_repo}", file=sys.stderr)
    commits = api_get(f"/repos/{owner_repo}/commits", {"per_page": max_commits})
    if isinstance(commits, dict) and "error" in commits:
        result["errors"].append(f"commits: {commits['error']}")
        result["commits"] = []
    else:
        result["commits"] = [
            {
                "sha": c.get("sha", "")[:7],
                "full_sha": c.get("sha", ""),
                "message": (c.get("commit", {}).get("message", "").split("\n")[0])[:120],
                "author": c.get("commit", {}).get("author", {}).get("name", "N/A"),
                "date": c.get("commit", {}).get("author", {}).get("date", ""),
                "html_url": c.get("html_url", ""),
            }
            for c in (commits if isinstance(commits, list) else [])
        ]

    # Pull Requests
    print(f"  🔀 Pull Requests: {owner_repo}", file=sys.stderr)
    pulls = api_get(
        f"/repos/{owner_repo}/pulls",
        {"per_page": max_pulls, "state": "all", "sort": "updated", "direction": "desc"},
    )
    if isinstance(pulls, dict) and "error" in pulls:
        result["errors"].append(f"pulls: {pulls['error']}")
        result["pull_requests"] = []
    else:
        result["pull_requests"] = [
            {
                "number": p.get("number", 0),
                "title": p.get("title", ""),
                "state": p.get("state", "unknown"),
                "author": p.get("user", {}).get("login", "N/A"),
                "updated_at": p.get("updated_at", ""),
                "html_url": p.get("html_url", ""),
            }
            for p in (pulls if isinstance(pulls, list) else [])
        ]

    return result


def main():
    parser = argparse.ArgumentParser(description="GitHub Monitor — Fetch")
    parser.add_argument("--repo", help="单个仓库 (owner/repo)")
    parser.add_argument("--repos", help="逗号分隔的仓库列表")
    parser.add_argument("--all", action="store_true", help="拉取默认仓库列表")
    parser.add_argument("--max-releases", type=int, default=DEFAULT_MAX_RELEASES)
    parser.add_argument("--max-commits", type=int, default=DEFAULT_MAX_COMMITS)
    parser.add_argument("--max-pulls", type=int, default=DEFAULT_MAX_PULLS)
    args = parser.parse_args()

    repos = []
    if args.repo:
        repos = [args.repo]
    elif args.repos:
        repos = [r.strip() for r in args.repos.split(",") if r.strip()]
    elif args.all:
        repos = load_default_repos()

    if not repos:
        print("❌ 未指定仓库。使用 --repo, --repos 或 --all", file=sys.stderr)
        sys.exit(1)

    print(f"🚀 开始拉取 {len(repos)} 个仓库...\n", file=sys.stderr)

    results = []
    for r in repos:
        data = fetch_repo(r, args.max_releases, args.max_commits, args.max_pulls)
        results.append(data)

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
