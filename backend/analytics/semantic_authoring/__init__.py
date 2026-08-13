"""Agent-facing authoring support for Markdown semantic definitions.

Published Markdown remains the only durable semantic definition.  The models
in this package are drafts, validation results, and publication control data.
"""

from .contracts import (
    AuthoringBrief,
    FrontmatterEffect,
    inspect_frontmatter_contract,
    repair_technical_frontmatter,
)
from .documents import MarkdownDocument, parse_markdown_document, render_markdown_document
from .validation import validate_markdown_definition

__all__ = [
    "AuthoringBrief",
    "FrontmatterEffect",
    "MarkdownDocument",
    "inspect_frontmatter_contract",
    "parse_markdown_document",
    "render_markdown_document",
    "repair_technical_frontmatter",
    "validate_markdown_definition",
]
