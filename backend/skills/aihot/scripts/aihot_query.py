#!/usr/bin/env python3
"""Query AI HOT and emit a PuddingClaw structured tool-result envelope."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://aihot.virxact.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 "
    "Safari/537.36 aihot-skill/0.3.0"
)
SHANGHAI = timezone(timedelta(hours=8))
CATEGORY_LABELS = {
    "ai-models": "模型发布/更新",
    "ai-products": "产品发布/更新",
    "industry": "行业动态",
    "paper": "论文研究",
    "tip": "技巧与观点",
}
CATEGORY_KEYWORDS = (
    ("paper", ("论文", "研究", "paper")),
    ("ai-models", ("大模型", "模型发布", "模型更新", "model")),
    ("ai-products", ("产品", "应用", "product")),
    ("industry", ("行业", "融资", "创业", "industry")),
    ("tip", ("技巧", "观点", "教程", "tip")),
)
SEARCH_TERMS = (
    "OpenAI", "Anthropic", "Google", "DeepMind", "Microsoft", "Meta",
    "NVIDIA", "Apple", "Amazon", "xAI", "Mistral", "DeepSeek",
    "Qwen", "Claude", "Gemini", "ChatGPT", "GPT", "Sora", "RAG",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="查询 AI HOT 并保留可引用来源")
    parser.add_argument(
        "--user-query",
        default=os.environ.get("SKILL_USER_QUERY", ""),
        help="用户原始问题；未提供时读取 SKILL_USER_QUERY",
    )
    parser.add_argument("--kind", choices=("items", "daily", "dailies"))
    parser.add_argument("--mode", choices=("selected", "all"))
    parser.add_argument("--category", choices=tuple(CATEGORY_LABELS))
    parser.add_argument("--query", dest="search_query")
    parser.add_argument("--since", help="ISO-8601 时间")
    parser.add_argument("--hours", type=int)
    parser.add_argument("--days", type=int)
    parser.add_argument("--date", help="日报日期 YYYY-MM-DD")
    parser.add_argument("--take", type=int)
    parser.add_argument("--timeout", type=float, default=12.0)
    return parser.parse_args()


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(word.lower() in lowered for word in words)


def infer_request(args: argparse.Namespace) -> dict[str, Any]:
    query = (args.user_query or "").strip()
    lowered = query.lower()

    kind = args.kind
    if not kind:
        if _contains_any(query, ("日报存档", "日报列表", "有哪些日报", "列一下日报")):
            kind = "dailies"
        elif "日报" in query:
            kind = "daily"
        else:
            kind = "items"

    request: dict[str, Any] = {"kind": kind}
    if kind == "dailies":
        request["take"] = max(1, min(args.take or 14, 180))
        return request

    if kind == "daily":
        date = args.date
        if not date:
            explicit = re.search(r"(20\d{2}-\d{2}-\d{2})", query)
            if explicit:
                date = explicit.group(1)
            elif "前天" in query:
                date = (datetime.now(SHANGHAI) - timedelta(days=2)).date().isoformat()
            elif "昨天" in query or "昨日" in query:
                date = (datetime.now(SHANGHAI) - timedelta(days=1)).date().isoformat()
        if date:
            request["date"] = date
        return request

    request["mode"] = args.mode or (
        "all" if _contains_any(query, ("全部", "完整", "所有", "全量")) else "selected"
    )
    request["take"] = max(1, min(args.take or 30, 100))

    category = args.category
    if not category:
        for slug, keywords in CATEGORY_KEYWORDS:
            if _contains_any(query, keywords):
                category = slug
                break
    if category:
        request["category"] = category

    search_query = args.search_query
    if not search_query:
        for term in SEARCH_TERMS:
            if term.lower() in lowered:
                search_query = term
                break
    if search_query and len(search_query.strip()) >= 2:
        request["q"] = search_query.strip()[:200]

    if args.since:
        request["since"] = args.since
    else:
        hours = args.hours
        days = args.days
        if hours is None:
            match = re.search(r"(?:最近|过去)\s*(\d+)\s*(?:个)?小时", query)
            if match:
                hours = int(match.group(1))
        if days is None:
            match = re.search(r"(?:最近|过去)\s*(\d+)\s*(?:个)?天", query)
            if match:
                days = int(match.group(1))
            elif "一周" in query or "7天" in query or "七天" in query:
                days = 7
        delta = timedelta(hours=max(1, hours)) if hours else timedelta(days=max(1, min(days or 1, 7)))
        request["since"] = (
            datetime.now(timezone.utc) - delta
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return request


def build_url(request: dict[str, Any]) -> str:
    kind = request["kind"]
    if kind == "daily":
        suffix = f"/{request['date']}" if request.get("date") else ""
        return f"{BASE_URL}/api/public/daily{suffix}"
    if kind == "dailies":
        return f"{BASE_URL}/api/public/dailies?{urlencode({'take': request['take']})}"
    params = {
        key: request[key]
        for key in ("mode", "category", "since", "take", "q")
        if request.get(key) not in (None, "")
    }
    return f"{BASE_URL}/api/public/items?{urlencode(params)}"


def fetch_json(url: str, timeout: float, retries: int = 2) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("AI HOT 返回的不是 JSON 对象")
            return payload
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code < 500 or attempt == retries:
                raise RuntimeError(f"AI HOT HTTP {exc.code}: {detail}") from exc
            last_error = exc
        except (URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt == retries:
                break
        time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"AI HOT 请求失败: {last_error}")


def _source_id(uri: str, title: str) -> str:
    digest = hashlib.sha256(f"{uri}|{title}".encode("utf-8")).hexdigest()[:16]
    return f"src_{digest}"


def _clean(value: Any, limit: int = 800) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _make_source(
    *, title: str, uri: str, quote: str = "", document_id: str = "", metadata: dict[str, Any] | None = None,
    score: Any = None,
) -> dict[str, Any] | None:
    if not uri.startswith(("http://", "https://")):
        return None
    source: dict[str, Any] = {
        "source_id": _source_id(uri, title),
        "title": title or "AI HOT 来源",
        "uri": uri,
        "document_id": document_id or uri,
        "chunk_id": document_id or uri,
        "source_type": "web",
        "quote": _clean(quote),
        "metadata": {"provider": "aihot", "evidence_kind": "derived_summary", **(metadata or {})},
    }
    if score not in (None, ""):
        source["score"] = score
    return source


def format_items(payload: dict[str, Any], request: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    sources: list[dict[str, Any]] = []
    lines = [f"AI HOT 返回 {len(items)} 条动态（{'精选' if request['mode'] == 'selected' else '全部'}）："]
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        title = _clean(item.get("title") or item.get("title_en") or "未命名动态", 300)
        summary = _clean(item.get("summary"), 800)
        source = _make_source(
            title=title,
            uri=_clean(item.get("url"), 2000),
            quote=summary,
            document_id=_clean(item.get("id"), 300),
            score=item.get("score"),
            metadata={
                "source_name": _clean(item.get("source"), 200),
                "published_at": _clean(item.get("publishedAt"), 100),
                "category": _clean(item.get("category"), 100),
                "selected": bool(item.get("selected")),
            },
        )
        marker = ""
        if source:
            sources.append(source)
            marker = f" [^{source['source_id']}]"
        meta = " · ".join(
            filter(None, (_clean(item.get("source"), 100), _clean(item.get("publishedAt"), 40)))
        )
        lines.append(f"{index}. {title}{marker}" + (f"\n   {summary}" if summary else "") + (f"\n   {meta}" if meta else ""))
    if payload.get("hasNext"):
        lines.append("结果还有下一页；如需更多，可继续按 cursor 查询。")
    return "\n".join(lines), sources


def format_daily(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    date = _clean(payload.get("date"), 40) or "最新"
    lines = [f"AI HOT {date} 日报："]
    lead = payload.get("lead")
    if isinstance(lead, dict):
        lead_title = _clean(lead.get("title"), 300)
        lead_text = _clean(lead.get("leadParagraph"), 1200)
        if lead_title or lead_text:
            lines.append(f"导语：{lead_title}" + (f"\n{lead_text}" if lead_text else ""))
    sources: list[dict[str, Any]] = []
    for section in payload.get("sections", []):
        if not isinstance(section, dict):
            continue
        lines.append(f"\n## {_clean(section.get('label'), 100) or '其他'}")
        for item in section.get("items", []):
            if not isinstance(item, dict):
                continue
            title = _clean(item.get("title"), 300) or "未命名动态"
            summary = _clean(item.get("summary"), 800)
            uri = _clean(item.get("sourceUrl"), 2000)
            source = _make_source(
                title=title,
                uri=uri,
                quote=summary,
                metadata={"source_name": _clean(item.get("sourceName"), 200), "daily_date": date},
            )
            marker = ""
            if source:
                sources.append(source)
                marker = f" [^{source['source_id']}]"
            lines.append(f"- {title}{marker}" + (f"：{summary}" if summary else ""))
    flashes = payload.get("flashes") if isinstance(payload.get("flashes"), list) else []
    if flashes:
        lines.append("\n## 快讯")
    for item in flashes:
        if not isinstance(item, dict):
            continue
        title = _clean(item.get("title"), 300) or "未命名快讯"
        source = _make_source(
            title=title,
            uri=_clean(item.get("sourceUrl"), 2000),
            metadata={
                "source_name": _clean(item.get("sourceName"), 200),
                "published_at": _clean(item.get("publishedAt"), 100),
                "daily_date": date,
            },
        )
        marker = ""
        if source:
            sources.append(source)
            marker = f" [{source['source_id']}]"
        lines.append(f"- {title}{marker}")
    return "\n".join(lines), sources


def format_dailies(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    lines = [f"AI HOT 日报存档共返回 {len(items)} 条："]
    for item in items:
        if isinstance(item, dict):
            lines.append(f"- {_clean(item.get('date'), 40)}：{_clean(item.get('leadTitle'), 300)}")
    return "\n".join(lines), []


def main() -> int:
    args = parse_args()
    request = infer_request(args)
    try:
        payload = fetch_json(build_url(request), args.timeout)
        if request["kind"] == "items":
            answer_context, sources = format_items(payload, request)
        elif request["kind"] == "daily":
            answer_context, sources = format_daily(payload)
        else:
            answer_context, sources = format_dailies(payload)
        print(json.dumps({
            "puddingclaw_tool_result": 1,
            "answer_context": answer_context,
            "sources": sources,
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"AI HOT 查询失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
