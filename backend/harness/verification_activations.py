"""Typed current-Run Tool activations for effective verification contracts."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from graph.attachment_store import attachment_store
from graph.session_manager import session_manager
from graph.tool_result_adapter import tool_result_adapter
from harness.artifact_paths import artifact_path_matches, extract_declared_artifact_targets
from harness.models import (
    ArtifactReference,
    ArtifactRole,
    ArtifactScope,
    ValidationReceipt,
    VerificationActivation,
)
from harness.tool_execution import ShellPolicyAnalyzer

_WEB_TOOLS = frozenset(
    {
        "fetch_url",
        "tavily_search",
        "llamaindex_knowledge_query",
    }
)
_ANALYTICS_TOOLS = frozenset(
    {
        "pandas_knowledge_query",
        "database_schema_inspect",
        "database_sql_generate",
        "database_sql_validate",
        "database_sql_execute",
        "database_query_trace_inspect",
        "database_query_result_page",
        "semantic_entity_lookup",
        "inspect_dimension_build_input",
        "request_dimension_build_rule",
        "enqueue_semantic_dimension_build",
        "get_semantic_dimension_build_job",
        "publish_semantic_dimension_build",
        "ensure_attachment_table_asset",
        "list_logical_dataset_candidates",
        "request_logical_dataset_rule",
        "apply_logical_dataset_rule",
    }
)
_MATERIAL_ANALYTICS_TOOLS = frozenset(
    {
        "pandas_knowledge_query",
        "database_sql_execute",
        "database_query_trace_inspect",
        "database_query_result_page",
        "semantic_entity_lookup",
        "inspect_dimension_build_input",
        "get_semantic_dimension_build_job",
        "publish_semantic_dimension_build",
        "ensure_attachment_table_asset",
        "list_logical_dataset_candidates",
        "apply_logical_dataset_rule",
    }
)
_WRITE_TOOLS = frozenset(
    {
        "write_file",
        "edit_file",
        "patch_file",
        "commit_external_artifact",
        "commit_external_directory",
        "publish_attachment",
    }
)
_PROPORTIONAL_MUTATION_TOOLS = frozenset(
    {
        *_WRITE_TOOLS,
        "install_skill",
        "update_skill",
        "apply_logical_dataset_rule",
        "publish_semantic_dimension_build",
    }
)
_CODE_EXTENSIONS = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".css",
        ".dart",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".htm",
        ".java",
        ".js",
        ".cjs",
        ".jsx",
        ".mjs",
        ".kt",
        ".kts",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".scss",
        ".sh",
        ".sql",
        ".swift",
        ".ts",
        ".cts",
        ".mts",
        ".tsx",
        ".vue",
    }
)
_ARTIFACT_EXTENSIONS = frozenset(
    {
        ".csv",
        ".docx",
        ".html",
        ".ipynb",
        ".json",
        ".md",
        ".pdf",
        ".pptx",
        ".svg",
        ".txt",
        ".xlsx",
        ".yaml",
        ".yml",
    }
)
_TEST_COMMAND_RE = re.compile(
    r"(?:^|\s)(?:pytest|ruff|mypy|pyright|flutter\s+(?:test|analyze)|"
    r"(?:npm|pnpm|yarn)\s+(?:test|build|lint|run\s+(?:test|build|lint|check)))(?:\s|$)",
    re.IGNORECASE,
)
_NETWORK_COMMAND_RE = re.compile(r"(?:^|\s)(?:curl|wget)\s+", re.IGNORECASE)
_WEB_SKILL_SCRIPT_RE = re.compile(
    r"(?:^|/)(?:aihot|tavily-search)/(?:.+/)?[^/\s]+\.(?:py|js|mjs|cjs)$",
    re.IGNORECASE,
)
_ANALYTICS_COMMAND_RE = re.compile(
    r"(?:pandas|polars|duckdb|sqlite|\.csv\b|\.tsv\b|\.xlsx?\b|select\s+.+\s+from)",
    re.IGNORECASE | re.DOTALL,
)
_COMMAND_EXIT_RE = re.compile(
    r"\[Command\s+(?P<status>succeeded|failed)\s+with\s+exit\s+code\s+"
    r"(?P<code>-?\d+)\]",
    re.IGNORECASE,
)
_PLAIN_EXIT_RE = re.compile(r"(?:^|\n)Exit code:\s*(?P<code>-?\d+)\s*$", re.IGNORECASE)
_ANALYTICS_RESULT_REF_RE = re.compile(
    r"(?:result_id|query_trace_id|trace_id|database_source_id|数据源)[：:\s]+"
    r"(?P<value>[A-Za-z0-9_.:/-]+)",
    re.IGNORECASE,
)
_ERROR_PREFIXES = (
    "error:",
    "exception:",
    "traceback",
    "tool execution did not return",
    "❌",
    "🧮 sql 执行失败",
    "📊 pandasqueryengine 查询失败",
    "sql 执行失败",
    "查询失败",
    "未找到相关内容",
    "command not found",
)


def verification_packs_for_tool(
    tool_name: str,
    args: dict[str, Any] | None = None,
) -> list[str]:
    """Return exact verification packs activated by one successful Tool."""

    if tool_name in _WEB_TOOLS:
        return ["web_research"]
    if tool_name in _ANALYTICS_TOOLS:
        return ["analytics"]
    if tool_name == "prepare_attachment_edit":
        # Taking an edit lease is a one-way capability transition: the Run may
        # no longer finish with only a scratch copy or a verbal completion.
        # Artifact delivery remains unsatisfied until publish_attachment emits
        # a material Attachment ArtifactReference.
        return ["artifact"]
    if tool_name == "publish_attachment":
        # Publishing is delivery, not proof that code was tested. Every file
        # type (including binary/image/archive outputs) receives the same
        # artifact gate; code validation must come from a real validation
        # command rather than the filename extension.
        return ["artifact"]
    if tool_name in _WRITE_TOOLS:
        raw_path = str(
            ((args or {}).get("output_name") if tool_name == "publish_attachment" else None)
            or (args or {}).get("file_path")
            or (args or {}).get("path")
            or (args or {}).get("output_path")
            or ""
        ).lower()
        suffix = "." + raw_path.rsplit(".", 1)[-1] if "." in raw_path else ""
        packs: list[str] = []
        if suffix in _CODE_EXTENSIONS:
            packs.append("code")
        if suffix in _ARTIFACT_EXTENSIONS:
            packs.append("artifact")
        return packs
    if tool_name not in {"execute", "terminal"}:
        return []
    command = str((args or {}).get("command") or "")
    packs: list[str] = []
    if _NETWORK_COMMAND_RE.search(command) or _command_executes_web_skill(command):
        packs.append("web_research")
    if _command_performs_analytics(command):
        packs.append("analytics")
    if _command_performs_validation(command):
        packs.append("code")
    return packs


def _command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def _command_executes_web_skill(command: str) -> bool:
    """Recognize an executed Skill entrypoint, not a path mentioned in code."""

    return any(
        _tokens_execute_web_skill(tokens, cwd=cwd)
        for cwd, tokens in _effective_command_segments(command)
    )


def _effective_command_segments(
    command: str,
    *,
    initial_cwd: str = "/workspace",
) -> list[tuple[str, list[str]]]:
    """Flatten wrappers/shells while carrying ``cd`` across command segments."""

    try:
        raw_segments = ShellPolicyAnalyzer.parse_segments(command)
    except ValueError:
        return []
    cwd = initial_cwd
    effective: list[tuple[str, list[str]]] = []
    for raw_tokens in raw_segments:
        tokens = ShellPolicyAnalyzer.unwrap_command(raw_tokens)
        if not tokens:
            continue
        executable = tokens[0].rsplit("/", 1)[-1].lower()
        if executable == "cd" and len(tokens) > 1:
            target = tokens[1]
            cwd = posixpath.normpath(
                target if target.startswith("/") else posixpath.join(cwd, target)
            )
            continue
        if executable in {"sh", "bash", "zsh"} and len(tokens) >= 3 and tokens[1] in {"-c", "-lc"}:
            effective.extend(
                _effective_command_segments(tokens[2], initial_cwd=cwd)
            )
            continue
        effective.append((cwd, tokens))
    return effective


def _tokens_execute_web_skill(tokens: list[str], *, cwd: str) -> bool:
    executable = tokens[0].rsplit("/", 1)[-1].lower()
    if executable not in {"python", "python3", "node"}:
        return False
    for token in tokens[1:]:
        if token.startswith("-"):
            continue
        resolved = posixpath.normpath(
            token if token.startswith("/") else posixpath.join(cwd, token)
        )
        if resolved.startswith("/skills/") and _WEB_SKILL_SCRIPT_RE.search(
            resolved.removeprefix("/skills/")
        ):
            return True
    return False


def _command_performs_analytics(command: str) -> bool:
    tokens = _command_tokens(command)
    if not tokens:
        return False
    executable = tokens[0].rsplit("/", 1)[-1].lower()
    if executable in {
        "flutter",
        "mypy",
        "npm",
        "pnpm",
        "pyright",
        "pytest",
        "ruff",
        "yarn",
    }:
        return False
    if executable in {
        "cat",
        "echo",
        "find",
        "grep",
        "head",
        "ls",
        "printf",
        "rg",
        "stat",
        "tail",
    }:
        return False
    return bool(_ANALYTICS_COMMAND_RE.search(command))


def _command_performs_validation(command: str) -> bool:
    if re.search(r"(?:\|\||&&|;)\s*true(?:\s|$)", command, re.IGNORECASE):
        return False
    tokens = _command_tokens(command)
    if not tokens:
        return False
    executable = tokens[0].rsplit("/", 1)[-1].lower()
    if executable in {"pytest", "ruff", "mypy", "pyright"}:
        return bool(_TEST_COMMAND_RE.search(command))
    if executable == "flutter":
        return len(tokens) > 1 and tokens[1].lower() in {"test", "analyze"}
    if executable in {"npm", "pnpm", "yarn"}:
        return bool(_TEST_COMMAND_RE.search(command))
    if executable == "node":
        return len(tokens) > 1 and tokens[1].lower() in {"--check", "-c"}
    if executable == "npx":
        meaningful = [
            token.lower()
            for token in tokens[1:]
            if token and not token.startswith("-")
        ]
        return bool(meaningful and meaningful[0] in {"eslint", "tsc"})
    if executable in {"python", "python3"}:
        if len(tokens) > 2 and tokens[1] == "-m":
            return tokens[2].lower() in {"mypy", "pyright", "pytest", "ruff"}
        if len(tokens) < 2 or tokens[1] in {"-c", "-"}:
            return False
        script_name = tokens[1].rsplit("/", 1)[-1].lower()
        return bool(
            script_name.endswith(".py")
            and re.search(r"(?:^|[_-])(check|lint|test|validate|validator)(?:[_-]|\.|$)", script_name)
        )
    return False


def _result_messages(
    result: ToolMessage | Command[Any],
    *,
    expected_call_id: str | None = None,
) -> list[ToolMessage]:
    if isinstance(result, ToolMessage):
        messages = [result]
    elif isinstance(result, Command):
        update = result.update if isinstance(result.update, dict) else {}
        raw_messages = update.get("messages") or []
        messages = [item for item in raw_messages if isinstance(item, ToolMessage)]
    else:
        messages = []
    if expected_call_id:
        messages = [
            message
            for message in messages
            if str(getattr(message, "tool_call_id", "") or "") == expected_call_id
        ]
    return messages


def _message_succeeded(message: ToolMessage) -> bool:
    if str(getattr(message, "status", "") or "").lower() == "error":
        return False
    content = str(getattr(message, "content", "") or "").strip()
    lowered = content.lower()
    if not content or any(lowered.startswith(prefix) for prefix in _ERROR_PREFIXES):
        return False
    command_exit = _COMMAND_EXIT_RE.search(content)
    if command_exit is not None:
        return (
            command_exit.group("status").lower() == "succeeded"
            and int(command_exit.group("code")) == 0
        )
    plain_exit = _PLAIN_EXIT_RE.search(content)
    if plain_exit is not None and int(plain_exit.group("code")) != 0:
        return False
    return True


def tool_result_succeeded(
    result: ToolMessage | Command[Any],
    *,
    expected_call_id: str | None = None,
) -> bool:
    """Fail closed for the current Tool call, including structured exit codes."""

    messages = _result_messages(result, expected_call_id=expected_call_id)
    if not messages:
        return False
    return all(_message_succeeded(message) for message in messages)


def resolve_published_attachment(
    artifact: dict[str, Any] | None,
    *,
    session_id: str,
    run_id: str,
    query_id: str,
    tool_call_id: str,
    goal_id: str | None = None,
    goal_revision: int | None = None,
) -> dict[str, Any] | None:
    """Resolve a publish receipt through AttachmentStore authority.

    Neither model text nor ToolMessage artifact URLs are trusted. The returned
    object is rebuilt by the server only after receipt, scope, ownership and
    actual-byte hash checks all agree.
    """

    if not isinstance(artifact, dict):
        return None
    published_item = artifact.get("published_attachment")
    receipt_data = artifact.get("artifact_reference")
    if not isinstance(published_item, dict):
        return None
    try:
        receipt = ArtifactReference.model_validate(receipt_data)
    except Exception:
        return None
    attachment_id = str(published_item.get("id") or "")
    stored = attachment_store.get(session_id, attachment_id) if attachment_id else None
    if not isinstance(stored, dict):
        return None
    try:
        stored_path = Path(str(stored.get("path") or "")).resolve(strict=True)
        hasher = hashlib.sha256()
        with stored_path.open("rb") as source_file:
            while chunk := source_file.read(1024 * 1024):
                hasher.update(chunk)
        actual_sha = f"sha256:{hasher.hexdigest()}"
        receipt_host_path = Path(str(receipt.host_path or "")).resolve(strict=True)
    except (OSError, ValueError):
        return None
    expected_goal_id = goal_id or None
    checks = (
        receipt.scope == ArtifactScope.ATTACHMENT,
        receipt.authorized,
        receipt.tool_call_id == tool_call_id,
        receipt.run_id == run_id,
        receipt.query_id == query_id,
        receipt.goal_id == expected_goal_id,
        receipt.goal_revision == goal_revision,
        stored.get("source") == "generated",
        stored.get("created_by_run_id") == run_id,
        stored.get("created_by_query_id") == query_id,
        (stored.get("created_by_goal_id") or None) == expected_goal_id,
        stored.get("created_by_goal_revision") == goal_revision,
        receipt.path == f"attachment://{attachment_id}",
        receipt_host_path == stored_path,
        receipt.content_sha256 == actual_sha,
        stored.get("sha256") == actual_sha,
        published_item.get("sha256") == actual_sha,
    )
    return attachment_store.public_item(stored) if all(checks) else None


def _result_evidence_refs(
    *,
    tool_call_id: str,
    tool_name: str,
    args: dict[str, Any],
    result: ToolMessage | Command[Any] | None,
    session_id: str = "",
    run_id: str = "",
    query_id: str = "",
    workspace_path: str = "",
    goal_id: str | None = None,
    goal_revision: int | None = None,
) -> list[dict[str, Any]]:
    if result is None:
        return []
    messages = _result_messages(result, expected_call_id=tool_call_id)
    refs: list[dict[str, Any]] = []
    for message in messages:
        content = str(getattr(message, "content", "") or "").strip()
        if not content or not _message_succeeded(message):
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        refs.append(
            {
                "kind": "tool_result",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "output_digest": f"sha256:{digest}",
                "output_preview": content[:1000],
            }
        )
        # Normalize every ToolMessage at the authority boundary. The adapter
        # itself rejects non-retrieval tools, while explicit envelopes and
        # arbitrary Skill script outputs remain portable across tool names.
        tool_input = json.dumps(args, ensure_ascii=False, sort_keys=True)
        adapted = tool_result_adapter.adapt(
            content,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_call_id=tool_call_id,
        )
        for source in adapted.sources:
            refs.append(
                {
                    "kind": "source",
                    "tool_call_id": tool_call_id,
                    "source_id": source.get("source_id"),
                    "source_type": source.get("source_type"),
                    "uri": source.get("uri"),
                    "title": source.get("title"),
                }
            )
        for match in _ANALYTICS_RESULT_REF_RE.finditer(content):
            refs.append(
                {
                    "kind": "analytics_result",
                    "tool_call_id": tool_call_id,
                    "ref": match.group("value"),
                }
            )
        if tool_name == "publish_attachment":
            message_artifact = getattr(message, "artifact", None)
            resolved = resolve_published_attachment(
                message_artifact if isinstance(message_artifact, dict) else None,
                session_id=session_id,
                run_id=run_id,
                query_id=query_id,
                tool_call_id=tool_call_id,
                goal_id=goal_id,
                goal_revision=goal_revision,
            )
            if resolved is not None:
                receipt = ArtifactReference.model_validate(message_artifact["artifact_reference"])
                refs.append({"kind": "artifact_write", **receipt.model_dump(mode="json")})
        elif tool_name == "commit_external_directory":
            message_artifact = getattr(message, "artifact", None)
            deliveries = (
                message_artifact.get("delivered_artifacts")
                if isinstance(message_artifact, dict)
                else None
            )
            for delivered in deliveries if isinstance(deliveries, list) else []:
                if not isinstance(delivered, dict):
                    continue
                target_path = str(delivered.get("target_path") or "")
                content_sha256 = str(delivered.get("content_sha256") or "")
                artifact_id = str(delivered.get("artifact_id") or "")
                if not target_path or not content_sha256 or not artifact_id:
                    continue
                artifact = ArtifactReference(
                    artifact_id=artifact_id,
                    scope=ArtifactScope.EXTERNAL,
                    role=ArtifactRole.TARGET,
                    path=target_path,
                    host_path=target_path,
                    authorized=True,
                    run_id=run_id or None,
                    query_id=query_id or None,
                    goal_id=goal_id,
                    goal_revision=goal_revision,
                    tool_call_id=tool_call_id,
                    output_digest=f"sha256:{digest}",
                    content_sha256=content_sha256,
                )
                refs.append({"kind": "artifact_write", **artifact.model_dump(mode="json")})
                refs.extend(
                    _commit_validation_receipts(
                        session_id=session_id,
                        run_id=run_id,
                        goal_id=goal_id,
                        goal_revision=goal_revision,
                        args={
                            "file_path": target_path,
                            "expected_draft_sha256": content_sha256,
                            "validation_receipt_ids": delivered.get(
                                "validation_receipt_ids"
                            )
                            or [],
                        },
                    )
                )
        elif tool_name in _WRITE_TOOLS:
            raw_path = str(args.get("file_path") or args.get("path") or "").strip()
            if raw_path:
                artifact = _artifact_reference_for_write(
                    raw_path=raw_path,
                    tool_call_id=tool_call_id,
                    output_digest=f"sha256:{digest}",
                    session_id=session_id,
                    run_id=run_id,
                    query_id=query_id,
                    workspace_path=workspace_path,
                )
                refs.append({"kind": "artifact_write", **artifact.model_dump(mode="json")})
                if artifact.scope == ArtifactScope.EXTERNAL and artifact.content_sha256:
                    mutation = session_manager.find_external_mutation_receipt(
                        session_id,
                        run_id=run_id,
                        canonical_path=str(artifact.host_path or artifact.path),
                        after_sha256=artifact.content_sha256,
                    )
                    if mutation is not None:
                        refs.append(dict(mutation))
    return refs


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _external_write_grant(
    session_id: str,
    host_path: str,
    *,
    run_id: str,
) -> dict[str, Any] | None:
    if not session_id:
        return None
    for grant in session_manager.list_permission_grants(session_id):
        if (
            grant.get("type") == "external_file_write"
            and grant.get("target_kind") == "exact_file"
            and grant.get("target") == host_path
            and "write" in (grant.get("capabilities") or [])
        ):
            return grant
        if (
            grant.get("type") == "external_directory_write"
            and grant.get("target_kind") == "exact_directory"
            and "write" in (grant.get("capabilities") or [])
        ):
            root = Path(str(grant.get("target") or "")).expanduser().resolve()
            requested = Path(host_path).expanduser().resolve()
            if (
                _is_relative_to(requested, root)
                and session_manager.has_external_directory_permission(
                    session_id,
                    root,
                    access="write",
                    run_id=run_id,
                )
            ):
                return grant
    return None


def _artifact_reference_for_write(
    *,
    raw_path: str,
    tool_call_id: str,
    output_digest: str,
    session_id: str,
    run_id: str,
    query_id: str,
    workspace_path: str,
) -> ArtifactReference:
    """Resolve a write target without changing its authority boundary."""

    workspace = Path(workspace_path).expanduser().resolve() if workspace_path else None
    try:
        persisted_run = (
            session_manager.get_run_state(session_id, run_id)
            if session_id and run_id
            else None
        )
    except (AssertionError, FileNotFoundError):
        # The pure builder is also used with isolated SessionManager instances
        # in tests and migrations. Runtime middleware always has the initialized
        # authoritative manager; receipt verification remains fail-closed.
        persisted_run = None
    run_payload = persisted_run if isinstance(persisted_run, dict) else {}
    execution = (
        run_payload.get("config_snapshot", {}).get("execution", {})
        if isinstance(run_payload.get("config_snapshot"), dict)
        else {}
    )
    objective = str(run_payload.get("objective") or "")
    normalized = raw_path.replace("\\", "/")
    if normalized == "/scratch" or normalized.startswith("/scratch/"):
        scratch_root_value = str(execution.get("scratch_host_path") or "").strip()
        scratch_root = Path(scratch_root_value).expanduser().resolve() if scratch_root_value else None
        relative = normalized.removeprefix("/scratch").lstrip("/")
        host = (scratch_root / relative).resolve() if scratch_root is not None else None
        if host is not None and scratch_root is not None and _is_relative_to(host, scratch_root):
            virtual_path = "/scratch" + (f"/{relative}" if relative else "")
            canonical = virtual_path
        else:
            virtual_path = None
            canonical = raw_path
        scope = ArtifactScope.SCRATCH
        grant = None
    elif normalized == "/workspace" or normalized.startswith("/workspace/"):
        relative = normalized.removeprefix("/workspace").lstrip("/")
        host = (workspace / relative).resolve() if workspace is not None else None
        if host is not None and workspace is not None and _is_relative_to(host, workspace):
            virtual_path = "/workspace" + (f"/{relative}" if relative else "")
            canonical = str(host)
            scope = ArtifactScope.WORKSPACE
            grant = None
        else:
            # ``/workspace/../...`` is not a workspace file. Preserve the
            # resolved authority boundary instead of minting a trusted receipt.
            virtual_path = None
            canonical = str(host) if host is not None else raw_path
            scope = ArtifactScope.EXTERNAL
            grant = _external_write_grant(session_id, canonical, run_id=run_id)
    else:
        requested = Path(raw_path).expanduser()
        if requested.is_absolute():
            host = requested.resolve()
        elif workspace is not None:
            host = (workspace / requested).resolve()
        else:
            host = None
        if host is not None and workspace is not None and _is_relative_to(host, workspace):
            relative = host.relative_to(workspace).as_posix()
            virtual_path = f"/workspace/{relative}" if relative else "/workspace"
            canonical = str(host)
            scope = ArtifactScope.WORKSPACE
            grant = None
        else:
            relative = ""
            virtual_path = None
            canonical = str(host) if host is not None else raw_path
            scope = ArtifactScope.EXTERNAL
            grant = _external_write_grant(session_id, canonical, run_id=run_id)

    identity_path = str(host) if host is not None else canonical
    declared_targets = (
        [str(item) for item in run_payload.get("declared_artifact_targets") or [] if str(item)]
        if isinstance(run_payload.get("declared_artifact_targets"), list)
        else extract_declared_artifact_targets(objective)
    )
    if scope == ArtifactScope.SCRATCH:
        artifact_role = ArtifactRole.TEMPORARY
    else:
        artifact_role = ArtifactRole.TARGET if any(
            artifact_path_matches(candidate, declared)
            for candidate in {raw_path, canonical, virtual_path or ""}
            if candidate
            for declared in declared_targets
        ) else ArtifactRole.CANDIDATE
    content_sha256: str | None = None
    size_bytes: int | None = None
    mtime_ns: int | None = None
    if host is not None and host.is_file():
        hasher = hashlib.sha256()
        with host.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        stat = host.stat()
        content_sha256 = f"sha256:{hasher.hexdigest()}"
        size_bytes = stat.st_size
        mtime_ns = stat.st_mtime_ns
    artifact_id = "artifact-" + hashlib.sha256(
        (
            f"{scope.value}\0{execution.get('workspace_id', '')}\0"
            f"{identity_path}"
        ).encode()
    ).hexdigest()[:20]
    return ArtifactReference(
        artifact_id=artifact_id,
        scope=scope,
        role=artifact_role,
        path=canonical,
        host_path=str(host) if host is not None else None,
        virtual_path=virtual_path,
        workspace_relative_path=relative or None,
        authorized=(scope in {ArtifactScope.WORKSPACE, ArtifactScope.SCRATCH} or grant is not None),
        permission_grant_id=(str(grant.get("id")) if grant is not None else None),
        run_id=run_id or None,
        query_id=query_id or None,
        goal_id=(str(run_payload.get("goal_id")) if run_payload.get("goal_id") else None),
        goal_revision=(
            int(run_payload.get("goal_revision"))
            if run_payload.get("goal_revision") is not None
            else None
        ),
        backend_id=(str(execution.get("backend_id")) if execution.get("backend_id") else None),
        workspace_id=(
            str(execution.get("workspace_id")) if execution.get("workspace_id") else None
        ),
        tool_call_id=tool_call_id,
        output_digest=output_digest,
        content_sha256=content_sha256,
        size_bytes=size_bytes,
        mtime_ns=mtime_ns,
    )


def _validator_input_paths(command: str) -> list[str]:
    """Return file operands that the recognized validator actually received."""

    inputs: list[str] = []
    for cwd, tokens in _effective_command_segments(command):
        if not tokens:
            continue
        executable = tokens[0].rsplit("/", 1)[-1].lower()
        operands: list[str] = []
        if executable == "node":
            for flag in ("--check", "-c"):
                if flag in tokens:
                    index = tokens.index(flag)
                    if index + 1 < len(tokens):
                        operands.append(tokens[index + 1])
                    break
        elif executable in {"python", "python3"}:
            if len(tokens) > 2 and tokens[1] == "-m":
                operands.extend(tokens[3:])
            elif len(tokens) > 2:
                # tokens[1] is the validator program itself, not the artifact
                # whose bytes the validator claims to cover.
                operands.extend(tokens[2:])
        elif executable == "npx" and len(tokens) > 2:
            operands.extend(tokens[2:])
        elif executable in {"pytest", "ruff", "mypy", "pyright"}:
            operands.extend(tokens[1:])

        for raw in operands:
            if not raw or raw.startswith("-") or "://" in raw:
                continue
            if raw.lower() in {"check", "test", "build", "lint"}:
                continue
            normalized = posixpath.normpath(
                raw if raw.startswith("/") else posixpath.join(cwd, raw)
            )
            if normalized not in inputs:
                inputs.append(normalized)
    return inputs


def _controlled_validator_spec(command: str) -> tuple[str, str] | None:
    """Return commit-authoritative validator kind/version for one safe argv.

    This intentionally rejects compound shell programs.  Tool success belongs
    to the whole shell expression, so accepting ``node --check bad.js || true``
    would mint a false syntax receipt.  Free-form Python validator scripts are
    likewise completion evidence only: argv does not prove that the script
    opened or interpreted the named artifact.
    """

    try:
        raw_segments = ShellPolicyAnalyzer.parse_segments(command)
    except ValueError:
        return None
    if len(raw_segments) != 1:
        return None
    tokens = ShellPolicyAnalyzer.unwrap_command(raw_segments[0])
    if not tokens:
        return None
    non_authorizing_flags = {
        "-h",
        "--help",
        "-v",
        "--version",
        "--exit-zero",
    }
    if any(item.lower() in non_authorizing_flags for item in tokens[1:]):
        return None
    executable = tokens[0].rsplit("/", 1)[-1].lower()
    if (
        executable == "node"
        and len(tokens) == 3
        and tokens[1] in {"--check", "-c"}
        and not tokens[2].startswith("-")
    ):
        return "javascript_syntax", "node-check/v1"
    if (
        executable in {"python", "python3"}
        and len(tokens) >= 4
        and tokens[1:3] == ["-m", "py_compile"]
        and all(not item.startswith("-") for item in tokens[3:])
    ):
        return "static_check", "python-py-compile/v1"
    if (
        executable == "ruff"
        and len(tokens) >= 3
        and tokens[1] == "check"
        and any(not item.startswith("-") for item in tokens[2:])
    ):
        return "static_check", "ruff-check/v1"
    if executable in {"mypy", "pyright"} and len(tokens) >= 2 and any(
        not item.startswith("-") for item in tokens[1:]
    ):
        return "static_check", f"{executable}/v1"
    if (
        executable == "npx"
        and len(tokens) >= 3
        and tokens[1] in {"eslint", "html-validate", "stylelint"}
        and any(not item.startswith("-") for item in tokens[2:])
    ):
        validator = tokens[1]
        kind = "html_structure" if validator == "html-validate" else "static_check"
        return kind, f"{validator}/v1"
    if executable in {"shellcheck", "sqlfluff"} and len(tokens) >= 2 and any(
        not item.startswith("-") for item in tokens[1:]
    ):
        return "static_check", f"{executable}/v1"
    return None


def _file_identity(path: Path) -> tuple[str, int] | None:
    try:
        if not path.is_file():
            return None
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        return f"sha256:{hasher.hexdigest()}", path.stat().st_size
    except OSError:
        return None


def _artifact_identity_id(scope: str, workspace_id: str, identity_path: str) -> str:
    return "artifact-" + hashlib.sha256(
        f"{scope}\0{workspace_id}\0{identity_path}".encode()
    ).hexdigest()[:20]


def _validation_artifact_refs(
    *,
    session_id: str,
    run_id: str,
    command: str,
    workspace_path: str,
) -> list[dict[str, Any]]:
    """Resolve only explicit validator operands to server-known artifact identities."""

    if not session_id or not run_id:
        return []
    try:
        run = session_manager.get_run_state(session_id, run_id)
    except (AssertionError, FileNotFoundError):
        run = None
    run_payload = run if isinstance(run, dict) else {}
    execution = (
        run_payload.get("config_snapshot", {}).get("execution", {})
        if isinstance(run_payload.get("config_snapshot"), dict)
        else {}
    )
    workspace_id = str(execution.get("workspace_id") or "")
    scratch_root_value = str(execution.get("scratch_host_path") or "")
    scratch_root = (
        Path(scratch_root_value).expanduser().resolve()
        if scratch_root_value
        else None
    )
    workspace = Path(workspace_path).expanduser().resolve() if workspace_path else None

    known_refs: list[dict[str, Any]] = []
    for activation in run_payload.get("verification_activations") or []:
        if not isinstance(activation, dict):
            continue
        for ref in activation.get("evidence_refs") or []:
            if isinstance(ref, dict) and ref.get("kind") == "artifact_write":
                known_refs.append(ref)
    goal_id = str(run_payload.get("goal_id") or "")
    if goal_id:
        try:
            goal = session_manager.get_goal_state(session_id, goal_id)
        except (AssertionError, FileNotFoundError):
            goal = None
        if isinstance(goal, dict) and goal.get("objective_revision") == run_payload.get(
            "goal_revision"
        ):
            for ref in goal.get("evidence_refs") or []:
                if isinstance(ref, dict) and ref.get("kind") == "artifact_write":
                    known_refs.append(ref)

    alias_map: dict[str, dict[str, Any]] = {}
    for ref in known_refs:
        for key in ("path", "host_path", "virtual_path"):
            value = str(ref.get(key) or "").strip()
            if value:
                alias_map[posixpath.normpath(value)] = ref

    try:
        leases = session_manager.list_external_artifact_leases(session_id)
    except (AssertionError, FileNotFoundError):
        leases = []
    lease_by_staged_path = {
        posixpath.normpath(str(lease.get("staged_path") or "")): lease
        for lease in leases
        if isinstance(lease, dict) and lease.get("staged_path") and lease.get("target_path")
    }
    try:
        directory_leases = session_manager.list_external_directory_leases(session_id)
    except (AssertionError, FileNotFoundError):
        directory_leases = []
    directory_lease_roots = sorted(
        (
            (
                posixpath.normpath(str(lease.get("staged_dir") or "")),
                lease,
            )
            for lease in directory_leases
            if isinstance(lease, dict)
            and lease.get("staged_dir")
            and lease.get("directory_path")
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )

    resolved: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for observed_path in _validator_input_paths(command):
        normalized = posixpath.normpath(observed_path)
        matched = alias_map.get(normalized)
        lease = lease_by_staged_path.get(normalized)
        if lease is not None:
            target_path = str(Path(str(lease["target_path"])).expanduser().resolve())
            digest = str((matched or {}).get("content_sha256") or "")
            host_value = str((matched or {}).get("host_path") or "")
            actual = _file_identity(Path(host_value)) if host_value else None
            if actual is None and scratch_root is not None and normalized.startswith("/scratch/"):
                actual = _file_identity(
                    (scratch_root / normalized.removeprefix("/scratch/")).resolve()
                )
            if actual is not None:
                digest = actual[0]
            if not digest.startswith("sha256:"):
                continue
            ref_workspace_id = str((matched or {}).get("workspace_id") or workspace_id)
            artifact_id = _artifact_identity_id("external", ref_workspace_id, target_path)
            item = {
                "artifact_id": artifact_id,
                "content_sha256": digest,
                "path": target_path,
                "observed_path": normalized,
            }
        elif directory_lease := next(
            (
                (staged_root, candidate)
                for staged_root, candidate in directory_lease_roots
                if normalized.startswith(f"{staged_root}/")
            ),
            None,
        ):
            staged_root, lease_payload = directory_lease
            relative = posixpath.relpath(normalized, staged_root)
            target_path = str(
                (
                    Path(str(lease_payload["directory_path"])).expanduser().resolve()
                    / relative
                ).resolve()
            )
            actual = None
            if scratch_root is not None and normalized.startswith("/scratch/"):
                actual = _file_identity(
                    (scratch_root / normalized.removeprefix("/scratch/")).resolve()
                )
            if actual is None:
                continue
            artifact_id = _artifact_identity_id(
                "external",
                workspace_id,
                target_path,
            )
            item = {
                "artifact_id": artifact_id,
                "content_sha256": actual[0],
                "path": target_path,
                "observed_path": normalized,
            }
        elif matched is not None:
            digest = str(matched.get("content_sha256") or "")
            host_value = str(matched.get("host_path") or "")
            actual = _file_identity(Path(host_value)) if host_value else None
            if actual is not None:
                digest = actual[0]
            if not digest.startswith("sha256:"):
                continue
            canonical = str(
                matched.get("host_path")
                or matched.get("path")
                or matched.get("virtual_path")
                or normalized
            )
            item = {
                "artifact_id": str(matched.get("artifact_id") or ""),
                "content_sha256": digest,
                "path": canonical,
                "observed_path": normalized,
            }
        else:
            host: Path | None = None
            scope = "external"
            identity_path = normalized
            if normalized == "/workspace" or normalized.startswith("/workspace/"):
                if workspace is not None:
                    host = (workspace / normalized.removeprefix("/workspace/")).resolve()
                    scope = "workspace"
                    identity_path = str(host)
            elif normalized == "/scratch" or normalized.startswith("/scratch/"):
                if scratch_root is not None:
                    host = (scratch_root / normalized.removeprefix("/scratch/")).resolve()
                    scope = "scratch"
                    identity_path = normalized
            elif normalized.startswith("/"):
                host = Path(normalized).expanduser().resolve()
                identity_path = str(host)
            actual = _file_identity(host) if host is not None else None
            if actual is None:
                continue
            item = {
                "artifact_id": _artifact_identity_id(scope, workspace_id, identity_path),
                "content_sha256": actual[0],
                "path": str(host),
                "observed_path": normalized,
            }
        identity = (
            str(item["artifact_id"]),
            str(item["content_sha256"]),
            str(item["path"]),
        )
        if all(identity) and identity not in seen:
            seen.add(identity)
            resolved.append(item)
    return resolved


def _command_result_ref(
    result: ToolMessage | Command[Any] | None,
    *,
    tool_call_id: str,
    tool_name: str,
) -> dict[str, Any] | None:
    if result is None:
        return None
    messages = _result_messages(result, expected_call_id=tool_call_id)
    if not messages:
        return None
    content = "\n".join(str(getattr(item, "content", "") or "") for item in messages).strip()
    if not content:
        return None
    return {
        "kind": "tool_result",
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "output_digest": f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}",
        "output_preview": content[:1000],
    }


def _command_exit_code(result_ref: dict[str, Any] | None, *, succeeded: bool) -> int:
    preview = str((result_ref or {}).get("output_preview") or "")
    match = _COMMAND_EXIT_RE.search(preview) or _PLAIN_EXIT_RE.search(preview)
    if match is not None:
        return int(match.group("code"))
    return 0 if succeeded else 1


def _commit_validation_receipts(
    *,
    session_id: str,
    run_id: str,
    goal_id: str | None,
    goal_revision: int | None,
    args: dict[str, Any],
) -> list[dict[str, Any]]:
    """Carry the exact pre-commit receipts onto the committed artifact write."""

    requested_ids = {
        str(item)
        for item in (args.get("validation_receipt_ids") or [])
        if str(item)
    }
    target_path = str(args.get("file_path") or "").strip()
    draft_sha256 = str(args.get("expected_draft_sha256") or "").strip()
    if not session_id or not run_id or not requested_ids or not target_path or not draft_sha256:
        return []

    normalized_target = posixpath.normpath(target_path.replace("\\", "/"))
    candidates: dict[str, dict[str, Any]] = {}

    def collect(refs: Any) -> None:
        for ref in refs if isinstance(refs, list) else []:
            if not isinstance(ref, dict) or ref.get("kind") != "validation_receipt":
                continue
            receipt_id = str(ref.get("validation_receipt_id") or "")
            if receipt_id in requested_ids:
                candidates[receipt_id] = ref

    try:
        run = session_manager.get_run_state(session_id, run_id)
    except (AssertionError, FileNotFoundError):
        run = None
    if isinstance(run, dict):
        for activation in run.get("verification_activations") or []:
            if isinstance(activation, dict):
                collect(activation.get("evidence_refs"))
    if goal_id:
        try:
            goal = session_manager.get_goal_state(session_id, goal_id)
        except (AssertionError, FileNotFoundError):
            goal = None
        if isinstance(goal, dict) and goal.get("objective_revision") == goal_revision:
            collect(goal.get("evidence_refs"))

    carried: list[dict[str, Any]] = []
    for receipt_id in sorted(requested_ids):
        receipt = candidates.get(receipt_id)
        if receipt is None:
            continue
        if (
            str(receipt.get("status") or "passed") != "passed"
            or int(receipt.get("exit_code", -1)) != 0
            or int(receipt.get("checks_failed") or 0) != 0
        ):
            continue
        matches_draft = any(
            isinstance(ref, dict)
            and str(ref.get("content_sha256") or "") == draft_sha256
            and posixpath.normpath(str(ref.get("path") or "").replace("\\", "/"))
            == normalized_target
            for ref in receipt.get("artifact_refs") or []
        )
        if matches_draft:
            carried.append({**receipt, "material": True})
    return carried


def build_verification_activations(
    *,
    run_id: str,
    query_id: str,
    tool_call_id: str,
    tool_name: str,
    args: dict[str, Any] | None,
    result: ToolMessage | Command[Any] | None = None,
    session_id: str = "",
    workspace_path: str = "",
    goal_id: str | None = None,
    goal_revision: int | None = None,
) -> list[VerificationActivation]:
    normalized_args = args or {}
    preview = json.dumps(normalized_args, ensure_ascii=False, sort_keys=True)[:1000]
    succeeded = True if result is None else tool_result_succeeded(
        result, expected_call_id=tool_call_id
    )
    result_refs = _result_evidence_refs(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        args=normalized_args,
        result=result,
        session_id=session_id,
        run_id=run_id,
        query_id=query_id,
        workspace_path=workspace_path,
        goal_id=goal_id,
        goal_revision=goal_revision,
    )
    commit_receipts = (
        _commit_validation_receipts(
            session_id=session_id,
            run_id=run_id,
            goal_id=goal_id,
            goal_revision=goal_revision,
            args=normalized_args,
        )
        if tool_name == "commit_external_artifact" and succeeded
        else []
    )
    activations: list[VerificationActivation] = []
    packs = verification_packs_for_tool(tool_name, args)
    if tool_name == "commit_external_directory" and succeeded:
        written = [
            ref
            for ref in result_refs
            if ref.get("kind") == "artifact_write" and ref.get("path")
        ]
        packs = ["artifact"] if written else []
        if any(Path(str(ref["path"])).suffix.lower() in _CODE_EXTENSIONS for ref in written):
            packs.append("code")
    if not succeeded:
        # Failed business/network tools cannot activate acceptance packs. A
        # failed validator is different: it is durable blocking evidence for
        # the exact artifact bytes it actually inspected.
        packs = [pack for pack in packs if pack == "code"]
    has_web_source = any(
        item.get("kind") == "source"
        and (
            item.get("source_type") == "web"
            or str(item.get("uri") or "").startswith(("http://", "https://"))
        )
        for item in result_refs
    )
    if has_web_source and "web_research" not in packs:
        packs.append("web_research")
    for pack in packs:
        material = bool(result_refs)
        if pack == "analytics":
            material = material and (
                tool_name in _MATERIAL_ANALYTICS_TOOLS
                or tool_name in {"execute", "terminal"}
            )
        elif pack == "web_research":
            material = any(item.get("kind") == "source" for item in result_refs)
        elif pack == "artifact":
            material = tool_name in _WRITE_TOOLS and any(
                item.get("kind") == "artifact_write" for item in result_refs
            )
        elif pack == "code":
            if tool_name in _WRITE_TOOLS:
                material = any(
                    item.get("kind") == "artifact_write"
                    and item.get("role") != ArtifactRole.TEMPORARY.value
                    for item in result_refs
                )
            elif tool_name in {"execute", "terminal"}:
                material = True
        digest = hashlib.sha256(
            f"{run_id}:{query_id}:{tool_call_id}:{tool_name}:{pack}".encode()
        ).hexdigest()[:20]
        evidence_refs = [
            {
                "kind": "tool_execution",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "input_preview": preview,
                "material": material,
            },
            *[{**item, "material": material} for item in result_refs],
        ]
        if pack == "code" and material and commit_receipts:
            evidence_refs.extend(commit_receipts)
        if pack == "code" and material and session_id and result is not None:
            receipt = _validation_receipt_for_result(
                session_id=session_id,
                run_id=run_id,
                goal_id=goal_id,
                goal_revision=goal_revision,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                args=normalized_args,
                result=result,
                workspace_path=workspace_path,
            )
            if receipt is not None:
                evidence_refs.append(
                    {
                        "kind": "validation_receipt",
                        **receipt.model_dump(mode="json"),
                        "material": True,
                    }
                )
        activations.append(
            VerificationActivation(
                activation_id=f"verification-activation-{digest}",
                run_id=run_id,
                query_id=query_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                pack=pack,
                # The activation records that Harness successfully captured the
                # validator attempt. Pass/fail authority lives in the nested
                # ValidationReceipt, so it can use the existing append-only
                # successful-activation ledger without conflating the two.
                status="succeeded",
                evidence_refs=evidence_refs,
            )
        )
    return activations


def _validation_receipt_for_result(
    *,
    session_id: str,
    run_id: str,
    goal_id: str | None,
    goal_revision: int | None,
    tool_call_id: str,
    tool_name: str,
    args: dict[str, Any],
    result: ToolMessage | Command[Any],
    workspace_path: str,
) -> ValidationReceipt | None:
    if tool_name not in {"execute", "terminal"}:
        return None
    command = str(args.get("command") or args.get("cmd") or "")
    controlled_spec = _controlled_validator_spec(command)
    lowered = command.lower()
    if controlled_spec is not None:
        validator_kind, validator_version = controlled_spec
    elif re.search(r"(?:^|\s)node\s+(?:--check|-c)(?:\s|$)", lowered):
        validator_kind = "javascript_syntax"
    elif re.search(r"(?:pytest|\btest\b|npm\s+(?:run\s+)?test)", lowered):
        validator_kind = "project_test"
    elif ".html" in lowered and re.search(r"validate|validator|check", lowered):
        validator_kind = "html_structure"
    else:
        validator_kind = "static_check"
    if controlled_spec is None:
        validator_version = "command-receipt/v1"

    output_ref = _command_result_ref(
        result,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
    )
    if output_ref is None:
        return None
    succeeded = tool_result_succeeded(result, expected_call_id=tool_call_id)
    exit_code = _command_exit_code(output_ref, succeeded=succeeded)
    output_preview = str(output_ref.get("output_preview") or "")
    passed_match = re.search(
        r"(?P<passed>\d+)\s*(?:/\s*(?P<total>\d+)\s*)?(?:checks?\s+)?passed",
        output_preview,
        re.IGNORECASE,
    )
    checks_passed = int(passed_match.group("passed")) if passed_match else None
    artifact_refs = _validation_artifact_refs(
        session_id=session_id,
        run_id=run_id,
        command=command,
        workspace_path=workspace_path,
    )
    receipt_digest = hashlib.sha256(
        json.dumps(
            {
                "run_id": run_id,
                "tool_call_id": tool_call_id,
                "validator_kind": validator_kind,
                "artifacts": artifact_refs,
                "status": "passed" if succeeded else "failed",
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:20]
    return ValidationReceipt(
        validation_receipt_id=f"validation-{receipt_digest}",
        run_id=run_id,
        goal_id=goal_id,
        goal_revision=goal_revision,
        validator_kind=validator_kind,
        validator_version=validator_version,
        artifact_refs=artifact_refs,
        command_evidence_ref=str(output_ref.get("output_digest") or ""),
        exit_code=exit_code,
        checks_passed=checks_passed,
        checks_failed=0 if succeeded else 1,
        status="passed" if succeeded else "failed",
        blocking=True,
        commit_authority=controlled_spec is not None,
        obligation_key=f"{validator_kind}:{validator_version}",
    )


class VerificationActivationMiddleware(AgentMiddleware):
    """Record successful main/subagent Tool executions in the Run authority."""

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[
            [ToolCallRequest],
            ToolMessage | Command[Any],
        ],
    ) -> ToolMessage | Command[Any]:
        result = handler(request)
        self._record(request, result)
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[
            [ToolCallRequest],
            Awaitable[ToolMessage | Command[Any]],
        ],
    ) -> ToolMessage | Command[Any]:
        result = await handler(request)
        self._record(request, result)
        return result

    @staticmethod
    def _record(
        request: ToolCallRequest,
        result: ToolMessage | Command[Any],
    ) -> None:
        runtime_context = getattr(request.runtime, "context", None)
        context = runtime_context if isinstance(runtime_context, dict) else {}
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        query_id = str(context.get("query_id") or "")
        tool_call_id = str(request.tool_call.get("id") or "")
        tool_name = str(request.tool_call.get("name") or "")
        if not all((session_id, run_id, query_id, tool_call_id, tool_name)):
            return
        args = request.tool_call.get("args")
        normalized_args = args if isinstance(args, dict) else {}
        succeeded = tool_result_succeeded(result, expected_call_id=tool_call_id)
        mutation = tool_name in _PROPORTIONAL_MUTATION_TOOLS
        if tool_name in {"execute", "terminal"}:
            command = str(normalized_args.get("command") or normalized_args.get("cmd") or "")
            capabilities = ShellPolicyAnalyzer.capabilities(
                command,
                workspace_path=str(context.get("workspace_path") or "."),
            )
            mutation = bool(
                capabilities.workspace_write
                or capabilities.package_install
                or capabilities.destructive
            )
        if succeeded and mutation:
            try:
                session_manager.upgrade_run_verification_mode(
                    session_id,
                    run_id,
                    "proportional",
                )
            except (FileNotFoundError, ValueError):
                pass
        if not succeeded and not (
            tool_name in {"execute", "terminal"}
            and "code" in verification_packs_for_tool(tool_name, normalized_args)
        ):
            return
        writer = getattr(request.runtime, "stream_writer", None)
        for activation in build_verification_activations(
            run_id=run_id,
            query_id=query_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args=normalized_args,
            result=result,
            session_id=session_id,
            workspace_path=str(context.get("workspace_path") or ""),
            goal_id=(str(context.get("goal_id")) if context.get("goal_id") else None),
            goal_revision=(
                int(context.get("goal_revision"))
                if context.get("goal_revision") is not None
                else None
            ),
        ):
            try:
                saved, created = session_manager.append_run_verification_activation(
                    session_id,
                    run_id,
                    activation.model_dump(mode="json"),
                )
            except (FileNotFoundError, ValueError):
                continue
            if created and writer is not None:
                writer(
                    {
                        "type": "verification_activation_recorded",
                        "activation": saved,
                    }
                )


__all__ = [
    "VerificationActivationMiddleware",
    "build_verification_activations",
    "tool_result_succeeded",
    "verification_packs_for_tool",
]
