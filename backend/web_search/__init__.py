"""Managed public-web search providers and deterministic routing."""

from .registry import WebSearchRegistry, get_web_search_registry
from .service import WebSearchService, get_web_search_service

__all__ = [
    "WebSearchRegistry",
    "WebSearchService",
    "get_web_search_registry",
    "get_web_search_service",
]
