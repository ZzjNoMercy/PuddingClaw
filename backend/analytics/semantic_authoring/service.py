"""Prepare and publish one immutable semantic Markdown definition candidate."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any

from pydantic import ValidationError

from analytics.models import get_analytics_model_registry
from analytics.semantic_assets import get_semantic_asset_registry
from runtime_identity.paths import PuddingClawPaths, safe_identity_component

from .contracts import AuthoringBrief, DefinitionKind, kind_from_logical_path, repair_technical_frontmatter
from .discovery import SemanticDiscoveryError, validate_discovery_receipt
from .documents import parse_markdown_document, render_markdown_document
from .validation import validate_markdown_definition

_PLAN_TTL_SECONDS = 24 * 60 * 60
_MAX_CANDIDATE_CHARS = 200_000
_MAX_PUBLIC_PREVIEW_CHARS = 24_000
_LOCK = RLock()
_IMMUTABLE_PLAN_KEYS = (
    "logical_path",
    "kind",
    "baseline_digest",
    "candidate_digest",
    "candidate_markdown",
    "brief",
    "discovery",
    "created_at",
    "expires_at",
    "session_id",
)


class SemanticAuthoringError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _digest_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _digest_text(content: str) -> str:
    return _digest_bytes(content.encode("utf-8"))


def _json_digest(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _digest_text(payload)


def _immutable_plan_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: plan.get(key) for key in _IMMUTABLE_PLAN_KEYS}


def _bounded_preview(value: str) -> tuple[str, bool]:
    if len(value) <= _MAX_PUBLIC_PREVIEW_CHARS:
        return value, False
    return value[:_MAX_PUBLIC_PREVIEW_CHARS].rstrip() + "\n\n…（预览已截断，完整候选仍绑定在 plan digest 中）\n", True


def _safe_definition_path(logical_path: str) -> tuple[PurePosixPath, DefinitionKind]:
    path = PurePosixPath(str(logical_path or "").strip().lstrip("/"))
    if any(part in {"", ".", ".."} or "\x00" in part for part in path.parts):
        raise SemanticAuthoringError("invalid_logical_path")
    try:
        kind = kind_from_logical_path(path.as_posix())
    except ValueError as exc:
        raise SemanticAuthoringError("unsupported_definition_path") from exc
    return path, kind


def _target_path(paths: PuddingClawPaths, logical_path: PurePosixPath) -> Path:
    root = paths.user_definitions().resolve()
    target = (root / Path(*logical_path.parts)).resolve()
    if not target.is_relative_to(root):
        raise SemanticAuthoringError("path_escape_rejected")
    return target


def _read_current(target: Path) -> tuple[str | None, str]:
    if not target.is_file():
        return None, ""
    content = target.read_text(encoding="utf-8")
    return _digest_text(content), content


def _effect_summary(kind: DefinitionKind, frontmatter: dict[str, Any], repaired: list[str]) -> list[str]:
    labels = {
        "measure": "Measure",
        "grain": "Grain",
        "dimension": "Dimension",
        "relation": "Relation",
        "analytics_model": "Analytics Model",
    }
    summary = [f"将该文件注册为 {labels[kind]}，并进入对应 Registry 的运行时上下文。"]
    name = str(frontmatter.get("name") or "").strip()
    description = str(frontmatter.get("description") or "").strip()
    aliases = [str(item) for item in frontmatter.get("aliases") or [] if str(item).strip()]
    tags = [str(item) for item in frontmatter.get("tags") or [] if str(item).strip()]
    version = str(frontmatter.get("version") or "").strip()
    if name:
        summary.append(f"检索与展示名称：{name}。")
    if aliases:
        summary.append(f"以下别名会提高问题匹配：{', '.join(aliases)}。")
    if tags:
        summary.append(f"以下标签会参与检索：{', '.join(tags)}。")
    if description:
        summary.append(f"目录摘要会参与检索：{description}")
    if version:
        summary.append(f"版本元数据：{version}（并发保护仍使用文件 digest）。")
    if kind == "dimension":
        summary.append(f"维度解析模式：{frontmatter.get('resolution_mode')}；解析映射会影响成员取值。")
    elif kind == "relation":
        relation = frontmatter.get("relation") if isinstance(frontmatter.get("relation"), dict) else {}
        summary.append(
            f"关系类型：{frontmatter.get('relation_type')}；基数：{relation.get('cardinality') or '未声明'}。"
        )
    elif kind == "analytics_model":
        data_assets = frontmatter.get("data_assets") if isinstance(frontmatter.get("data_assets"), dict) else {}
        semantic_assets = (
            frontmatter.get("semantic_assets") if isinstance(frontmatter.get("semantic_assets"), dict) else {}
        )
        semantic_count = sum(
            len(semantic_assets.get(group) or []) for group in ("measures", "dimensions", "grains")
        )
        summary.append(
            f"模型范围：{len(data_assets.get('tables') or [])} 个数据资产、{semantic_count} 个语义资产、"
            f"{len(frontmatter.get('asset_relations') or [])} 条关系、{len(frontmatter.get('guardrails') or [])} 条 Guardrail。"
        )
    if repaired:
        summary.append(f"Backend 基于目标路径补齐技术字段：{', '.join(repaired)}。")
    return summary


def _plans_root(paths: PuddingClawPaths) -> Path:
    return paths.state() / "semantic-steward" / "plans"


def _plan_path(paths: PuddingClawPaths, plan_id: str) -> Path:
    safe = safe_identity_component(plan_id, field="plan_id")
    return _plans_root(paths) / safe / "plan.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def prepare_semantic_markdown(
    *,
    logical_path: str,
    candidate_markdown: str,
    baseline_digest: str = "",
    discovery_receipt_id: str = "",
    session_id: str = "",
    brief: dict[str, Any] | None = None,
    paths: PuddingClawPaths | None = None,
) -> dict[str, Any]:
    """Freeze a validated candidate and return a user-auditable preview."""

    user_paths = paths or PuddingClawPaths.from_environment()
    if not str(session_id or "").strip():
        raise SemanticAuthoringError("session_required", "A bound Agent session is required.")
    if len(candidate_markdown) > _MAX_CANDIDATE_CHARS:
        raise SemanticAuthoringError(
            "candidate_too_large",
            f"Semantic Markdown must not exceed {_MAX_CANDIDATE_CHARS} characters.",
        )
    path, kind = _safe_definition_path(logical_path)
    try:
        discovery = validate_discovery_receipt(
            receipt_id=str(discovery_receipt_id or ""),
            target_kind=kind,
            session_id=str(session_id or ""),
            paths=user_paths,
        )
    except SemanticDiscoveryError as exc:
        raise SemanticAuthoringError(exc.code, str(exc)) from exc
    target = _target_path(user_paths, path)
    current_digest, current_content = _read_current(target)
    expected_definition_id = path.parent.name if kind == "analytics_model" else f"{kind}:{path.parent.name}"
    if current_digest is not None and expected_definition_id not in set(discovery.get("returned_ids") or []):
        raise SemanticAuthoringError(
            "discovery_target_not_returned",
            "The existing target was not among the reviewed discovery candidates.",
        )
    expected = str(baseline_digest or "").strip()
    if expected and expected != (current_digest or "absent"):
        raise SemanticAuthoringError(
            "baseline_changed",
            "The published Markdown changed before preparation; read it again before drafting.",
        )
    document = parse_markdown_document(candidate_markdown)
    repaired_document, repaired = repair_technical_frontmatter(
        document,
        kind=kind,
        logical_path=path.as_posix(),
    )
    frozen = render_markdown_document(repaired_document)
    if brief is None:
        raise SemanticAuthoringError("missing_authoring_brief", "An Authoring Brief is required before preparation.")
    try:
        parsed_brief = AuthoringBrief.model_validate(brief)
    except ValidationError as exc:
        raise SemanticAuthoringError(
            "invalid_authoring_brief",
            "Authoring Brief is incomplete or malformed.",
        ) from exc
    validation = validate_markdown_definition(
        frozen,
        logical_path=path.as_posix(),
        brief=parsed_brief,
        definitions_root=user_paths.user_definitions(),
    )
    if not validation["valid"]:
        raise SemanticAuthoringError(
            "candidate_invalid",
            json.dumps(validation["diagnostics"], ensure_ascii=False),
        )
    candidate_digest = _digest_text(frozen)
    diff_lines = list(
        difflib.unified_diff(
            current_content.splitlines(),
            frozen.splitlines(),
            fromfile=f"published/{path.as_posix()}",
            tofile=f"candidate/{path.as_posix()}",
            lineterm="",
        )
    )
    created_at = time.time()
    immutable = {
        "logical_path": path.as_posix(),
        "kind": kind,
        "baseline_digest": current_digest,
        "candidate_digest": candidate_digest,
        "candidate_markdown": frozen,
        "brief": parsed_brief.model_dump(mode="json") if parsed_brief else None,
        "discovery": {
            key: discovery.get(key)
            for key in (
                "receipt_id",
                "query",
                "mode",
                "kinds",
                "catalog_digest",
                "catalog_count",
                "match_count",
                "returned_ids",
                "complete",
            )
        },
        "created_at": created_at,
        "expires_at": created_at + _PLAN_TTL_SECONDS,
        "session_id": str(session_id or ""),
    }
    plan_digest = _json_digest(immutable)
    plan_id = f"semantic-plan-{uuid.uuid4().hex[:16]}"
    body_preview, body_preview_truncated = _bounded_preview(repaired_document.body)
    technical_diff, technical_diff_truncated = _bounded_preview("\n".join(diff_lines))
    plan = {
        **immutable,
        "plan_id": plan_id,
        "plan_digest": plan_digest,
        "status": "prepared",
        "validation": validation,
        "machine_effect_summary": _effect_summary(kind, repaired_document.frontmatter, repaired),
        "body_preview": body_preview,
        "body_preview_truncated": body_preview_truncated,
        "technical_diff": technical_diff,
        "technical_diff_truncated": technical_diff_truncated,
    }
    with _LOCK:
        _write_json(_plan_path(user_paths, plan_id), plan)
    return _public_plan(plan)


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        key: plan.get(key)
        for key in (
            "plan_id",
            "plan_digest",
            "status",
            "logical_path",
            "kind",
            "baseline_digest",
            "candidate_digest",
            "expires_at",
            "validation",
            "machine_effect_summary",
            "body_preview",
            "body_preview_truncated",
            "technical_diff",
            "technical_diff_truncated",
        )
    }


def _load_plan(paths: PuddingClawPaths, plan_id: str) -> dict[str, Any]:
    path = _plan_path(paths, plan_id)
    if not path.is_file():
        raise SemanticAuthoringError("plan_not_found")
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SemanticAuthoringError("plan_unreadable") from exc
    if not isinstance(plan, dict):
        raise SemanticAuthoringError("plan_unreadable")
    return plan


def publish_semantic_markdown(
    *,
    plan_id: str,
    plan_digest: str,
    session_id: str = "",
    paths: PuddingClawPaths | None = None,
) -> dict[str, Any]:
    """Publish one frozen Markdown candidate after digest-bound approval."""

    user_paths = paths or PuddingClawPaths.from_environment()
    if not str(session_id or "").strip():
        raise SemanticAuthoringError("session_required", "A bound Agent session is required.")
    with _LOCK:
        plan = _load_plan(user_paths, plan_id)
        stored_plan_digest = str(plan.get("plan_digest") or "")
        if not stored_plan_digest or _json_digest(_immutable_plan_payload(plan)) != stored_plan_digest:
            raise SemanticAuthoringError("plan_integrity_mismatch")
        if plan.get("status") == "published":
            if plan.get("plan_digest") != plan_digest:
                raise SemanticAuthoringError("plan_digest_mismatch")
            return {
                "ok": True,
                "already_published": True,
                "logical_path": plan["logical_path"],
                "published_digest": plan.get("published_digest"),
                "publication_id": plan.get("publication_id"),
            }
        if plan.get("status") != "prepared":
            raise SemanticAuthoringError("plan_not_prepared")
        if not plan_digest or plan.get("plan_digest") != plan_digest:
            raise SemanticAuthoringError("plan_digest_mismatch")
        if time.time() > float(plan.get("expires_at") or 0):
            raise SemanticAuthoringError("plan_expired")
        owner_session = str(plan.get("session_id") or "")
        if owner_session and owner_session != str(session_id or ""):
            raise SemanticAuthoringError("plan_session_mismatch")
        candidate = str(plan.get("candidate_markdown") or "")
        if _digest_text(candidate) != plan.get("candidate_digest"):
            raise SemanticAuthoringError("candidate_digest_mismatch")
        path, kind = _safe_definition_path(str(plan.get("logical_path") or ""))
        if plan.get("kind") != kind:
            raise SemanticAuthoringError("plan_kind_mismatch")
        target = _target_path(user_paths, path)
        current_digest, current_content = _read_current(target)
        if current_digest == plan.get("candidate_digest"):
            plan["status"] = "published"
            plan["published_digest"] = current_digest
            plan["published_at"] = time.time()
            plan["publication_id"] = plan.get("publication_id") or f"semantic-publication-{uuid.uuid4().hex[:16]}"
            _write_json(_plan_path(user_paths, plan_id), plan)
            return {
                "ok": True,
                "already_published": True,
                "logical_path": path.as_posix(),
                "published_digest": current_digest,
                "publication_id": plan["publication_id"],
            }
        if current_digest != plan.get("baseline_digest"):
            raise SemanticAuthoringError(
                "baseline_changed",
                "The published Markdown changed after preview; prepare a new plan.",
            )
        discovery_data = plan.get("discovery") if isinstance(plan.get("discovery"), dict) else {}
        try:
            validate_discovery_receipt(
                receipt_id=str(discovery_data.get("receipt_id") or ""),
                target_kind=kind,
                session_id=str(session_id or ""),
                paths=user_paths,
            )
        except SemanticDiscoveryError as exc:
            raise SemanticAuthoringError(exc.code, str(exc)) from exc
        try:
            plan_brief = AuthoringBrief.model_validate(plan.get("brief"))
        except ValidationError as exc:
            raise SemanticAuthoringError("plan_brief_invalid") from exc
        current_validation = validate_markdown_definition(
            candidate,
            logical_path=path.as_posix(),
            brief=plan_brief,
            definitions_root=user_paths.user_definitions(),
        )
        if not current_validation["valid"]:
            raise SemanticAuthoringError(
                "definition_dependencies_changed",
                "A referenced definition or package resource changed after preparation.",
            )
        publication_id = f"semantic-publication-{uuid.uuid4().hex[:16]}"
        snapshot_path: Path | None = None
        if current_digest is not None:
            snapshot_path = (
                user_paths.data()
                / "semantic-steward"
                / "snapshots"
                / publication_id
                / path.name
            )
            _atomic_write(snapshot_path, current_content)
        try:
            _atomic_write(target, candidate)
            post_write_validation = validate_markdown_definition(
                candidate,
                logical_path=path.as_posix(),
                brief=plan_brief,
                definitions_root=user_paths.user_definitions(),
            )
            if not post_write_validation["valid"]:
                raise RuntimeError("definition dependencies changed during publication")
            if kind == "analytics_model":
                model_registry = get_analytics_model_registry(user_paths.user_definitions())
                snapshot = model_registry.refresh()
                expected_id = path.parent.name
                loaded = any(item.get("id") == expected_id for item in snapshot.get("models") or [])
                if loaded:
                    context = model_registry.get_model_context(expected_id)
                    if context.get("missing_references"):
                        raise RuntimeError("published Analytics Model has missing package references")
            else:
                snapshot = get_semantic_asset_registry(user_paths.user_definitions()).refresh()
                expected_id = f"{kind}:{path.parent.name}"
                loaded = any(item.get("id") == expected_id for item in snapshot.get("assets") or [])
            if not loaded:
                raise RuntimeError(f"published {kind} did not load into its registry")
        except Exception:
            if current_digest is None:
                target.unlink(missing_ok=True)
            else:
                _atomic_write(target, current_content)
            if kind == "analytics_model":
                get_analytics_model_registry(user_paths.user_definitions()).refresh()
            else:
                get_semantic_asset_registry(user_paths.user_definitions()).refresh()
            raise
        published_digest = _digest_text(target.read_text(encoding="utf-8"))
        plan.update(
            {
                "status": "published",
                "published_at": time.time(),
                "published_digest": published_digest,
                "publication_id": publication_id,
                "snapshot_available": snapshot_path is not None,
            }
        )
        _write_json(_plan_path(user_paths, plan_id), plan)
        return {
            "ok": True,
            "already_published": False,
            "logical_path": path.as_posix(),
            "published_digest": published_digest,
            "publication_id": publication_id,
            "snapshot_available": snapshot_path is not None,
        }
