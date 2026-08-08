"""One stable Agent tool backed by user-configured web-search providers."""

from __future__ import annotations

from typing import Literal

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, ValidationError

from graph.citations import encode_tool_result
from web_search.models import SearchRequest, WebSearchError
from web_search.service import get_web_search_service


class WebSearchInput(BaseModel):
    query: str = Field(description="要检索的公开网络问题或关键词")
    scope: Literal["auto", "domestic", "global"] = Field(
        default="auto", description="国内公网、全球公网或自动判断"
    )
    source: Literal["auto", "web", "x", "both"] = Field(
        default="auto", description="普通网页、X、两者或自动判断"
    )
    provider: Literal["auto", "tavily", "deepseek", "grok"] = Field(
        default="auto", description="仅在用户明确指定供应商时覆盖自动路由"
    )
    cross_check: bool = Field(
        default=False,
        description="用户明确要求多源核实时设为 true；还需要设置页允许交叉验证",
    )
    max_results: int = Field(default=5, ge=1, le=10, description="最多返回的可引用来源数")
    include_domains: list[str] = Field(default_factory=list, description="只包含这些网页域名，最多 5 个")
    exclude_domains: list[str] = Field(default_factory=list, description="排除这些网页域名，最多 5 个")
    time_range: Literal["day", "week", "month", "year"] | None = Field(
        default=None, description="网页搜索的相对时间范围"
    )
    allowed_x_handles: list[str] = Field(default_factory=list, description="只检索这些 X 账号，最多 20 个")
    excluded_x_handles: list[str] = Field(default_factory=list, description="排除这些 X 账号，最多 20 个")
    from_date: str | None = Field(default=None, description="X Search 起始日期，YYYY-MM-DD")
    to_date: str | None = Field(default=None, description="X Search 结束日期，YYYY-MM-DD")


class ManagedWebSearchTool(BaseTool):
    name: str = "web_search"
    description: str = (
        "Search the configured public internet providers and return citation-ready sources. "
        "Use scope=domestic for Chinese mainland public-web intent, scope=global for global sources, "
        "and source=x when the user asks what people or accounts say on X. "
        "Use provider only when the user explicitly names Tavily, DeepSeek, or Grok."
    )
    args_schema: type[BaseModel] = WebSearchInput
    risk_level: str = "safe"

    def _run(self, **kwargs) -> str:
        try:
            request = SearchRequest(**kwargs)
            response = get_web_search_service().search(request)
        except ValidationError as exc:
            return f"❌ 联网搜索参数无效：{exc.errors()[0]['msg']}"
        except (ValueError, WebSearchError) as exc:
            return f"❌ {exc}"

        sources = [
            {
                "title": item.title,
                "uri": item.uri,
                "document_id": item.uri,
                "chunk_id": f"{response.selected_provider}-{item.source_type}-result",
                "source_type": item.source_type,
                "quote": item.quote,
                "score": item.score,
                "metadata": {
                    **item.metadata,
                    "provider": response.selected_provider,
                    "resolved_scope": response.resolved_scope,
                    "resolved_source": response.resolved_source,
                },
            }
            for item in response.sources
        ]
        context = (
            f"搜索路由：{response.selected_provider} · {response.resolved_scope} · {response.resolved_source}\n"
            f"{response.answer_context}"
        )
        return encode_tool_result(context, sources)


def create_web_search_tool() -> ManagedWebSearchTool:
    return ManagedWebSearchTool()
