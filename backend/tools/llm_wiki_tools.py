"""Schema-bound PuddingClaw Agent tools for the LLM Wiki protocol."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from db import get_sessionmaker
from graph.attachment_store import attachment_store
from graph.citations import encode_tool_result
from knowledge.import_jobs import create_llm_wiki_ingest_job as enqueue_llm_wiki_ingest_job
from knowledge.import_jobs import job_to_dict
from knowledge.llm_wiki import LlmWikiError, LlmWikiService
from knowledge.service import KnowledgeService, KnowledgeServiceError

_INTAKE_SECRET = secrets.token_bytes(32)


def _intake_id(*, session_id: str, query_id: str, raw_paths: list[str]) -> str:
    payload = json.dumps(
        {
            "session_id": session_id,
            "query_id": query_id,
            "raw_paths": raw_paths,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_INTAKE_SECRET, payload, hashlib.sha256).hexdigest()


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


class WikiConversationDocumentsInput(BaseModel):
    document_ids: list[str] = Field(
        default_factory=list,
        max_length=32,
        description=(
            "Optional exact ids from the catalog. Empty lists metadata only; selected ids return their full "
            "server-owned content so the Agent can synthesize the corrected Raw Markdown."
        ),
    )


class WikiCreateRawInput(BaseModel):
    source: Literal["current_message", "conversation", "attachments", "knowledge_file"] = Field(
        description=(
            "Authoritative input to snapshot. For conversation, first inspect llm_wiki_conversation_documents and pass "
            "the exact Agent-selected conversation_document_ids; no implicit recent-message range is allowed."
        )
    )
    title: str = Field(default="", max_length=200)
    conversation_document_ids: list[str] = Field(
        default_factory=list,
        max_length=32,
        description=(
            "For source=conversation, the exact document_id values selected by the Agent from "
            "llm_wiki_conversation_documents. The server never selects a recent-message window automatically."
        ),
    )
    raw_markdown: str = Field(
        default="",
        max_length=200_000,
        description=(
            "For source=conversation, the final clean Markdown synthesized by the main Agent from the selected "
            "documents plus the user's current corrections. The server snapshots these exact bytes and never "
            "concatenates Session history."
        ),
    )
    attachment_ids: list[str] = Field(
        default_factory=list,
        description="For attachments, selected current-turn Markdown attachment ids. Empty means every current Markdown attachment.",
    )
    virtual_path: str = Field(
        default="",
        description="For knowledge_file, the exact /knowledge/... Markdown path.",
    )


class WikiStartIngestInput(BaseModel):
    raw_paths: list[str] = Field(min_length=1, description="Exact snapshot_path values returned by llm_wiki_create_raw.")
    intake_id: str = Field(
        min_length=64,
        max_length=64,
        description="Opaque current-turn intake id returned by llm_wiki_create_raw.",
    )
    import_gbrain: bool = Field(
        default=False,
        description=(
            "Whether to continue importing the validated Wiki into gbrain after publish and Lint. "
            "Keep false when the user only asks to compile or organize content as Wiki; set true only when the user "
            "explicitly asks to enter/import/sync gbrain."
        ),
    )


class WikiPageRetirement(BaseModel):
    slug: str = Field(description="Exact obsolete Wiki slug relative to the wiki/ root.")
    replacement: str = Field(description="Exact existing replacement Wiki slug relative to the wiki/ root.")


class WikiRetirePagesInput(BaseModel):
    retirements: list[WikiPageRetirement] = Field(
        min_length=1,
        max_length=20,
        description="Explicit obsolete-to-replacement page mappings authorized by the user.",
    )
    summary: str = Field(default="退役重复或过期 Wiki 页面", min_length=1, max_length=500)
    sync_gbrain: bool = Field(
        default=False,
        description="Also soft-delete the obsolete slugs from configured gbrain. Enable only when the user explicitly requests it.",
    )


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
    allow_ingest: bool = Field(default=True, exclude=True, repr=False)

    def _run(self, operation: str, raw_paths: list[str] | None = None) -> str:
        try:
            if operation == "ingest" and not self.allow_ingest:
                raise LlmWikiError(
                    "聊天 Agent 不直接读取 Ingest Raw；请使用 llm_wiki_create_raw 和 llm_wiki_start_ingest 投递后台任务"
                )
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
        "Read-only deterministic query over published Wiki pages. It reads index.md and relevant Wiki pages only, "
        "never raw/. Treat the published Markdown LLM Wiki as the primary and complete internal knowledge source. "
        "Use filtered gbrain tools only after this query when entity relations, graph traversal, or structured filtering "
        "adds value; gbrain must never replace or skip the Markdown Wiki query."
    )
    args_schema: type[BaseModel] = WikiQueryInput
    risk_level: str = "safe"

    def _run(self, question: str, limit: int = 6) -> str:
        try:
            payload = self.service.query(question, limit=limit)
            pages = {
                str(page.get("slug") or ""): page
                for page in payload.get("pages", [])
                if isinstance(page, dict)
            }
            sources = []
            for reference in payload.get("references", []):
                if not isinstance(reference, dict):
                    continue
                slug = str(reference.get("slug") or "")
                page = pages.get(slug, {})
                content = str(reference.get("excerpt") or page.get("content") or "")
                sources.append(
                    {
                        "title": str(reference.get("title") or slug),
                        "uri": str(reference.get("uri") or ""),
                        "document_id": f"llm-wiki:{slug}",
                        "chunk_id": str(reference.get("chunk_id") or slug),
                        "source_type": "llm_wiki",
                        "quote": content[:1200],
                        "score": reference.get("score"),
                        "metadata": {
                            "wiki_slug": slug,
                            "page_type": reference.get("type"),
                            "raw_sources": reference.get("sources", []),
                            "virtual_path": reference.get("uri"),
                            "matched_by": reference.get("matched_by", []),
                            "chunk_title": reference.get("chunk_title"),
                            "retrieval_mode": payload.get("retrieval", {}).get("mode"),
                        },
                    }
                )
            return encode_tool_result(self.encode(payload), sources)
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


class LlmWikiConversationDocumentsTool(_WikiTool):
    name: str = "llm_wiki_conversation_documents"
    description: str = (
        "List the current Session's selectable conversation documents. Each completed user/assistant exchange and the "
        "current user instruction has a stable document_id plus a short preview. Call with no ids to inspect the catalog; "
        "then call with selected ids to read their exact content. The main Agent uses those sources and the user's current "
        "corrections to synthesize final Raw Markdown."
    )
    args_schema: type[BaseModel] = WikiConversationDocumentsInput
    risk_level: str = "safe"
    current_conversation_documents: list[dict[str, Any]] = Field(
        default_factory=list,
        exclude=True,
        repr=False,
    )

    def _run(self, document_ids: list[str] | None = None) -> str:
        requested = list(
            dict.fromkeys(str(item).strip() for item in document_ids or [] if str(item).strip())
        )
        available = {
            str(item.get("document_id") or ""): item
            for item in self.current_conversation_documents
            if isinstance(item, dict) and str(item.get("document_id") or "")
        }
        unknown = [item for item in requested if item not in available]
        if unknown:
            return self.error(LlmWikiError(f"会话文档不属于当前 Session：{', '.join(unknown)}"))
        selected = [available[item] for item in requested] if requested else list(available.values())
        documents = []
        for item in selected:
            if not isinstance(item, dict):
                continue
            document = {
                "document_id": str(item.get("document_id") or ""),
                "kind": str(item.get("kind") or "exchange"),
                "title": str(item.get("title") or ""),
                "preview": str(item.get("preview") or ""),
                "character_count": int(item.get("character_count") or 0),
            }
            if requested:
                document["content"] = str(item.get("content") or "")
            documents.append(document)
        return self.encode({"ok": True, "documents": documents})


class LlmWikiCreateRawTool(_WikiTool):
    name: str = "llm_wiki_create_raw"
    description: str = (
        "Create immutable LLM Wiki Raw snapshots from the exact current chat message, explicitly selected conversation "
        "documents, "
        "current Markdown attachments, or one existing /knowledge/ Markdown file. Use source=conversation when the user "
        "refers to earlier Session material: inspect and read the selected documents, then synthesize a clean final "
        "raw_markdown that incorporates the user's current corrections. Pass both the selected document ids and that "
        "Markdown. The server snapshots the Agent-authored Markdown exactly and never concatenates Session history. "
        "The title labels provenance only; it does not constrain page type, slug, title, or page count. After success, "
        "pass returned raw_paths to llm_wiki_start_ingest."
    )
    args_schema: type[BaseModel] = WikiCreateRawInput
    risk_level: str = "moderate"
    session_id: str = Field(default="", exclude=True, repr=False)
    query_id: str = Field(default="", exclude=True, repr=False)
    current_message: str = Field(default="", exclude=True, repr=False)
    current_conversation_documents: list[dict[str, Any]] = Field(
        default_factory=list,
        exclude=True,
        repr=False,
    )
    current_attachments: list[dict[str, Any]] = Field(default_factory=list, exclude=True, repr=False)

    def _run(self, **kwargs: Any) -> str:
        return self.error(RuntimeError("llm_wiki_create_raw must run asynchronously"))

    async def _arun(
        self,
        source: str,
        title: str = "",
        conversation_document_ids: list[str] | None = None,
        raw_markdown: str = "",
        attachment_ids: list[str] | None = None,
        virtual_path: str = "",
    ) -> str:
        try:
            records: list[dict[str, Any]] = []
            clean_title = str(title or "").strip()
            if source == "current_message":
                content = self.current_message
                if not content.strip():
                    raise LlmWikiError("当前消息没有可写入 Raw 的文本内容")
                asset_id = self.query_id or hashlib.sha256(content.encode("utf-8")).hexdigest()
                records.append(
                    await asyncio.to_thread(
                        self.service.snapshot_raw,
                        source_id=f"chat:{self.session_id or 'unknown'}",
                        asset_id=asset_id,
                        title=clean_title or "聊天文本",
                        content=content,
                        source_path=f"chat://{self.session_id or 'unknown'}/{asset_id}",
                    )
                )
            elif source == "conversation":
                available = {
                    str(item.get("document_id") or ""): item
                    for item in self.current_conversation_documents
                    if isinstance(item, dict) and str(item.get("document_id") or "")
                }
                selected_ids = list(
                    dict.fromkeys(
                        str(item).strip()
                        for item in conversation_document_ids or []
                        if str(item).strip()
                    )
                )
                if not selected_ids:
                    raise LlmWikiError(
                        "source=conversation 必须显式传 conversation_document_ids；"
                        "请先调用 llm_wiki_conversation_documents，由 Agent 选择相关会话文档"
                    )
                unauthorized = [item for item in selected_ids if item not in available]
                if unauthorized:
                    raise LlmWikiError(f"会话文档不属于当前 Session：{', '.join(unauthorized)}")
                content = str(raw_markdown or "").strip()
                if not content:
                    raise LlmWikiError(
                        "source=conversation 必须传入 Agent 根据所选文档和用户修正实时生成的 raw_markdown"
                    )
                asset_id = self.query_id or hashlib.sha256(content.encode("utf-8")).hexdigest()
                provenance_digest = hashlib.sha256(
                    json.dumps(selected_ids, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                ).hexdigest()[:16]
                records.append(
                    await asyncio.to_thread(
                        self.service.snapshot_raw,
                        source_id=f"conversation-synthesis:{self.session_id or 'unknown'}",
                        asset_id=asset_id,
                        title=clean_title or "会话整理材料",
                        content=content,
                        source_path=(
                            f"conversation-synthesis://{self.session_id or 'unknown'}/{asset_id}"
                            f"?documents_sha256={provenance_digest}"
                        ),
                    )
                )
            elif source == "attachments":
                allowed = {
                    str(item.get("id") or "")
                    for item in self.current_attachments
                    if isinstance(item, dict) and str(item.get("id") or "")
                }
                requested = list(dict.fromkeys(str(item).strip() for item in attachment_ids or [] if str(item).strip()))
                stored_by_id = {
                    attachment_id: attachment_store.get(self.session_id, attachment_id)
                    for attachment_id in sorted(allowed)
                }
                selected = requested or [
                    attachment_id
                    for attachment_id, item in stored_by_id.items()
                    if item
                    and (
                        Path(str(item.get("path") or "")).suffix.lower() in {".md", ".markdown"}
                        or str(item.get("mime_type") or "") in {"text/markdown", "text/x-markdown"}
                    )
                ]
                if not selected:
                    raise LlmWikiError("当前消息没有 Markdown 附件")
                unauthorized = [item for item in selected if item not in allowed]
                if unauthorized:
                    raise LlmWikiError(f"附件不属于当前消息：{', '.join(unauthorized)}")
                for attachment_id in selected:
                    item = stored_by_id.get(attachment_id)
                    if not item:
                        raise LlmWikiError(f"附件不存在或不可读取：{attachment_id}")
                    path = Path(str(item.get("path") or ""))
                    name = str(item.get("name") or path.name or attachment_id)
                    mime_type = str(item.get("mime_type") or "")
                    if path.suffix.lower() not in {".md", ".markdown"} and mime_type not in {
                        "text/markdown",
                        "text/x-markdown",
                    }:
                        raise LlmWikiError(f"只有 Markdown 附件可以进入 Raw：{name}")
                    records.append(
                        await asyncio.to_thread(
                            self.service.snapshot_raw_file,
                            source_id=f"attachment:{self.session_id or 'unknown'}",
                            asset_id=attachment_id,
                            title=clean_title or Path(name).stem,
                            path=path,
                            source_path=f"attachment://{self.session_id}/{attachment_id}",
                        )
                    )
            elif source == "knowledge_file":
                knowledge = KnowledgeService(self.base_dir)
                path = knowledge.resolve_raw_file(virtual_path=virtual_path)
                canonical_virtual_path = (
                    f"/knowledge/{path.relative_to(knowledge.knowledge_dir.resolve()).as_posix()}"
                )
                try:
                    path.relative_to(self.service.root.resolve())
                except ValueError:
                    pass
                else:
                    raise LlmWikiError("LLM Wiki 工作目录中的文件不能再次加入 Raw")
                records.append(
                    await asyncio.to_thread(
                        self.service.snapshot_raw_file,
                        source_id="knowledge-file",
                        asset_id=canonical_virtual_path,
                        title=clean_title or path.stem,
                        path=path,
                        source_path=canonical_virtual_path,
                    )
                )
            else:
                raise LlmWikiError(f"不支持的 Raw 来源：{source}")
            return self.encode(
                {
                    "ok": True,
                    "raw_paths": [str(record["snapshot_path"]) for record in records],
                    "snapshots": records,
                    "conversation_document_ids": (
                        selected_ids if source == "conversation" else []
                    ),
                    "intake_id": _intake_id(
                        session_id=self.session_id,
                        query_id=self.query_id,
                        raw_paths=[str(record["snapshot_path"]) for record in records],
                    ),
                    "next_tool": "llm_wiki_start_ingest",
                }
            )
        except (LlmWikiError, KnowledgeServiceError, OSError) as exc:
            return self.error(exc)


class LlmWikiStartIngestTool(_WikiTool):
    name: str = "llm_wiki_start_ingest"
    description: str = (
        "Queue the existing durable LLM Wiki compiler pipeline for exact immutable raw_paths. Returns immediately with "
        "a task id. By default the dedicated compiler Agent stops after context → publish → lint (Wiki only). Set "
        "import_gbrain=true only when the user explicitly requests gbrain; that adds the validated gbrain PostgreSQL "
        "import as a second stage. The queue request does not prescribe or reveal final page types, slugs, titles, or "
        "page count; do not claim those outputs until the Compiler has completed."
    )
    args_schema: type[BaseModel] = WikiStartIngestInput
    risk_level: str = "moderate"
    session_id: str = Field(default="", exclude=True, repr=False)
    query_id: str = Field(default="", exclude=True, repr=False)

    def _run(self, **kwargs: Any) -> str:
        return self.error(RuntimeError("llm_wiki_start_ingest must run asynchronously"))

    async def _arun(self, raw_paths: list[str], intake_id: str, import_gbrain: bool = False) -> str:
        try:
            expected_intake_id = _intake_id(
                session_id=self.session_id,
                query_id=self.query_id,
                raw_paths=raw_paths,
            )
            if not self.session_id or not self.query_id or not hmac.compare_digest(intake_id, expected_intake_id):
                raise KnowledgeServiceError(
                    "intake_id 不属于当前消息；请先调用 llm_wiki_create_raw，并原样传递其 raw_paths 与 intake_id"
                )
            sessionmaker = get_sessionmaker()
            async with sessionmaker() as session:
                job = await enqueue_llm_wiki_ingest_job(
                    session,
                    base_dir=self.base_dir,
                    raw_paths=raw_paths,
                    import_gbrain=import_gbrain,
                )
            return self.encode(
                {
                    "ok": True,
                    "queued": job.status == "queued",
                    "status": job.status,
                    "job": job_to_dict(job),
                    "task_center_path": "/knowledge/imports",
                }
            )
        except (KnowledgeServiceError, LlmWikiError, OSError, SQLAlchemyError) as exc:
            return self.error(exc)


class LlmWikiRetirePagesTool(_WikiTool):
    name: str = "llm_wiki_retire_pages"
    description: str = (
        "Deterministically retire explicitly identified obsolete LLM Wiki pages in favor of existing replacement pages. "
        "It rewrites inbound Wiki links, rebuilds index.md, appends log.md, archives retired Markdown, writes an audit "
        "receipt, and runs Lint atomically. This tool does not invoke an LLM. Use only after the user has explicitly "
        "identified the obsolete and replacement slugs. Set sync_gbrain=true only when the user also asks to remove the "
        "obsolete pages from gbrain; gbrain deletion is soft and recoverable for 72 hours. Treat an ok=true result, "
        "including already_retired=true, as authoritative and do not verify it with generic filesystem tools."
    )
    args_schema: type[BaseModel] = WikiRetirePagesInput
    risk_level: str = "moderate"

    def _run(
        self,
        retirements: list[WikiPageRetirement | dict[str, Any]],
        summary: str = "退役重复或过期 Wiki 页面",
        sync_gbrain: bool = False,
    ) -> str:
        normalized = [
            item.model_dump() if isinstance(item, WikiPageRetirement) else dict(item)
            for item in retirements
        ]
        try:
            wiki_result = self.service.retire_pages(retirements=normalized, summary=summary)
        except (LlmWikiError, OSError) as exc:
            return self.error(exc)
        gbrain_result: dict[str, Any] | None = None
        if sync_gbrain:
            try:
                gbrain_result = self.service.retire_gbrain_pages([str(item["slug"]) for item in normalized])
            except (LlmWikiError, OSError) as exc:
                return self.encode(
                    {
                        "ok": False,
                        "error": f"Wiki 页面已退役，但 gbrain 同步失败：{exc}",
                        "wiki": wiki_result,
                        "gbrain": None,
                        "retry_safe": True,
                    }
                )
            if not gbrain_result.get("ok"):
                return self.encode(
                    {
                        "ok": False,
                        "error": "Wiki 页面已退役，但 gbrain 软删除未全部成功",
                        "wiki": wiki_result,
                        "gbrain": gbrain_result,
                        "retry_safe": True,
                    }
                )
        return self.encode({"ok": True, "wiki": wiki_result, "gbrain": gbrain_result})


def create_llm_wiki_tools(base_dir: Path) -> list[BaseTool]:
    resolved = base_dir.resolve()
    return [
        LlmWikiContextTool(base_dir=resolved),
        LlmWikiPublishTool(base_dir=resolved),
        LlmWikiLintTool(base_dir=resolved),
        LlmWikiQueryTool(base_dir=resolved),
        LlmWikiCompileTool(base_dir=resolved),
        LlmWikiConversationDocumentsTool(base_dir=resolved),
        LlmWikiCreateRawTool(base_dir=resolved),
        LlmWikiStartIngestTool(base_dir=resolved),
        LlmWikiRetirePagesTool(base_dir=resolved),
    ]
