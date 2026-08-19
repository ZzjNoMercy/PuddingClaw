"""Durable Feishu Wiki discovery and incremental Docx synchronization."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import re
import shutil
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge.connectors.feishu import FeishuConnectorError, FeishuOpenApi
from knowledge.connectors.feishu_blocks import convert_feishu_blocks_to_markdown
from knowledge.indexer import refresh_local_knowledge_index
from knowledge.models import KnowledgeDocument, KnowledgeSourceConnection, KnowledgeSourceItem, KnowledgeSyncRun
from knowledge.queue_repository import claim_next, current_lease_owner, new_worker_id, require_current_lease
from knowledge.service import KnowledgeService, _slugify
from knowledge.sources import stable_source_item_id, upsert_source_item
from runtime_control import writes_allowed

MAX_DISCOVERED_NODES = 100_000


def _unix_datetime(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _safe_revision(document: dict[str, Any], node: dict[str, Any]) -> str:
    value = document.get("revision_id")
    if value in {None, ""}:
        value = node.get("obj_edit_time") or node.get("node_edit_time")
    return str(value or "")


def _source_url(source: KnowledgeSourceConnection, node: dict[str, Any]) -> str | None:
    if node.get("url"):
        return str(node["url"])
    domain = str((source.config_json or {}).get("tenant_domain") or "").strip().lower()
    token = str(node.get("node_token") or "")
    if domain and token and all(char.isalnum() or char in ".-" for char in domain):
        return f"https://{domain}/wiki/{token}"
    return None


class FeishuWikiSync:
    def __init__(self, *, base_dir: Path, api: FeishuOpenApi | None = None) -> None:
        self.base_dir = base_dir
        self.api = api or FeishuOpenApi()
        self.service = KnowledgeService(base_dir)

    async def run(
        self,
        session: AsyncSession,
        *,
        source: KnowledgeSourceConnection,
        run: KnowledgeSyncRun,
    ) -> KnowledgeSyncRun:
        if source.connector_key != "feishu_wiki":
            raise FeishuConnectorError("Sync run is not bound to a Feishu source.")
        config = dict(source.config_json or {})
        if run.mode == "reindex":
            return await self._run_local_reindex(session, source=source, run=run)
        space_id = str(config.get("space_id") or "").strip()
        if not space_id:
            raise FeishuConnectorError("请先选择飞书知识空间。")
        run.status = "running"
        run.current_step = "discovering"
        run.progress = 2
        run.started_at = run.started_at or datetime.now(timezone.utc)
        source.status = "syncing"
        await session.commit()

        nodes = await self._discover(
            session,
            source=source,
            space_id=space_id,
            root_node_token=str(config.get("root_node_token") or "").strip() or None,
        )
        # End the read transaction opened by discovery (credential lookups)
        # before writing run stats. Under SQLite WAL, upgrading a stale read
        # snapshot to a write fails immediately with SQLITE_BUSY_SNAPSHOT,
        # which busy_timeout does not cover. Commit (not rollback) so the
        # loaded ORM objects stay usable (rollback would expire them).
        await session.commit()
        stats = {"discovered": len(nodes), "changed": 0, "unchanged": 0, "failed": 0, "deleted": 0, "unsupported": 0}
        run.stats_json = stats
        run.current_step = "syncing_items"
        await session.commit()

        changed = False
        for index, (node, path) in enumerate(nodes, start=1):
            await session.refresh(run, attribute_names=["status"])
            if run.status == "cancelled":
                now = datetime.now(timezone.utc)
                run.current_step = "cancelled"
                run.finished_at = now
                run.lease_owner = None
                run.lease_expires_at = None
                run.heartbeat_at = None
                source.status = "ready"
                await session.commit()
                return run
            try:
                item_changed = await self._sync_node(
                    session,
                    source=source,
                    run=run,
                    node=node,
                    path=path,
                )
                if item_changed is None:
                    stats["unsupported"] += 1
                elif item_changed:
                    stats["changed"] += 1
                    changed = True
                else:
                    stats["unchanged"] += 1
            except Exception as exc:  # one document never aborts the entire source
                stats["failed"] += 1
                item = await self._item_for_node(session, source.id, str(node.get("node_token") or ""))
                if item is not None:
                    item.status = "error"
                    item.metadata_json = {
                        **(item.metadata_json or {}),
                        "last_error": f"{type(exc).__name__}: {exc}",
                    }
            run.progress = min(94, 5 + int(index / max(1, len(nodes)) * 89))
            run.stats_json = dict(stats)
            run.cursor_json = {"last_node_token": str(node.get("node_token") or ""), "processed": index}
            await self._lease_fence(session, run)
            await session.commit()

        if run.mode == "full_scan":
            deleted = await self._reconcile_deleted(session, source=source, run=run)
            stats["deleted"] = deleted
            changed = changed or deleted > 0

        run.current_step = "indexing"
        run.progress = 96
        await session.commit()
        vector_result: dict[str, Any] = {"refreshed": False, "reason": "no changes"}
        if changed and bool(config.get("publish_vector", True)):
            try:
                vector_result = await asyncio.to_thread(refresh_local_knowledge_index, self.base_dir)
            except Exception as exc:  # local documents remain usable even if Milvus is temporarily unavailable
                vector_result = {"refreshed": False, "error": f"{type(exc).__name__}: {exc}"}
                source.last_error_json = {"stage": "indexing", "message": str(exc)}

        now = datetime.now(timezone.utc)
        run.status = "succeeded_with_errors" if stats["failed"] or vector_result.get("error") else "succeeded"
        run.current_step = "completed"
        run.progress = 100
        run.stats_json = {**stats, "vector_index": vector_result}
        run.finished_at = now
        source.status = "ready" if stats["failed"] == 0 else "error"
        source.last_synced_at = now
        source.last_sync_run_id = run.id
        await self._lease_fence(session, run, terminal=True)
        run.lease_owner = None
        run.lease_expires_at = None
        run.heartbeat_at = None
        await session.commit()
        return run

    async def _discover(
        self,
        session: AsyncSession,
        *,
        source: KnowledgeSourceConnection,
        space_id: str,
        root_node_token: str | None,
    ) -> list[tuple[dict[str, Any], list[str]]]:
        discovered: list[tuple[dict[str, Any], list[str]]] = []
        queue: deque[tuple[str | None, list[str]]] = deque()
        seen: set[str] = set()
        if root_node_token:
            root = await self.api.get_node(session, source, node_token=root_node_token)
            root_title = str(root.get("title") or "根节点")
            discovered.append((root, [root_title]))
            seen.add(str(root.get("node_token") or root_node_token))
            if root.get("has_child"):
                queue.append((root_node_token, [root_title]))
        else:
            queue.append((None, []))

        while queue:
            parent_token, parent_path = queue.popleft()
            children = await self.api.list_nodes(
                session,
                source,
                space_id=space_id,
                parent_node_token=parent_token,
            )
            for node in children:
                token = str(node.get("node_token") or "")
                if not token or token in seen:
                    continue
                seen.add(token)
                path = [*parent_path, str(node.get("title") or "未命名")]
                discovered.append((node, path))
                if len(discovered) > MAX_DISCOVERED_NODES:
                    raise FeishuConnectorError("飞书节点数超过单个 Source 的安全上限，请选择更小的根节点。")
                if node.get("has_child"):
                    queue.append((token, path))
        return discovered

    async def _upsert_node_item(
        self,
        session: AsyncSession,
        *,
        source: KnowledgeSourceConnection,
        run: KnowledgeSyncRun,
        node: dict[str, Any],
        path: list[str],
        source_url: str | None,
        existing: KnowledgeSourceItem | None,
    ) -> KnowledgeSourceItem:
        item = await upsert_source_item(
            session,
            source=source,
            external_id=str(node.get("node_token") or ""),
            external_type=str(node.get("obj_type") or "").lower() or "wiki_node",
            title=str(node.get("title") or "未命名"),
            source_url=source_url,
            status=existing.status if existing is not None else "discovered",
            document_id=existing.document_id if existing is not None else None,
            content_sha256=existing.content_sha256 if existing is not None else None,
            revision=existing.revision if existing is not None else None,
            path=path,
            metadata={
                **(existing.metadata_json if existing is not None else {}),
                "space_id": str(node.get("space_id") or (source.config_json or {}).get("space_id") or ""),
                "obj_token": str(node.get("obj_token") or ""),
                "node_type": str(node.get("node_type") or ""),
            },
        )
        item.external_parent_id = str(node.get("parent_node_token") or "") or None
        item.remote_created_at = _unix_datetime(node.get("obj_create_time") or node.get("node_create_time"))
        item.remote_updated_at = _unix_datetime(node.get("obj_edit_time") or node.get("node_edit_time"))
        item.last_seen_run_id = run.id
        return item

    async def _sync_node(
        self,
        session: AsyncSession,
        *,
        source: KnowledgeSourceConnection,
        run: KnowledgeSyncRun,
        node: dict[str, Any],
        path: list[str],
    ) -> bool | None:
        node_token = str(node.get("node_token") or "")
        obj_token = str(node.get("obj_token") or "")
        obj_type = str(node.get("obj_type") or "").lower()
        source_url = _source_url(source, node)
        # Fetch phase first: no writes happen until every remote call below has
        # completed. Holding the SQLite write lock across block pagination and
        # asset downloads stalled every other writer past busy_timeout, which
        # surfaced as "database is locked" on the queue claim path.
        existing = await self._item_for_node(session, source.id, node_token)
        if obj_type != "docx" or not obj_token:
            item = await self._upsert_node_item(
                session,
                source=source,
                run=run,
                node=node,
                path=path,
                source_url=source_url,
                existing=existing,
            )
            item.status = "unsupported"
            await session.flush()
            return None

        document_meta = await self.api.get_docx_document(session, source, document_id=obj_token)
        revision = _safe_revision(document_meta, node)
        unchanged_remote = (
            run.mode == "incremental"
            and existing is not None
            and existing.revision == revision
            and existing.document_id
        )
        local_assets: list[dict[str, Any]] = []
        pdf_assets: list[dict[str, Any]] = []
        normalized_markdown = ""
        warnings: list[str] = []
        content_hash = ""
        if not unchanged_remote:
            blocks = await self.api.list_docx_blocks(
                session,
                source,
                document_id=obj_token,
                document_revision_id=int(revision) if revision.isdigit() else -1,
            )
            converted = convert_feishu_blocks_to_markdown(blocks)
            warnings = converted.warnings
            item_id = existing.id if existing is not None else stable_source_item_id(source.id, node_token)
            normalized_markdown, local_assets, pdf_assets = await self._materialize_assets(
                session,
                source=source,
                item_id=item_id,
                descriptors=converted.assets,
                markdown=converted.markdown,
                warnings=converted.warnings,
            )
            content_hash = hashlib.sha256(normalized_markdown.encode("utf-8")).hexdigest()

        # Drop the read snapshot held across the fetch phase so the write
        # phase starts a fresh transaction (upgrading a stale WAL snapshot to
        # a write fails immediately with SQLITE_BUSY_SNAPSHOT). Commit, not
        # rollback, keeps the loaded ORM objects unexpired and usable.
        await session.commit()

        # Write phase: pure database work, committed by the caller right after.
        item = await self._upsert_node_item(
            session,
            source=source,
            run=run,
            node=node,
            path=path,
            source_url=source_url,
            existing=existing,
        )
        metadata_title = str(document_meta.get("title") or node.get("title") or item.title)
        if unchanged_remote:
            item.status = "ready"
            item.revision = revision
            await self._mark_attachment_children_seen(session, source=source, parent=item, run=run)
            await self._refresh_document_source_metadata(
                session,
                item=item,
                title=metadata_title,
                path=path,
                source_url=source_url,
                revision=revision,
            )
            return False
        if run.mode != "reindex" and existing is not None and existing.content_sha256 == content_hash and existing.document_id:
            item.revision = revision
            item.status = "ready"
            await self._mark_attachment_children_seen(
                session,
                source=source,
                parent=item,
                run=run,
                media_tokens={str(asset.get("token") or "") for asset in local_assets},
            )
            await self._refresh_document_source_metadata(
                session,
                item=item,
                title=metadata_title,
                path=path,
                source_url=source_url,
                revision=revision,
            )
            return False
        title = str(document_meta.get("title") or node.get("title") or "未命名")
        frontmatter = {
            "title": title,
            "source": "feishu_wiki",
            "source_url": source_url or "",
            "space_id": str((source.config_json or {}).get("space_id") or ""),
            "node_token": node_token,
            "obj_token": obj_token,
            "revision_id": revision,
            "wiki_path": path,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
        markdown = f"---\n{yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()}\n---\n\n{normalized_markdown}"
        content = markdown.encode("utf-8")
        document, _ingest = await self.service.ingest_markdown_upload(
            session,
            filename=f"{title}.md",
            content=content,
            title=title,
            knowledge_base_id=source.knowledge_base_id,
            publish_targets=["local_markdown", "vector", "feishu_wiki"],
            publish_vector_now=False,
            source_connection_id=source.id,
            source_item_id=item.id,
            origin_url=source_url,
            source_revision=revision,
        )
        document.source_type = "feishu_docx"
        document.doc_metadata = {
            **(document.doc_metadata or {}),
            "feishu": {
                "space_id": frontmatter["space_id"],
                "node_token": node_token,
                "obj_token": obj_token,
                "revision_id": revision,
                "wiki_path": path,
                "assets": local_assets,
                "normalization_warnings": warnings,
            },
        }
        item.document_id = document.id
        item.content_sha256 = content_hash
        item.revision = revision
        item.status = "ready"
        await session.flush()
        for pdf_asset in pdf_assets:
            # MinerU PDF parsing outlives busy_timeout, so each attachment is
            # committed before its parse starts instead of holding the write
            # lock across it (see _ingest_pdf_attachment).
            await session.commit()
            await self._ingest_pdf_attachment(
                session,
                source=source,
                run=run,
                parent_item=item,
                revision=revision,
                path=path,
                asset=pdf_asset,
            )
        return True

    async def _run_local_reindex(
        self,
        session: AsyncSession,
        *,
        source: KnowledgeSourceConnection,
        run: KnowledgeSyncRun,
    ) -> KnowledgeSyncRun:
        """Rebuild the local index without touching Feishu OpenAPI."""

        run.status = "running"
        run.current_step = "indexing"
        run.progress = 10
        run.started_at = run.started_at or datetime.now(timezone.utc)
        source.status = "syncing"
        await session.commit()
        items = (
            await session.execute(
                select(KnowledgeSourceItem).where(
                    KnowledgeSourceItem.source_connection_id == source.id,
                    KnowledgeSourceItem.status == "ready",
                )
            )
        ).scalars().all()
        stats = {
            "discovered": len(items),
            "changed": 0,
            "unchanged": len(items),
            "failed": 0,
            "deleted": 0,
            "unsupported": 0,
        }
        # End the read transaction before vector indexing; the write below
        # must not upgrade a snapshot held across the (slow) index refresh.
        # Commit keeps the loaded ORM objects unexpired and usable.
        await session.commit()
        try:
            vector_result = await asyncio.to_thread(refresh_local_knowledge_index, self.base_dir)
        except Exception as exc:
            vector_result = {"refreshed": False, "error": f"{type(exc).__name__}: {exc}"}
            stats["failed"] = 1
            source.last_error_json = {"stage": "indexing", "message": str(exc)}
        now = datetime.now(timezone.utc)
        run.status = "succeeded_with_errors" if stats["failed"] else "succeeded"
        run.current_step = "completed"
        run.progress = 100
        run.stats_json = {**stats, "vector_index": vector_result}
        run.finished_at = now
        source.status = "error" if stats["failed"] else "ready"
        source.last_synced_at = now
        source.last_sync_run_id = run.id
        await self._lease_fence(session, run, terminal=True)
        run.lease_owner = None
        run.lease_expires_at = None
        run.heartbeat_at = None
        await session.commit()
        return run

    @staticmethod
    async def _mark_attachment_children_seen(
        session: AsyncSession,
        *,
        source: KnowledgeSourceConnection,
        parent: KnowledgeSourceItem,
        run: KnowledgeSyncRun,
        media_tokens: set[str] | None = None,
    ) -> None:
        filters = [
            KnowledgeSourceItem.source_connection_id == source.id,
            KnowledgeSourceItem.external_parent_id == parent.external_id,
            KnowledgeSourceItem.external_type == "pdf_attachment",
            KnowledgeSourceItem.status != "deleted",
        ]
        if media_tokens is not None:
            external_ids = {f"{parent.external_id}:media:{token}" for token in media_tokens if token}
            if not external_ids:
                return
            filters.append(KnowledgeSourceItem.external_id.in_(external_ids))
        children = (await session.execute(select(KnowledgeSourceItem).where(*filters))).scalars()
        for child in children:
            child.last_seen_run_id = run.id

    async def _refresh_document_source_metadata(
        self,
        session: AsyncSession,
        *,
        item: KnowledgeSourceItem,
        title: str,
        path: list[str],
        source_url: str | None,
        revision: str,
    ) -> None:
        if not item.document_id:
            return
        document = await session.get(KnowledgeDocument, item.document_id)
        if document is None:
            return
        feishu_metadata = dict((document.doc_metadata or {}).get("feishu") or {})
        document.title = title.strip() or document.title
        document.origin_url = source_url
        document.source_revision = revision
        document.doc_metadata = {
            **(document.doc_metadata or {}),
            "feishu": {**feishu_metadata, "wiki_path": list(path), "revision_id": revision},
        }

    async def _materialize_assets(
        self,
        session: AsyncSession,
        *,
        source: KnowledgeSourceConnection,
        item_id: str,
        descriptors: list[dict[str, str]],
        markdown: str,
        warnings: list[str],
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        if not descriptors:
            return markdown, [], []
        try:
            payloads = await self.api.download_media_assets(
                session,
                source,
                file_tokens=[descriptor.get("token", "") for descriptor in descriptors],
            )
        except FeishuConnectorError as exc:
            warnings.append(f"素材下载失败：{exc}")
            raise
        target_dir = self.service.knowledge_dir / "assets" / "feishu" / source.id / item_id
        target_dir.mkdir(parents=True, exist_ok=True)
        local_assets: list[dict[str, Any]] = []
        pdf_assets: list[dict[str, Any]] = []
        rewritten = markdown
        for descriptor in descriptors:
            token = descriptor.get("token", "")
            if token not in payloads:
                raise FeishuConnectorError("飞书素材响应不完整，已保留上一版文档并等待重试。")
            content, content_type, disposition = payloads[token]
            original_name = descriptor.get("filename") or f"asset-{descriptor.get('block_id', '')}"
            disposition_match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", disposition, flags=re.IGNORECASE)
            if disposition_match:
                original_name = disposition_match.group(1).strip()
            filename = _slugify(original_name)
            suffix = Path(filename).suffix.lower()
            if suffix in {"", ".bin"}:
                guessed = mimetypes.guess_extension(content_type) or ""
                filename = f"{Path(filename).stem}{guessed}"
            target = target_dir / filename
            if target.exists() and hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(content).digest():
                filename = f"{Path(filename).stem}-{hashlib.sha256(content).hexdigest()[:8]}{Path(filename).suffix}"
                target = target_dir / filename
            target.write_bytes(content)
            virtual_path = f"/knowledge/assets/feishu/{source.id}/{item_id}/{filename}"
            rewritten = rewritten.replace(
                f"./assets/{quote(descriptor.get('filename') or '')}",
                virtual_path,
            )
            record = {
                "token": token,
                "type": descriptor.get("type") or "file",
                "filename": filename,
                "path": str(target),
                "virtual_path": virtual_path,
                "content_type": content_type,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            local_assets.append(record)
            if Path(filename).suffix.lower() == ".pdf" or content_type == "application/pdf":
                pdf_assets.append({**record, "content": content})
        return rewritten, local_assets, pdf_assets

    async def _ingest_pdf_attachment(
        self,
        session: AsyncSession,
        *,
        source: KnowledgeSourceConnection,
        run: KnowledgeSyncRun,
        parent_item: KnowledgeSourceItem,
        revision: str,
        path: list[str],
        asset: dict[str, Any],
    ) -> None:
        token = str(asset.get("token") or "")
        filename = str(asset.get("filename") or "attachment.pdf")
        attachment_item = await upsert_source_item(
            session,
            source=source,
            external_id=f"{parent_item.external_id}:media:{token}",
            external_type="pdf_attachment",
            title=filename,
            status="processing",
            revision=revision,
            path=[*path, filename],
            metadata={"parent_source_item_id": parent_item.id, "media_token": token},
        )
        attachment_item.external_parent_id = parent_item.external_id
        attachment_item.last_seen_run_id = run.id
        # Commit the attachment row before MinerU parsing: the parse can take
        # far longer than busy_timeout and must not hold the SQLite write lock.
        await session.commit()
        try:
            document, _ingest = await self.service.ingest_pdf_upload(
                session,
                filename=filename,
                content=bytes(asset["content"]),
                title=Path(filename).stem,
                knowledge_base_id=source.knowledge_base_id,
                publish_targets=["local_markdown", "vector", "feishu_attachment"],
                publish_vector_now=False,
                source_connection_id=source.id,
                source_item_id=attachment_item.id,
                source_revision=revision,
            )
            document.source_type = "feishu_pdf_attachment"
            attachment_item.document_id = document.id
            attachment_item.content_sha256 = str(asset.get("sha256") or "")
            attachment_item.status = "ready"
        except Exception as exc:
            attachment_item.status = "error"
            attachment_item.metadata_json = {
                **(attachment_item.metadata_json or {}),
                "last_error": f"{type(exc).__name__}: {exc}",
            }
            await session.flush()
            raise FeishuConnectorError(f"PDF 附件 {filename} 处理失败，已保留错误状态。") from exc
        await session.flush()

    async def _reconcile_deleted(
        self,
        session: AsyncSession,
        *,
        source: KnowledgeSourceConnection,
        run: KnowledgeSyncRun,
    ) -> int:
        result = await session.execute(
            select(KnowledgeSourceItem).where(
                KnowledgeSourceItem.source_connection_id == source.id,
                KnowledgeSourceItem.status != "deleted",
                or_(
                    KnowledgeSourceItem.last_seen_run_id.is_(None),
                    KnowledgeSourceItem.last_seen_run_id != run.id,
                ),
            )
        )
        deleted = 0
        for item in result.scalars():
            item.status = "deleted"
            if item.document_id:
                document = await session.get(KnowledgeDocument, item.document_id)
                if document is not None:
                    document.status = "deleted"
                    path = Path(document.storage_path)
                    if path.is_file():
                        tombstone = self.service.knowledge_dir / ".tombstones" / source.id / f"{document.id}.md.deleted"
                        tombstone.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(path), str(tombstone))
                        document.doc_metadata = {
                            **(document.doc_metadata or {}),
                            "tombstone_path": str(tombstone),
                        }
            deleted += 1
        await session.flush()
        return deleted

    @staticmethod
    async def _item_for_node(
        session: AsyncSession, source_id: str, node_token: str
    ) -> KnowledgeSourceItem | None:
        if not node_token:
            return None
        return (
            await session.execute(
                select(KnowledgeSourceItem).where(
                    KnowledgeSourceItem.source_connection_id == source_id,
                    KnowledgeSourceItem.external_id == node_token,
                )
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _lease_fence(session: AsyncSession, run: KnowledgeSyncRun, *, terminal: bool = False) -> None:
        owner = current_lease_owner()
        if not owner:
            return
        # Terminal code clears the lease only after this fencing UPDATE.
        if terminal:
            run.lease_owner = owner
        await require_current_lease(session, KnowledgeSyncRun, run.id)


async def process_feishu_sync_run(
    session: AsyncSession,
    *,
    base_dir: Path,
    run: KnowledgeSyncRun,
    api: FeishuOpenApi | None = None,
) -> KnowledgeSyncRun:
    source = await session.get(KnowledgeSourceConnection, run.source_connection_id)
    if source is None:
        raise FeishuConnectorError("Knowledge source no longer exists.")
    return await FeishuWikiSync(base_dir=base_dir, api=api).run(session, source=source, run=run)


async def claim_next_feishu_sync_run(
    session: AsyncSession,
    *,
    worker_id: str | None = None,
    lease_seconds: int | None = None,
) -> KnowledgeSyncRun | None:
    if not await writes_allowed(session):
        return None
    await session.rollback()
    run = await claim_next(
        session,
        KnowledgeSyncRun,
        worker_id=worker_id or new_worker_id("manual-feishu-sync"),
        lease_seconds=lease_seconds,
        extra_sets={"current_step": "starting", "progress": 1, "finished_at": None},
    )
    if run is None:
        return None
    await session.commit()
    await session.refresh(run)
    return run


async def mark_feishu_sync_failed(
    session: AsyncSession,
    *,
    run: KnowledgeSyncRun,
    error: Exception,
) -> None:
    owner = current_lease_owner()
    if owner:
        await require_current_lease(session, KnowledgeSyncRun, run.id)
    now = datetime.now(timezone.utc)
    run.status = "failed"
    run.current_step = "failed"
    run.error_json = {"type": type(error).__name__, "message": str(error)}
    run.finished_at = now
    run.lease_owner = None
    run.lease_expires_at = None
    run.heartbeat_at = None
    source = await session.get(KnowledgeSourceConnection, run.source_connection_id)
    if source is not None:
        source.status = "error"
        source.last_error_json = dict(run.error_json)
    await session.commit()
