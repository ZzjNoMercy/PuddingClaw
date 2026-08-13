"""Parse and render the Markdown files used as semantic definitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml


class MarkdownDocumentError(ValueError):
    """Raised when a semantic Markdown document cannot be parsed safely."""


@dataclass(frozen=True)
class MarkdownDocument:
    frontmatter: dict[str, Any]
    body: str


def parse_markdown_document(content: str) -> MarkdownDocument:
    """Parse one Markdown document without interpreting its business prose."""

    text = str(content or "")
    if not text.startswith("---"):
        return MarkdownDocument(frontmatter={}, body=text)
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return MarkdownDocument(frontmatter={}, body=text)
    closing = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if closing is None:
        raise MarkdownDocumentError("frontmatter is missing its closing delimiter")
    raw = "".join(lines[1:closing])
    try:
        parsed = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise MarkdownDocumentError(f"frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise MarkdownDocumentError("frontmatter must be a YAML mapping")
    body = "".join(lines[closing + 1 :]).lstrip("\n")
    return MarkdownDocument(frontmatter=dict(parsed), body=body)


def render_markdown_document(document: MarkdownDocument) -> str:
    """Render stable YAML frontmatter followed by the user-authored body."""

    frontmatter = yaml.safe_dump(
        document.frontmatter,
        allow_unicode=True,
        sort_keys=False,
    ).strip()
    body = document.body.rstrip() + "\n" if document.body else ""
    return f"---\n{frontmatter}\n---\n\n{body}"
