"""Schema-bound PuddingClaw Agent tools for the LLM Wiki protocol."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from knowledge.llm_wiki import LlmWikiError, LlmWikiService


class WikiContextInput(BaseModel):
    operation: Literal["ingest", "query", "lint"] = Field(description="The AGENTS.md operation contract to load.")
    raw_paths: list[str] = Field(
        default_factory=list,
        description="For ingest, the exact immutable raw snapshot paths selected for this compile.",
    )


class WikiPageDraft(BaseModel):
    slug: str = Field(description="Lowercase hyphen Wiki slug without .md")
    content: str = Field(description="Complete Markdown page including YAML frontmatter")


class WikiPublishInput(BaseModel):
    pages: list[WikiPageDraft] = Field(min_length=1)
    expected_bundle_hash: str = Field(min_length=64, max_length=64)
    summary: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    raw_paths: list[str] = Field(default_factory=list)


class WikiQueryInput(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=6, ge=1, le=20)


class NoInput(BaseModel):
    pass


class _WikiTool(BaseTool):
    base_dir: Path

    @property
    def service(self) -> LlmWikiService:
        return LlmWikiService(self.base_dir)

    @staticmethod
    def encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)

    @staticmethod
    def error(exc: Exception) -> str:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


class LlmWikiContextTool(_WikiTool):
    name: str = "llm_wiki_context"
    description: str = (
        "Load the active Schema Bundle, AGENTS.md operation contract, Wiki index, and only the raw snapshots "
        "authorized for an LLM Wiki Ingest/Query/Lint operation. Always call this before the other LLM Wiki tools."
    )
    args_schema: type[BaseModel] = WikiContextInput
    risk_level: str = "safe"

    def _run(self, operation: str, raw_paths: list[str] | None = None) -> str:
        try:
            return self.encode(self.service.operation_context(operation, raw_paths=raw_paths or []))
        except (LlmWikiError, OSError) as exc:
            return self.error(exc)


class LlmWikiPublishTool(_WikiTool):
    name: str = "llm_wiki_publish"
    description: str = (
        "Submit complete Agent-generated Wiki pages to the deterministic publisher. The publisher verifies the "
        "Schema Bundle hash, raw immutability, frontmatter, Wiki links, index coverage, and append-only log before commit."
    )
    args_schema: type[BaseModel] = WikiPublishInput
    risk_level: str = "moderate"

    def _run(self, **kwargs: Any) -> str:
        try:
            return self.encode(
                self.service.publish(
                    pages=[
                        item.model_dump() if isinstance(item, WikiPageDraft) else dict(item)
                        for item in kwargs.get("pages", [])
                    ],
                    expected_bundle_hash=str(kwargs.get("expected_bundle_hash") or ""),
                    summary=str(kwargs.get("summary") or ""),
                    model=str(kwargs.get("model") or ""),
                    raw_paths=[str(item) for item in kwargs.get("raw_paths", [])],
                )
            )
        except (LlmWikiError, OSError) as exc:
            return self.error(exc)


class LlmWikiLintTool(_WikiTool):
    name: str = "llm_wiki_lint"
    description: str = "Read-only Lint for Schema drift, bad frontmatter, broken links, index omissions, or raw hash drift."
    args_schema: type[BaseModel] = NoInput
    risk_level: str = "safe"

    def _run(self) -> str:
        try:
            return self.encode(self.service.lint())
        except (LlmWikiError, OSError) as exc:
            return self.error(exc)


class LlmWikiQueryTool(_WikiTool):
    name: str = "llm_wiki_query"
    description: str = (
        "Read-only deterministic fallback query over published Wiki pages. It reads index.md and relevant Wiki pages only, "
        "never raw/. Prefer the filtered gbrain query/get_page MCP tools when the compiled brain is online."
    )
    args_schema: type[BaseModel] = WikiQueryInput
    risk_level: str = "safe"

    def _run(self, question: str, limit: int = 6) -> str:
        try:
            return self.encode(self.service.query(question, limit=limit))
        except (LlmWikiError, OSError) as exc:
            return self.error(exc)


class LlmWikiCompileTool(_WikiTool):
    name: str = "llm_wiki_compile"
    description: str = (
        "Run deterministic Wiki Lint plus the real gbrain schema validate/schema lint/wiki lint gates. "
        "This Agent tool validates only; it cannot import or write the gbrain PostgreSQL database."
    )
    args_schema: type[BaseModel] = NoInput
    risk_level: str = "safe"

    def _run(self) -> str:
        try:
            return self.encode(self.service.compile_gbrain(import_pages=False))
        except (LlmWikiError, OSError) as exc:
            return self.error(exc)


def create_llm_wiki_tools(base_dir: Path) -> list[BaseTool]:
    resolved = base_dir.resolve()
    return [
        LlmWikiContextTool(base_dir=resolved),
        LlmWikiPublishTool(base_dir=resolved),
        LlmWikiLintTool(base_dir=resolved),
        LlmWikiQueryTool(base_dir=resolved),
        LlmWikiCompileTool(base_dir=resolved),
    ]
