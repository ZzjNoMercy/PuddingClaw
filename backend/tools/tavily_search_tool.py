"""TavilySearchTool — structured web search with citation-ready results."""

import os
import time
from typing import Type

import requests
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from graph.citations import encode_tool_result


class TavilySearchInput(BaseModel):
    query: str = Field(description="要搜索的网页问题或关键词")
    max_results: int = Field(default=5, ge=1, le=10, description="返回结果数，默认 5")


class TavilySearchTool(BaseTool):
    name: str = "tavily_search"
    description: str = (
        "Search the public web and return recent results with title, URL and snippet. "
        "Use for general news/current information when no domain-specific Skill applies. "
        "Prefer this over repeatedly guessing search-page URLs with fetch_url."
    )
    args_schema: Type[BaseModel] = TavilySearchInput
    risk_level: str = "safe"

    def _run(self, query: str, max_results: int = 5) -> str:
        api_key = os.getenv("TAVILY_API_KEY", "").strip()
        if not api_key:
            return "❌ Tavily search unavailable: TAVILY_API_KEY is not configured"

        last_error: Exception | None = None
        payload = None
        for attempt in range(2):
            try:
                response = requests.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": api_key,
                        "query": query,
                        "max_results": max(1, min(max_results, 10)),
                        "search_depth": "basic",
                        "include_answer": False,
                        "include_images": False,
                        "include_raw_content": False,
                    },
                    headers={"Accept": "application/json"},
                    timeout=15,
                )
                response.raise_for_status()
                payload = response.json()
                break
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.4)
        if payload is None:
            return f"❌ Tavily search failed after retry: {last_error}"

        sources = []
        lines = [f"网页搜索“{query}”返回以下结果："]
        for index, item in enumerate(payload.get("results") or [], 1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            snippet = str(item.get("content") or "").strip()
            if not title or not url.startswith(("http://", "https://")):
                continue
            source = {
                "title": title,
                "uri": url,
                "document_id": url,
                "chunk_id": "tavily-result",
                "source_type": "web",
                "quote": snippet,
                "score": item.get("score"),
                "metadata": {"adapter": "tavily_search", "query": query},
            }
            sources.append(source)
            lines.append(f"{index}. {title}\n   {snippet}")

        if not sources:
            return f"Tavily 没有找到与“{query}”相关的有效网页结果。"
        return encode_tool_result("\n".join(lines), sources)


def create_tavily_search_tool() -> TavilySearchTool:
    return TavilySearchTool()
