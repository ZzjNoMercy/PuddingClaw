"""Shared contracts for managed web-search adapters."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

ProviderId = Literal["tavily", "deepseek", "grok"]
SearchScope = Literal["auto", "domestic", "global"]
SearchSource = Literal["auto", "web", "x", "both"]


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    scope: SearchScope = "auto"
    source: SearchSource = "auto"
    provider: Literal["auto", "tavily", "deepseek", "grok"] = "auto"
    cross_check: bool = False
    max_results: int = Field(default=5, ge=1, le=10)
    include_domains: list[str] = Field(default_factory=list, max_length=5)
    exclude_domains: list[str] = Field(default_factory=list, max_length=5)
    time_range: Literal["day", "week", "month", "year"] | None = None
    allowed_x_handles: list[str] = Field(default_factory=list, max_length=20)
    excluded_x_handles: list[str] = Field(default_factory=list, max_length=20)
    from_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    to_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    enable_image_understanding: bool = False
    enable_image_search: bool = False
    enable_video_understanding: bool = False

    @model_validator(mode="after")
    def validate_filters(self) -> SearchRequest:
        if self.include_domains and self.exclude_domains:
            raise ValueError("include_domains 与 exclude_domains 不能同时设置")
        if self.allowed_x_handles and self.excluded_x_handles:
            raise ValueError("allowed_x_handles 与 excluded_x_handles 不能同时设置")
        if self.source == "web" and (self.allowed_x_handles or self.excluded_x_handles):
            raise ValueError("X 账号过滤仅适用于 source=x 或 source=both")
        if self.source == "x" and (self.include_domains or self.exclude_domains):
            raise ValueError("网页域名过滤不适用于 source=x")
        if self.source == "x" and self.enable_image_search:
            raise ValueError("图片搜索仅适用于 source=web 或 source=both")
        if self.source == "web" and self.enable_video_understanding:
            raise ValueError("视频理解仅适用于 source=x 或 source=both")
        if (
            self.enable_image_understanding
            or self.enable_image_search
            or self.enable_video_understanding
        ) and self.provider not in {"auto", "grok"}:
            raise ValueError("图片搜索、图片理解与视频理解仅由 Grok 支持")
        return self


class SearchResult(BaseModel):
    title: str
    uri: str
    quote: str = ""
    source_type: Literal["web", "x"] = "web"
    published_at: str | None = None
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AdapterResponse(BaseModel):
    provider: ProviderId
    answer_context: str
    sources: list[SearchResult] = Field(default_factory=list)
    latency_ms: int
    usage: dict[str, Any] = Field(default_factory=dict)
    server_tools: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    answer_context: str
    sources: list[SearchResult]
    requested_scope: SearchScope
    resolved_scope: Literal["domestic", "global"]
    requested_source: SearchSource
    resolved_source: Literal["web", "x", "both"]
    selected_provider: ProviderId
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)


class WebSearchError(RuntimeError):
    """Typed provider failure used to decide whether fallback is safe."""

    def __init__(self, message: str, *, category: str = "provider_error", retryable: bool = True) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable
