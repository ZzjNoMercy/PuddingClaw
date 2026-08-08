"""Built-in managed web-search adapters."""

from .deepseek import DeepSeekSearchAdapter
from .grok import GrokSearchAdapter
from .tavily import TavilySearchAdapter

__all__ = ["DeepSeekSearchAdapter", "GrokSearchAdapter", "TavilySearchAdapter"]
