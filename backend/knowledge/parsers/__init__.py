"""Document parser contracts and registry.

Parsers stop at portable Markdown plus local assets. Chunking, embedding,
LLM Wiki compilation and GBrain publication intentionally remain downstream.
"""

from knowledge.parsers.contracts import (
    DocumentParser,
    ParsedAsset,
    ParserCapabilities,
    ParseRequest,
    ParserError,
    ParseResult,
)
from knowledge.parsers.registry import DocumentParserRegistry, get_document_parser_registry

__all__ = [
    "DocumentParser",
    "DocumentParserRegistry",
    "ParseRequest",
    "ParseResult",
    "ParsedAsset",
    "ParserCapabilities",
    "ParserError",
    "get_document_parser_registry",
]
