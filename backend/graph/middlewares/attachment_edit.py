"""Capability-gated editing for immutable Session attachments."""

import hashlib
import mimetypes
import posixpath
import time
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from graph.attachment_store import attachment_store
from graph.session_manager import session_manager
from harness.models import ArtifactReference, ArtifactRole, ArtifactScope


MAX_EDITABLE_ATTACHMENT_BYTES = 100 * 1024 * 1024
LEASE_TTL_SECONDS = 6 * 60 * 60


class PrepareAttachmentEditInput(BaseModel):
    attachment_id: str = Field(
        description="Current-Session attachment id (att_xxx) that the user wants modified"
    )


class PublishAttachmentInput(BaseModel):
    lease_id: str
    output_path: str = Field(
        description="Exact /scratch/attachments/<lease_id>/... file produced from this lease"
    )
    output_name: str | None = Field(
        default=None,
        description="Optional user-visible filename; defaults to output_path basename",
    )
    mime_type: str | None = None


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _runtime_binding(runtime: ToolRuntime[Any, Any]) -> dict[str, Any]:
    context = runtime.context if isinstance(runtime.context, dict) else {}
    return {
        "session_id": str(context.get("session_id") or ""),
        "run_id": str(context.get("run_id") or ""),
        "query_id": str(context.get("query_id") or ""),
        "goal_id": str(context.get("goal_id") or ""),
        "goal_revision": context.get("goal_revision"),
    }


def _tool_error(name: str, runtime: ToolRuntime[Any, Any], content: str) -> ToolMessage:
    return ToolMessage(
        content=f"Error: {content}",
        name=name,
        tool_call_id=runtime.tool_call_id,
        status="error",
    )


def _binding_matches(lease: dict[str, Any], binding: dict[str, Any]) -> bool:
    return (
        str(lease.get("session_id") or "") == binding["session_id"]
        and str(lease.get("run_id") or "") == binding["run_id"]
        and str(lease.get("query_id") or "") == binding["query_id"]
        and str(lease.get("goal_id") or "") == binding["goal_id"]
        and lease.get("goal_revision") == binding["goal_revision"]
    )


def _attachment_bytes(
    session_id: str,
    attachment_id: str,
    *,
    max_bytes: int = MAX_EDITABLE_ATTACHMENT_BYTES,
) -> tuple[dict[str, Any] | None, bytes | None]:
    item = attachment_store.get(session_id, attachment_id)
    if item is None:
        return None, None
    try:
        path = Path(str(item["path"]))
        if path.stat().st_size > max_bytes:
            return item, None
        data = path.read_bytes()
        if len(data) > max_bytes:
            return item, None
        return item, data
    except (OSError, KeyError):
        return None, None


def _public_publish_artifact(
    *,
    item: dict[str, Any],
    binding: dict[str, Any],
    tool_call_id: str,
) -> dict[str, Any]:
    internal = attachment_store.get(binding["session_id"], str(item.get("id") or ""))
    if internal is None:
        raise RuntimeError("published attachment disappeared before receipt creation")
    path = Path(str(internal["path"]))
    stat = path.stat()
    receipt = ArtifactReference(
        artifact_id=f"attachment-artifact-{item['id']}",
        scope=ArtifactScope.ATTACHMENT,
        role=ArtifactRole.TARGET,
        path=f"attachment://{item['id']}",
        host_path=str(path),
        authorized=True,
        run_id=binding["run_id"],
        query_id=binding["query_id"],
        goal_id=binding["goal_id"] or None,
        goal_revision=binding["goal_revision"],
        backend_id="attachment_store",
        tool_call_id=tool_call_id,
        output_digest=str(item.get("sha256") or ""),
        content_sha256=str(item.get("sha256") or ""),
        size_bytes=int(item.get("size") or stat.st_size),
        mtime_ns=stat.st_mtime_ns,
    )
    return {
        "published_attachment": item,
        "artifact_reference": receipt.model_dump(mode="json"),
    }


class AttachmentEditMiddleware(AgentMiddleware[Any, Any, Any]):
    """Upgrade immutable attachment reads into an explicit editable lease."""

    def __init__(self, backend: Any) -> None:
        super().__init__()
        self.backend = backend

        def prepare_attachment_edit(
            attachment_id: str,
            runtime: ToolRuntime[Any, Any],
        ) -> ToolMessage:
            binding = _runtime_binding(runtime)
            if not binding["session_id"] or not binding["run_id"] or not binding["query_id"]:
                return _tool_error(
                    "prepare_attachment_edit",
                    runtime,
                    "attachment editing requires an active Session, Run, and query",
                )
            item, source = _attachment_bytes(binding["session_id"], attachment_id)
            if item is None:
                return _tool_error(
                    "prepare_attachment_edit",
                    runtime,
                    "attachment is absent or does not belong to the current Session",
                )
            if source is None:
                return _tool_error(
                    "prepare_attachment_edit",
                    runtime,
                    f"attachment is unreadable or exceeds the {MAX_EDITABLE_ATTACHMENT_BYTES} byte edit limit",
                )

            source_sha = _sha256(source)
            lease_seed = (
                f"{binding['session_id']}:{binding['run_id']}:"
                f"{runtime.tool_call_id}:{attachment_id}"
            )
            lease_id = "attachment-lease-" + hashlib.sha256(
                lease_seed.encode("utf-8")
            ).hexdigest()[:16]
            staged_dir = f"/scratch/attachments/{lease_id}"
            staged_path = f"{staged_dir}/{item['name']}"
            existing = session_manager.get_attachment_edit_lease(
                binding["session_id"], lease_id
            )
            if isinstance(existing, dict):
                replay_ok = (
                    existing.get("source_attachment_id") == attachment_id
                    and existing.get("source_sha256") == source_sha
                    and existing.get("staged_path") == staged_path
                    and existing.get("status") in {"staged", "published"}
                    and _binding_matches(existing, binding)
                )
                downloaded = self.backend.download_files([staged_path]) if replay_ok else []
                staged = downloaded[0] if len(downloaded) == 1 else None
                replay_ok = bool(
                    replay_ok
                    and staged is not None
                    and staged.error is None
                    and staged.content is not None
                    and (
                        existing.get("status") == "published"
                        or _sha256(staged.content) == source_sha
                    )
                )
                if not replay_ok:
                    return _tool_error(
                        "prepare_attachment_edit",
                        runtime,
                        "tool-call identity already owns a conflicting attachment lease",
                    )
                return ToolMessage(
                    content=(
                        f"AttachmentEditLease already {existing['status']}. "
                        f"lease_id={lease_id}; staged_path={staged_path}; "
                        f"source_sha256={source_sha}."
                    ),
                    name="prepare_attachment_edit",
                    tool_call_id=runtime.tool_call_id,
                    status="success",
                    artifact={"attachment_edit_lease": existing},
                )

            response = self.backend.upload_files([(staged_path, source)])
            if len(response) != 1 or response[0].error is not None:
                detail = response[0].error if response else "backend returned no upload result"
                return _tool_error(
                    "prepare_attachment_edit",
                    runtime,
                    f"unable to stage attachment in the current Run scratch: {detail}",
                )
            now = time.time()
            lease = {
                "lease_id": lease_id,
                **binding,
                "source_attachment_id": attachment_id,
                "source_name": item["name"],
                "source_mime_type": item.get("mime_type"),
                "source_sha256": source_sha,
                "staged_dir": staged_dir,
                "staged_path": staged_path,
                "status": "staged",
                "created_at": now,
                "expires_at": now + LEASE_TTL_SECONDS,
            }
            session_manager.upsert_attachment_edit_lease(binding["session_id"], lease)
            return ToolMessage(
                content=(
                    f"AttachmentEditLease created. lease_id={lease_id}; "
                    f"staged_path={staged_path}; source_sha256={source_sha}. "
                    "Modify and validate files only inside staged_dir, then call publish_attachment. "
                    "The original attachment remains immutable."
                ),
                name="prepare_attachment_edit",
                tool_call_id=runtime.tool_call_id,
                status="success",
                artifact={"attachment_edit_lease": lease},
            )

        def publish_attachment(
            lease_id: str,
            output_path: str,
            output_name: str | None,
            mime_type: str | None,
            runtime: ToolRuntime[Any, Any],
        ) -> ToolMessage:
            binding = _runtime_binding(runtime)
            if not binding["session_id"] or not binding["run_id"] or not binding["query_id"]:
                return _tool_error(
                    "publish_attachment",
                    runtime,
                    "attachment publishing requires an active Session, Run, and query",
                )
            lease = session_manager.get_attachment_edit_lease(binding["session_id"], lease_id)
            if not isinstance(lease, dict):
                return _tool_error(
                    "publish_attachment", runtime, f"unknown AttachmentEditLease {lease_id}"
                )
            if not _binding_matches(lease, binding):
                return _tool_error(
                    "publish_attachment",
                    runtime,
                    "AttachmentEditLease belongs to a different Run, query, or Goal revision",
                )
            if float(lease.get("expires_at") or 0) < time.time():
                return _tool_error(
                    "publish_attachment", runtime, "AttachmentEditLease expired; prepare it again"
                )

            if "\\" in output_path:
                return _tool_error("publish_attachment", runtime, "output_path must be a POSIX scratch path")
            normalized_path = posixpath.normpath(output_path)
            staged_dir = str(lease.get("staged_dir") or "")
            if (
                output_path != normalized_path
                or not normalized_path.startswith(staged_dir + "/")
                or normalized_path == staged_dir
            ):
                return _tool_error(
                    "publish_attachment",
                    runtime,
                    "output_path must be a normalized file inside this lease's staged_dir",
                )
            requested_name = output_name or posixpath.basename(normalized_path)
            safe_name = attachment_store._safe_name(requested_name)
            if safe_name != requested_name or not safe_name:
                return _tool_error(
                    "publish_attachment", runtime, "output_name must be a plain safe filename"
                )
            resolved_mime = (mime_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream").strip()
            if "\n" in resolved_mime or "\r" in resolved_mime:
                return _tool_error("publish_attachment", runtime, "invalid mime_type")

            if lease.get("status") == "published":
                if (
                    lease.get("published_output_path") != normalized_path
                    or lease.get("published_name") != safe_name
                ):
                    return _tool_error(
                        "publish_attachment",
                        runtime,
                        "published lease replay does not match the original output path/name",
                    )
                published = attachment_store.get(
                    binding["session_id"], str(lease.get("published_attachment_id") or "")
                )
                if published is None:
                    return _tool_error(
                        "publish_attachment", runtime, "published attachment receipt no longer resolves"
                    )
                try:
                    published_path = Path(str(published["path"]))
                    if published_path.stat().st_size > MAX_EDITABLE_ATTACHMENT_BYTES:
                        return _tool_error(
                            "publish_attachment", runtime, "published attachment exceeds its size limit"
                        )
                    data = published_path.read_bytes()
                except (KeyError, OSError):
                    return _tool_error(
                        "publish_attachment", runtime, "published attachment bytes are unavailable"
                    )
                if _sha256(data) != lease.get("published_sha256"):
                    return _tool_error(
                        "publish_attachment", runtime, "published attachment bytes no longer match its receipt"
                    )
                public = attachment_store.public_item(published)
                artifact = _public_publish_artifact(
                    item=public, binding=binding, tool_call_id=runtime.tool_call_id
                )
                return ToolMessage(
                    content=(
                        f"Attachment already published. attachment_id={public['id']}; "
                        f"name={public['name']}; sha256={public['sha256']}; "
                        f"download_url={public['download_url']}"
                    ),
                    name="publish_attachment",
                    tool_call_id=runtime.tool_call_id,
                    status="success",
                    artifact=artifact,
                )
            if lease.get("status") not in {"staged", "publishing"}:
                return _tool_error(
                    "publish_attachment",
                    runtime,
                    f"AttachmentEditLease is not publishable ({lease.get('status')})",
                )

            source_item, source = _attachment_bytes(
                binding["session_id"], str(lease.get("source_attachment_id") or "")
            )
            if source_item is None or source is None or _sha256(source) != lease.get("source_sha256"):
                return _tool_error(
                    "publish_attachment",
                    runtime,
                    "immutable source attachment changed or disappeared after staging",
                )

            try:
                listing = self.backend.ls(posixpath.dirname(normalized_path))
                infos = (listing.entries or []) if listing.error is None else []
            except Exception:
                infos = []
            output_info = next(
                (
                    item
                    for item in infos
                    if posixpath.normpath(str(item.get("path") or "")) == normalized_path
                    and not item.get("is_dir")
                ),
                None,
            )
            if output_info is None or not isinstance(output_info.get("size"), int):
                return _tool_error(
                    "publish_attachment",
                    runtime,
                    "unable to verify staged output size before download",
                )
            if int(output_info["size"]) > MAX_EDITABLE_ATTACHMENT_BYTES:
                return _tool_error(
                    "publish_attachment",
                    runtime,
                    f"output exceeds the {MAX_EDITABLE_ATTACHMENT_BYTES} byte publish limit",
                )
            response = self.backend.download_files([normalized_path])
            if len(response) != 1 or response[0].error is not None or response[0].content is None:
                detail = response[0].error if response else "backend returned no download result"
                return _tool_error(
                    "publish_attachment", runtime, f"unable to read staged output: {detail}"
                )
            output = response[0].content
            if len(output) > MAX_EDITABLE_ATTACHMENT_BYTES:
                return _tool_error(
                    "publish_attachment",
                    runtime,
                    f"output exceeds the {MAX_EDITABLE_ATTACHMENT_BYTES} byte publish limit",
                )

            try:
                claimed = session_manager.claim_attachment_publish(
                    binding["session_id"],
                    lease_id=lease_id,
                    tool_call_id=runtime.tool_call_id,
                    output_path=normalized_path,
                    output_name=safe_name,
                )
            except (FileNotFoundError, RuntimeError) as exc:
                return _tool_error("publish_attachment", runtime, str(exc))
            if claimed.get("status") == "published":
                return _tool_error(
                    "publish_attachment",
                    runtime,
                    "AttachmentEditLease completed concurrently; replay the published path/name",
                )

            try:
                public = attachment_store.save_bytes(
                    session_id=binding["session_id"],
                    filename=safe_name,
                    mime_type=resolved_mime,
                    data=output,
                    source="generated",
                    derived_from=str(lease.get("source_attachment_id") or ""),
                    created_by_run_id=binding["run_id"],
                    created_by_query_id=binding["query_id"],
                    created_by_goal_id=binding["goal_id"] or None,
                    created_by_goal_revision=binding["goal_revision"],
                    attachment_id=(
                        "att_"
                        + hashlib.sha256(
                            (
                                f"{binding['session_id']}:{lease_id}:{normalized_path}:"
                                f"{safe_name}:{_sha256(output)}"
                            ).encode("utf-8")
                        ).hexdigest()[:12]
                    ),
                )
            except (OSError, ValueError) as exc:
                session_manager.release_attachment_publish_claim(
                    binding["session_id"],
                    lease_id=lease_id,
                    tool_call_id=runtime.tool_call_id,
                )
                return _tool_error(
                    "publish_attachment",
                    runtime,
                    f"unable to persist derived attachment safely: {exc}",
                )
            try:
                session_manager.commit_attachment_publish(
                    binding["session_id"],
                    lease_id=lease_id,
                    tool_call_id=runtime.tool_call_id,
                    published_fields={
                        "published_at": time.time(),
                        "published_attachment_id": public["id"],
                        "published_output_path": normalized_path,
                        "published_name": safe_name,
                        "published_sha256": public["sha256"],
                    },
                    delivery=public,
                )
            except (FileNotFoundError, RuntimeError, ValueError) as exc:
                return _tool_error(
                    "publish_attachment",
                    runtime,
                    f"derived attachment persisted but delivery commit failed: {exc}",
                )
            artifact = _public_publish_artifact(
                item=public, binding=binding, tool_call_id=runtime.tool_call_id
            )
            return ToolMessage(
                content=(
                    f"Attachment published. attachment_id={public['id']}; name={public['name']}; "
                    f"size={public['size']}; sha256={public['sha256']}; "
                    f"download_url={public['download_url']}. Original attachment remains unchanged."
                ),
                name="publish_attachment",
                tool_call_id=runtime.tool_call_id,
                status="success",
                artifact=artifact,
            )

        self.tools = [
            StructuredTool.from_function(
                name="prepare_attachment_edit",
                description=(
                    "Use only when the user wants an uploaded/pasted att_xxx modified, converted, "
                    "or emitted as a new file. It copies the immutable source into this Run's "
                    "/scratch and returns an AttachmentEditLease. For read-only questions, use "
                    "read_resource instead and do not call this tool."
                ),
                func=prepare_attachment_edit,
                args_schema=PrepareAttachmentEditInput,
                infer_schema=False,
            ),
            StructuredTool.from_function(
                name="publish_attachment",
                description=(
                    "Publish one validated file from an AttachmentEditLease as a new downloadable "
                    "derived attachment. The original upload is immutable; a scratch file is not "
                    "a delivered artifact until this tool succeeds."
                ),
                func=publish_attachment,
                args_schema=PublishAttachmentInput,
                infer_schema=False,
            ),
        ]
