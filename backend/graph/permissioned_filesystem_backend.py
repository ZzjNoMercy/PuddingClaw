"""DeepAgents filesystem backend with exact-file external write grants."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import shutil
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.backends.protocol import (
    EditResult,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)

from graph.host_file_broker import HostFileBroker
from graph.session_manager import session_manager


class _CandidateHTMLValidator(HTMLParser):
    """Small deterministic balance check for pre-commit HTML candidates."""

    _VOID = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.tags: set[str] = set()
        self.ids: set[str] = set()
        self.duplicate_ids: set[str] = set()
        self.local_resource_refs: list[str] = []

    def _record_attrs(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_attrs = {
            str(key).lower(): str(value or "")
            for key, value in attrs
        }
        element_id = normalized_attrs.get("id", "").strip()
        if element_id:
            if element_id in self.ids:
                self.duplicate_ids.add(element_id)
            self.ids.add(element_id)
        resource = (
            normalized_attrs.get("src", "")
            if tag == "script"
            else normalized_attrs.get("href", "")
            if tag == "link"
            else ""
        ).strip()
        if (
            resource
            and not resource.startswith(("#", "//"))
            and "://" not in resource
            and not resource.lower().startswith(("data:", "javascript:"))
        ):
            self.local_resource_refs.append(resource)

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized = tag.lower()
        self.tags.add(normalized)
        self._record_attrs(normalized, attrs)
        if normalized not in self._VOID:
            self.stack.append(normalized)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized = tag.lower()
        self.tags.add(normalized)
        self._record_attrs(normalized, attrs)
        return

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in self._VOID:
            return
        if not self.stack or self.stack[-1] != normalized:
            self.errors.append(f"unexpected closing tag </{normalized}>")
            return
        self.stack.pop()


class PermissionedCompositeBackend(CompositeBackend):
    """Delegate approved external writes while keeping normal virtual routing."""

    _ROUTED_VIRTUAL_PREFIXES = (
        "/workspace/",
        "/scratch/",
        "/large_tool_results/",
    )
    _READONLY_VIRTUAL_PREFIXES = (
        "/knowledge/",
        "/semantic-assets/",
        "/sql-guardrails/",
        "/analytics-models/",
        "/skills/",
    )

    def __init__(
        self,
        *,
        default: Any,
        routes: dict[str, Any],
        session_id: str,
        managed_readonly_roots: tuple[Path, ...] = (),
        workspace_root: Path | None = None,
        run_id: str = "",
        query_id: str = "",
    ) -> None:
        super().__init__(default=default, routes=routes)
        self.session_id = session_id
        self.run_id = run_id
        self.query_id = query_id
        self.host_file_broker = (
            HostFileBroker(
                session_id=session_id,
                run_id=run_id,
                query_id=query_id,
                validation_runner=self._validate_external_candidate,
            )
            if session_id and run_id
            else None
        )
        self.managed_readonly_roots = tuple(root.expanduser().resolve() for root in managed_readonly_roots)
        resolved_workspace = workspace_root.expanduser().resolve() if workspace_root is not None else None
        self.workspace_root = resolved_workspace
        workspace_prefixes: list[str] = []
        if resolved_workspace is not None:
            for root in self.managed_readonly_roots:
                try:
                    relative = root.relative_to(resolved_workspace).as_posix().strip("/")
                except ValueError:
                    continue
                if relative:
                    workspace_prefixes.append(f"/workspace/{relative}/")
        self._readonly_workspace_prefixes = tuple(workspace_prefixes)
        self.external_directory_writable_enabled = False

    def _validate_external_candidate(
        self,
        target: Any,
        content: bytes,
    ) -> dict[str, Any] | None:
        """Validate code-like host bytes inside the existing execution backend."""

        suffix = target.canonical_path.suffix.lower()
        validator: tuple[str, str, str] | None = None
        if suffix in {".js", ".mjs", ".cjs"}:
            validator = ("javascript_syntax", "node-check/v1", "node --check")
        elif suffix == ".py":
            validator = ("static_check", "python-py-compile/v1", "python3 -m py_compile")
        elif suffix == ".json":
            validator = ("json_structure", "python-json-tool/v1", "python3 -m json.tool")
        elif suffix in {".html", ".htm"}:
            validator = (
                "html_structure",
                "python-html-balance/v1",
                "__internal_html__",
            )
        if validator is None:
            return None

        content_sha256 = f"sha256:{hashlib.sha256(content).hexdigest()}"
        digest = content_sha256.removeprefix("sha256:")
        safe_name = "".join(
            character
            for character in target.canonical_path.name
            if character.isalnum() or character in "._-"
        ) or f"candidate{suffix}"
        validator_kind, validator_version, command_prefix = validator
        if command_prefix == "__internal_html__":
            virtual_path = "internal://html-candidate"
            try:
                text = content.decode("utf-8")
                parser = _CandidateHTMLValidator()
                parser.feed(text)
                parser.close()
                if parser.stack:
                    parser.errors.append(
                        "unclosed tags: " + ", ".join(parser.stack[-10:])
                    )
                output = (
                    "; ".join(parser.errors)
                    if parser.errors
                    else "html structure ok"
                )
                exit_code = 1 if parser.errors else 0
            except (UnicodeDecodeError, ValueError) as exc:
                output = f"invalid HTML candidate: {exc}"
                exit_code = 1
        else:
            execution_backend = getattr(self, "execution_backend", None)
            scratch_root = str(
                getattr(self, "execution_scratch_host_path", "") or ""
            )
            execution_error: Exception | None = None
            if execution_backend is None or not scratch_root:
                execution_error = RuntimeError(
                    "registered validation infrastructure is unavailable"
                )
                virtual_path = "unavailable://validation-candidate"
                output = str(execution_error)
                exit_code = 1
            else:
                relative = Path("validation") / digest / safe_name
                host_path = Path(scratch_root) / relative
                host_path.parent.mkdir(parents=True, exist_ok=True)
                host_path.write_bytes(content)
                virtual_path = f"/scratch/{relative.as_posix()}"
                command = f"{command_prefix} {shlex.quote(virtual_path)}"
                try:
                    result = execution_backend.execute(command, timeout=60)
                except Exception as exc:  # execution boundary, not artifact bytes
                    result = None
                    execution_error = exc
                finally:
                    shutil.rmtree(host_path.parent, ignore_errors=True)
                    try:
                        host_path.parent.parent.rmdir()
                    except OSError:
                        pass
                output = (
                    f"validation infrastructure error: {execution_error}"
                    if execution_error is not None
                    else str(getattr(result, "output", "") or "")
                )
                raw_exit_code = getattr(result, "exit_code", None)
                if not isinstance(raw_exit_code, int):
                    execution_error = RuntimeError(
                        "validation backend returned no exit code"
                    )
                    exit_code = 1
                else:
                    exit_code = raw_exit_code
        failure_class: str | None = None
        content_observed = True
        if exit_code != 0:
            lowered_output = output.lower()
            if (
                command_prefix != "__internal_html__"
                and (
                    execution_error is not None
                    or exit_code in {124, 126, 127, 137}
                    or "timed out" in lowered_output
                    or "infrastructure error" in lowered_output
                    or "command not found" in lowered_output
                    or "out of memory" in lowered_output
                    or "killed" in lowered_output
                )
            ):
                failure_class = "infrastructure_failure"
                content_observed = False
            elif (
                "cannot find module" in lowered_output
                or "module_not_found" in lowered_output
                or "no such file" in lowered_output
                or "enoent" in lowered_output
            ):
                failure_class = "invocation_failure"
                content_observed = False
            else:
                failure_class = "artifact_failure"
        receipt_seed = json.dumps(
            {
                "run_id": self.run_id,
                "path": str(target.canonical_path),
                "content_sha256": content_sha256,
                "validator": validator_version,
            },
            sort_keys=True,
        )
        receipt_id = "validation-" + hashlib.sha256(
            receipt_seed.encode("utf-8")
        ).hexdigest()[:20]
        artifact_id = "artifact-" + hashlib.sha256(
            f"external\0{target.canonical_path}".encode()
        ).hexdigest()[:20]
        return {
            "kind": "validation_receipt",
            "validation_receipt_id": receipt_id,
            "run_id": self.run_id,
            "validator_kind": validator_kind,
            "validator_version": validator_version,
            "artifact_refs": [
                {
                    "artifact_id": artifact_id,
                    "path": str(target.canonical_path),
                    "content_sha256": content_sha256,
                }
            ],
            "command_evidence_ref": "sha256:"
            + hashlib.sha256(output.encode("utf-8")).hexdigest(),
            "exit_code": exit_code,
            "checks_passed": 1 if exit_code == 0 else 0,
            "checks_failed": 0 if exit_code == 0 else 1,
            "status": "passed" if exit_code == 0 else "failed",
            "failure_class": failure_class,
            "content_observed": content_observed,
            "blocking": True,
            "commit_authority": True,
            "obligation_key": f"{validator_kind}:{validator_version}",
            "summary": output[:2000],
            "materialized_path": virtual_path,
            "temporary_materialization": True,
        }

    def rewind_external_file_changes(self) -> dict[str, Any]:
        if self.host_file_broker is None:
            return {
                "status": "permission_required",
                "error": "permission_required: no active HostFileBroker Run",
                "rewound_receipt_ids": [],
            }
        return self.host_file_broker.rewind_run()

    def apply_external_file_transaction(
        self,
        changes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self.host_file_broker is None:
            return {
                "status": "permission_required",
                "error": "permission_required: no active HostFileBroker Run",
            }
        return self.host_file_broker.apply_transaction(changes)

    def replace_external_file(
        self,
        file_path: str,
        content: bytes,
        *,
        expected_sha256: str,
        operation: str = "replace",
    ) -> dict[str, Any]:
        if self._managed_readonly(file_path):
            return {
                "status": "permission_required",
                "error_code": "managed_resource_read_only",
                "error": f"permission_required: managed resource is read-only: {file_path}",
            }
        if self.host_file_broker is None:
            return {
                "status": "permission_required",
                "error_code": "host_file_broker_unavailable",
                "error": "permission_required: no active HostFileBroker Run",
            }
        return self.host_file_broker.replace(
            file_path,
            content,
            expected_sha256=expected_sha256,
            operation=operation,
        )

    def create_external_file(
        self,
        file_path: str,
        content: bytes,
        *,
        operation: str = "create",
    ) -> dict[str, Any]:
        if self._managed_readonly(file_path):
            return {
                "status": "permission_required",
                "error_code": "managed_resource_read_only",
                "error": f"permission_required: managed resource is read-only: {file_path}",
            }
        if self.host_file_broker is None:
            return {
                "status": "permission_required",
                "error_code": "host_file_broker_unavailable",
                "error": "permission_required: no active HostFileBroker Run",
            }
        return self.host_file_broker.create(
            file_path,
            content,
            operation=operation,
        )

    def copy_external_file(
        self,
        source_path: str,
        target_path: str,
        *,
        expected_source_sha256: str | None = None,
    ) -> dict[str, Any]:
        if self._managed_readonly(target_path):
            return {
                "status": "permission_required",
                "error_code": "managed_resource_read_only",
                "error": f"permission_required: managed resource is read-only: {target_path}",
            }
        if self.host_file_broker is None:
            return {
                "status": "permission_required",
                "error_code": "host_file_broker_unavailable",
                "error": "permission_required: no active HostFileBroker Run",
            }
        if self._routed_virtual_path(target_path):
            content, source_result = self.host_file_broker.load_authorized_file(
                source_path,
                expected_sha256=expected_source_sha256,
            )
            if content is None:
                return source_result
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                return {
                    **source_result,
                    "status": "io_error",
                    "error_code": "workspace_copy_requires_utf8",
                    "error": "io_error: virtual-workspace files must be UTF-8 text",
                    "next_action": "use_a_binary_artifact_channel",
                }
            written = super().write(target_path, text)
            if written.error is not None:
                target_exists = "already exists" in written.error.lower()
                return {
                    **source_result,
                    "status": "conflict" if target_exists else "io_error",
                    "error_code": (
                        "target_already_exists"
                        if target_exists
                        else "workspace_write_failed"
                    ),
                    "error": written.error,
                    "next_action": (
                        "use_patch_file"
                        if target_exists
                        else "report_infrastructure_error"
                    ),
                }
            return {
                **source_result,
                "status": "completed",
                "target_path": target_path,
                "target_sha256": source_result["source_sha256"],
                "authority_kind": "virtual_workspace",
            }
        return self.host_file_broker.copy(
            source_path,
            target_path,
            expected_source_sha256=expected_source_sha256,
        )

    def delete_external_file(
        self,
        file_path: str,
        *,
        expected_sha256: str,
    ) -> dict[str, Any]:
        if self._managed_readonly(file_path):
            return {
                "status": "permission_required",
                "error": f"permission_required: managed resource is read-only: {file_path}",
            }
        if self.host_file_broker is None:
            return {
                "status": "permission_required",
                "error": "permission_required: no active HostFileBroker Run",
            }
        return self.host_file_broker.delete(
            file_path,
            expected_sha256=expected_sha256,
        )

    def execute_external_directory_command(
        self,
        directory_path: str,
        command: str,
        *,
        timeout: int,
        mode: str = "read_only",
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Run one command against a read-only root or isolated writable draft."""

        if self.host_file_broker is None:
            return {
                "status": "permission_required",
                "error": "permission_required: no active HostFileBroker Run",
            }
        try:
            directory = Path(directory_path).expanduser().resolve(strict=True)
        except OSError as exc:
            return {"status": "io_error", "error": f"io_error: {exc}"}
        access = "write" if mode == "writable_draft" else "read"
        if mode not in {"read_only", "writable_draft"}:
            return {
                "status": "io_error",
                "error_code": "unsupported_external_directory_mode",
                "error": f"io_error: unsupported mode {mode}",
                "next_action": "choose_read_only_or_writable_draft",
            }
        if (
            mode == "writable_draft"
            and not self.external_directory_writable_enabled
        ):
            return {
                "status": "permission_required",
                "error_code": "external_directory_writable_disabled",
                "error": (
                    "permission_required: writable external-directory drafts "
                    "are disabled by the deployment feature flag"
                ),
                "next_action": "use_copy_replace_or_enable_feature_flag",
            }
        if not directory.is_dir() or not self.host_file_broker.authorize(
            directory,
            access=access,
        ):
            return {
                "status": "permission_required",
                "error": (
                    f"permission_required: exact-directory {access} permission is required; "
                    "file permission never grants a shell mount"
                ),
            }
        execution_backend = getattr(self, "execution_backend", None)
        execute = getattr(execution_backend, "execute_external_directory", None)
        if not callable(execute):
            return {
                "status": "io_error",
                "error": "io_error: external directory commands require the Docker backend",
            }
        execution_directory = directory
        lease: dict[str, Any] | None = None
        if mode == "writable_draft":
            lease = session_manager.get_external_directory_lease(
                self.session_id,
                str(lease_id or ""),
            )
            if not isinstance(lease, dict):
                return {
                    "status": "conflict",
                    "error_code": "directory_lease_required",
                    "error": "conflict: writable_draft requires a valid staged lease",
                    "next_action": "stage_external_directory",
                }
            if (
                str(lease.get("directory_path") or "") != str(directory)
                or str(lease.get("status") or "") not in {"staged", "prepared"}
                or (
                    str(lease.get("goal_id") or "")
                    and str(lease.get("goal_id") or "")
                    != str(
                        (
                            session_manager.get_run_state(
                                self.session_id,
                                self.run_id,
                            )
                            or {}
                        ).get("goal_id")
                        or ""
                    )
                )
                or (
                    not str(lease.get("goal_id") or "")
                    and str(lease.get("run_id") or "") != self.run_id
                )
            ):
                return {
                    "status": "conflict",
                    "error_code": "directory_lease_binding_mismatch",
                    "error": "conflict: directory lease is not bound to this Run/Goal/path",
                    "next_action": "stage_external_directory",
                }
            scratch_root_raw = str(
                getattr(self, "execution_scratch_host_path", "") or ""
            )
            staged_virtual = str(lease.get("staged_dir") or "")
            if (
                not scratch_root_raw
                or not staged_virtual.startswith("/scratch/")
            ):
                return {
                    "status": "io_error",
                    "error_code": "directory_draft_unavailable",
                    "error": "io_error: staged directory has no execution mapping",
                    "next_action": "stage_external_directory",
                }
            scratch_root = Path(scratch_root_raw).resolve(strict=True)
            execution_directory = (
                scratch_root / staged_virtual.removeprefix("/scratch/")
            ).resolve(strict=True)
            try:
                execution_directory.relative_to(scratch_root)
            except ValueError:
                return {
                    "status": "io_error",
                    "error_code": "directory_draft_escape",
                    "error": "io_error: staged directory escaped scratch root",
                    "next_action": "report_infrastructure_error",
                }
            if not execution_directory.is_dir():
                return {
                    "status": "io_error",
                    "error_code": "directory_draft_unavailable",
                    "error": "io_error: staged directory is missing",
                    "next_action": "stage_external_directory",
                }
        execute_kwargs: dict[str, Any] = {"timeout": timeout}
        if mode == "writable_draft":
            execute_kwargs["writable"] = True
        response = execute(
            str(execution_directory),
            command,
            **execute_kwargs,
        )
        exit_code = getattr(response, "exit_code", None)
        draft_plan: dict[str, list[str]] | None = None
        if mode == "writable_draft" and exit_code == 0 and lease is not None:
            from graph.middlewares.external_directory import (
                _scan_source_directory,
            )

            staged_manifest, _contents, _skipped, scan_error = (
                _scan_source_directory(
                    execution_directory,
                    include_content=False,
                )
            )
            if scan_error is not None:
                return {
                    "status": "io_error",
                    "error_code": "directory_draft_scan_failed",
                    "error": f"io_error: {scan_error}",
                    "next_action": "discard_and_restage_directory",
                }
            source_manifest = dict(lease.get("source_manifest") or {})
            source_paths = set(source_manifest)
            staged_paths = set(staged_manifest)
            draft_plan = {
                "added": sorted(staged_paths - source_paths),
                "modified": sorted(
                    path
                    for path in source_paths & staged_paths
                    if str(source_manifest[path].get("sha256") or "")
                    != str(staged_manifest[path].get("sha256") or "")
                ),
                "deleted": sorted(source_paths - staged_paths),
            }
            lease.update(
                {
                    "status": "staged",
                    "draft_dirty": any(draft_plan.values()),
                    "draft_plan_preview": draft_plan,
                }
            )
            lease.pop("commit_plan", None)
            session_manager.upsert_external_directory_lease(
                self.session_id,
                lease,
            )
        result = {
            "status": "completed" if exit_code == 0 else "io_error",
            "directory_path": str(directory),
            "read_only": mode == "read_only",
            "ephemeral": True,
            "exit_code": exit_code,
            "output": str(getattr(response, "output", "") or ""),
            "truncated": bool(getattr(response, "truncated", False)),
        }
        if mode == "writable_draft":
            result.update(
                {
                    "mode": mode,
                    "lease_id": str(lease_id or ""),
                    "draft_plan_preview": draft_plan,
                    "next_action": (
                        "prepare_external_directory_commit"
                        if exit_code == 0
                        else "discard_and_restage_directory"
                    ),
                }
            )
        return result

    def validate_html_report(
        self,
        html_file_path: str,
        *,
        browser_e2e: bool | None,
        timeout: int,
    ) -> dict[str, Any]:
        """Validate HTML proportionally; start Chromium only when contracted."""

        requested = Path(html_file_path).expanduser()
        if not requested.is_absolute() or requested.suffix.lower() not in {
            ".html",
            ".htm",
        }:
            return {
                "status": "invocation_error",
                "error_code": "invalid_html_report_path",
                "failure_class": "invocation_failure",
                "error": "html_file_path must be an absolute .html/.htm file",
            }
        try:
            canonical = requested.resolve(strict=True)
        except OSError as exc:
            return {
                "status": "invocation_error",
                "error_code": "html_report_not_found",
                "failure_class": "invocation_failure",
                "error": str(exc),
            }
        if not canonical.is_file():
            return {
                "status": "invocation_error",
                "error_code": "html_report_not_regular_file",
                "failure_class": "invocation_failure",
                "error": f"not a regular file: {canonical}",
            }

        run_payload = (
            session_manager.get_run_state(self.session_id, self.run_id)
            if self.session_id and self.run_id
            else None
        )
        verification_contract = (
            run_payload.get("verification_contract")
            if isinstance(run_payload, dict)
            and isinstance(run_payload.get("verification_contract"), dict)
            else {}
        )
        contracted_browser_e2e = bool(
            verification_contract.get("browser_e2e_required")
        )
        requested_browser_e2e = (
            contracted_browser_e2e
            if browser_e2e is None
            else bool(browser_e2e)
        )
        if requested_browser_e2e != contracted_browser_e2e:
            return {
                "status": "invocation_error",
                "error_code": "html_validation_mode_contract_mismatch",
                "failure_class": "invocation_failure",
                "error": (
                    "browser_e2e must match the server-authored "
                    f"verification contract ({contracted_browser_e2e})"
                ),
                "html_file_path": str(canonical),
                "browser_e2e_required": contracted_browser_e2e,
            }

        if not contracted_browser_e2e:
            failures: list[str] = []
            try:
                text = canonical.read_text(encoding="utf-8")
                parser = _CandidateHTMLValidator()
                parser.feed(text)
                parser.close()
                failures.extend(parser.errors)
                if parser.stack:
                    failures.append(
                        "unclosed tags: " + ", ".join(parser.stack[-10:])
                    )
                if "html" not in parser.tags or "body" not in parser.tags:
                    failures.append(
                        "full HTML report must contain <html> and <body>"
                    )
                if parser.duplicate_ids:
                    failures.append(
                        "duplicate element ids: "
                        + ", ".join(sorted(parser.duplicate_ids)[:20])
                    )
                missing_resources: list[str] = []
                for raw_ref in parser.local_resource_refs:
                    relative_ref = raw_ref.split("#", 1)[0].split("?", 1)[0]
                    if not relative_ref:
                        continue
                    referenced = (
                        Path(relative_ref)
                        if Path(relative_ref).is_absolute()
                        else canonical.parent / relative_ref
                    )
                    if not referenced.is_file():
                        missing_resources.append(raw_ref)
                if missing_resources:
                    failures.append(
                        "missing local resources: "
                        + ", ".join(sorted(dict.fromkeys(missing_resources))[:20])
                    )
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                failures.append(f"invalid HTML report: {exc}")
                parser = None
            structure_output = {
                "passed": not failures,
                "mode": "structure",
                "html_file_path": str(canonical),
                "failures": failures,
                "element_id_count": len(parser.ids) if parser is not None else 0,
                "local_resource_count": (
                    len(parser.local_resource_refs)
                    if parser is not None
                    else 0
                ),
            }
            return {
                "status": "completed" if not failures else "io_error",
                "html_file_path": str(canonical),
                "validator_kind": "html_structure",
                "validator_version": "puddingclaw-html-structure/v1",
                "browser_e2e_required": False,
                "exit_code": 0 if not failures else 1,
                "output": json.dumps(
                    structure_output,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                **(
                    {}
                    if not failures
                    else {"failure_class": "artifact_failure"}
                ),
            }

        command_prefix = (
            "node /opt/puddingclaw/bin/validate-html-report-e2e.mjs "
        )
        response_payload: dict[str, Any]
        workspace = self.workspace_root
        in_workspace = False
        relative: Path | None = None
        if workspace is not None:
            try:
                relative = canonical.relative_to(workspace)
                in_workspace = True
            except ValueError:
                pass
        if in_workspace and relative is not None:
            execution_backend = getattr(self, "execution_backend", None)
            execute = getattr(execution_backend, "execute", None)
            if not callable(execute):
                response_payload = {
                    "status": "infrastructure_error",
                    "error_code": "html_validator_backend_unavailable",
                    "failure_class": "infrastructure_failure",
                    "error": "HTML browser validation requires the Docker backend",
                }
            else:
                response = execute(
                    command_prefix
                    + shlex.quote(f"/workspace/{relative.as_posix()}"),
                    timeout=timeout,
                )
                exit_code = getattr(response, "exit_code", None)
                response_payload = {
                    "status": "completed" if exit_code == 0 else "io_error",
                    "workspace_path": str(workspace),
                    "read_only": True,
                    "ephemeral": False,
                    "exit_code": exit_code,
                    "output": str(getattr(response, "output", "") or ""),
                    "truncated": bool(getattr(response, "truncated", False)),
                }
        else:
            response_payload = self.execute_external_directory_command(
                str(canonical.parent),
                command_prefix + shlex.quote(canonical.name),
                timeout=timeout,
                mode="read_only",
            )

        response_payload["html_file_path"] = str(canonical)
        response_payload["validator_kind"] = "browser_runtime"
        response_payload["validator_version"] = "puddingclaw-html-e2e/v1"
        response_payload["browser_e2e_required"] = True
        if str(response_payload.get("status") or "") != "completed":
            output = str(response_payload.get("output") or "")
            lowered = output.lower()
            if (
                str(response_payload.get("status") or "")
                == "permission_required"
                or "err_file_not_found" in lowered
                or "no such file" in lowered
            ):
                failure_class = "invocation_failure"
            elif (
                int(response_payload.get("exit_code") or 0) == 124
                or "timed out" in lowered
                or not output.lstrip().startswith("{")
            ):
                failure_class = "infrastructure_failure"
            else:
                failure_class = "artifact_failure"
            response_payload["failure_class"] = failure_class
        return response_payload

    def _readonly_virtual_path(self, file_path: str) -> bool:
        normalized = file_path.replace("\\", "/")
        return any(
            normalized == prefix.rstrip("/") or normalized.startswith(prefix)
            for prefix in (*self._READONLY_VIRTUAL_PREFIXES, *self._readonly_workspace_prefixes)
        )

    def _readonly_host_path(self, file_path: str) -> bool:
        requested = Path(file_path).expanduser()
        if not requested.is_absolute():
            return False
        try:
            resolved = requested.resolve()
        except OSError:
            return True
        for root in self.managed_readonly_roots:
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _managed_readonly(self, file_path: str) -> bool:
        return self._readonly_virtual_path(file_path) or self._readonly_host_path(file_path)

    def _unrouted_external_path(self, file_path: str | None) -> bool:
        """Identify a host absolute path that no normal Backend may touch.

        WorkspacePathRouter normally resolves this boundary before dispatch,
        but the Backend remains the final authority. An ungranted absolute
        path must never fall through to FilesystemBackend, whose root handling
        is not a permission decision.
        """

        if not file_path or self._routed_virtual_path(file_path) or self._managed_readonly(file_path):
            return False
        requested = Path(file_path).expanduser()
        if not requested.is_absolute():
            return False
        # DeepAgents' default virtual backend also uses root-absolute paths
        # (for example ``/dashboard.html``). Preserve that namespace when the
        # corresponding project target or its parent exists. Real host paths
        # outside the workspace have no such project projection and remain
        # fail-closed below.
        if self.workspace_root is not None:
            virtual_candidate = self.workspace_root / file_path.lstrip("/")
            if virtual_candidate.exists() or virtual_candidate.parent.exists():
                return False
        try:
            resolved = requested.resolve(strict=False)
        except OSError:
            return True
        if self.workspace_root is not None:
            try:
                resolved.relative_to(self.workspace_root)
                return False
            except ValueError:
                pass
        return True

    @staticmethod
    def _permission_error(file_path: str) -> str:
        return (
            "permission_required: external host path is not covered by an "
            f"effective file Grant: {file_path}"
        )

    @classmethod
    def _routed_virtual_path(cls, file_path: str) -> bool:
        """Return whether CompositeBackend, not host grants, owns this path."""

        normalized = file_path.replace("\\", "/")
        return any(
            normalized == prefix.rstrip("/") or normalized.startswith(prefix)
            for prefix in cls._ROUTED_VIRTUAL_PREFIXES
        )

    def _approved_external_target(self, file_path: str) -> tuple[FilesystemBackend, str, str] | None:
        if not self.session_id:
            return None
        if self._routed_virtual_path(file_path) or self._managed_readonly(file_path):
            return None
        requested = Path(file_path).expanduser()
        if not requested.is_absolute():
            return None
        resolved = requested.resolve()
        if not session_manager.has_external_file_write_permission(self.session_id, resolved):
            return None
        backend = FilesystemBackend(root_dir=resolved.parent, virtual_mode=True)
        return backend, f"/{resolved.name}", str(resolved)

    def _approved_external_read_target(self, file_path: str) -> tuple[FilesystemBackend, str] | None:
        if (
            not self.session_id
            or self._routed_virtual_path(file_path)
            or self._managed_readonly(file_path)
        ):
            return None
        requested = Path(file_path).expanduser()
        if not requested.is_absolute():
            return None
        resolved = requested.resolve()
        if not session_manager.has_external_file_read_permission(self.session_id, resolved):
            return None
        return FilesystemBackend(root_dir=resolved.parent, virtual_mode=True), f"/{resolved.name}"

    @staticmethod
    def _restore_external_path(result: Any, resolved: str):
        if result.path is not None:
            result.path = resolved
        return result

    def write(self, file_path: str, content: str):
        if self._managed_readonly(file_path):
            return WriteResult(error=f"Managed resource is read-only: {file_path}")
        if self.host_file_broker is not None:
            broker_result = self.host_file_broker.write(file_path, content)
            if broker_result is not None:
                return broker_result
            if self._unrouted_external_path(file_path):
                return WriteResult(error=self._permission_error(file_path))
        target = self._approved_external_target(file_path)
        if target is None:
            if self._unrouted_external_path(file_path):
                return WriteResult(error=self._permission_error(file_path))
            return super().write(file_path, content)
        backend, backend_path, resolved = target
        result = backend.write(backend_path, content)
        return self._restore_external_path(result, resolved)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000):
        if self.host_file_broker is not None:
            broker_result = self.host_file_broker.read(
                file_path,
                offset=offset,
                limit=limit,
            )
            if broker_result is not None:
                return broker_result
            if self._unrouted_external_path(file_path):
                return ReadResult(error=self._permission_error(file_path))
        target = self._approved_external_read_target(file_path)
        if target is None:
            if self._unrouted_external_path(file_path):
                return ReadResult(error=self._permission_error(file_path))
            return super().read(file_path, offset=offset, limit=limit)
        backend, backend_path = target
        return backend.read(backend_path, offset=offset, limit=limit)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000):
        if self.host_file_broker is not None:
            broker_result = await asyncio.to_thread(
                self.host_file_broker.read,
                file_path,
                offset=offset,
                limit=limit,
            )
            if broker_result is not None:
                return broker_result
            if self._unrouted_external_path(file_path):
                return ReadResult(error=self._permission_error(file_path))
        target = self._approved_external_read_target(file_path)
        if target is None:
            if self._unrouted_external_path(file_path):
                return ReadResult(error=self._permission_error(file_path))
            return await super().aread(file_path, offset=offset, limit=limit)
        backend, backend_path = target
        return await backend.aread(backend_path, offset=offset, limit=limit)

    async def awrite(self, file_path: str, content: str):
        if self._managed_readonly(file_path):
            return WriteResult(error=f"Managed resource is read-only: {file_path}")
        if self.host_file_broker is not None:
            broker_result = await asyncio.to_thread(
                self.host_file_broker.write,
                file_path,
                content,
            )
            if broker_result is not None:
                return broker_result
            if self._unrouted_external_path(file_path):
                return WriteResult(error=self._permission_error(file_path))
        target = self._approved_external_target(file_path)
        if target is None:
            if self._unrouted_external_path(file_path):
                return WriteResult(error=self._permission_error(file_path))
            return await super().awrite(file_path, content)
        backend, backend_path, resolved = target
        result = await backend.awrite(backend_path, content)
        return self._restore_external_path(result, resolved)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ):
        if self._managed_readonly(file_path):
            return EditResult(error=f"Managed resource is read-only: {file_path}")
        if self.host_file_broker is not None:
            broker_result = self.host_file_broker.edit(
                file_path,
                old_string,
                new_string,
                replace_all=replace_all,
            )
            if broker_result is not None:
                return broker_result
            if self._unrouted_external_path(file_path):
                return EditResult(error=self._permission_error(file_path))
        target = self._approved_external_target(file_path)
        if target is None:
            if self._unrouted_external_path(file_path):
                return EditResult(error=self._permission_error(file_path))
            return super().edit(file_path, old_string, new_string, replace_all=replace_all)
        backend, backend_path, resolved = target
        result = backend.edit(backend_path, old_string, new_string, replace_all=replace_all)
        return self._restore_external_path(result, resolved)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ):
        if self._managed_readonly(file_path):
            return EditResult(error=f"Managed resource is read-only: {file_path}")
        if self.host_file_broker is not None:
            broker_result = await asyncio.to_thread(
                self.host_file_broker.edit,
                file_path,
                old_string,
                new_string,
                replace_all=replace_all,
            )
            if broker_result is not None:
                return broker_result
            if self._unrouted_external_path(file_path):
                return EditResult(error=self._permission_error(file_path))
        target = self._approved_external_target(file_path)
        if target is None:
            if self._unrouted_external_path(file_path):
                return EditResult(error=self._permission_error(file_path))
            return await super().aedit(file_path, old_string, new_string, replace_all=replace_all)
        backend, backend_path, resolved = target
        result = await backend.aedit(backend_path, old_string, new_string, replace_all=replace_all)
        return self._restore_external_path(result, resolved)

    def ls(self, path: str):
        if self.host_file_broker is not None:
            result = self.host_file_broker.ls(path)
            if result is not None:
                return result
        if self._unrouted_external_path(path):
            return LsResult(error=self._permission_error(path))
        return super().ls(path)

    async def als(self, path: str):
        if self.host_file_broker is not None:
            result = await asyncio.to_thread(self.host_file_broker.ls, path)
            if result is not None:
                return result
        if self._unrouted_external_path(path):
            return LsResult(error=self._permission_error(path))
        return await super().als(path)

    def glob(self, pattern: str, path: str | None = None):
        if self.host_file_broker is not None:
            result = self.host_file_broker.glob(pattern, path=path)
            if result is not None:
                return result
        if self._unrouted_external_path(path):
            return GlobResult(error=self._permission_error(str(path)))
        return super().glob(pattern, path=path)

    async def aglob(self, pattern: str, path: str | None = None):
        if self.host_file_broker is not None:
            result = await asyncio.to_thread(
                self.host_file_broker.glob,
                pattern,
                path,
            )
            if result is not None:
                return result
        if self._unrouted_external_path(path):
            return GlobResult(error=self._permission_error(str(path)))
        return await super().aglob(pattern, path=path)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None):
        if self.host_file_broker is not None:
            result = self.host_file_broker.grep(pattern, path=path, glob=glob)
            if result is not None:
                return result
        if self._unrouted_external_path(path):
            return GrepResult(error=self._permission_error(str(path)))
        return super().grep(pattern, path=path, glob=glob)

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None):
        if self.host_file_broker is not None:
            result = await asyncio.to_thread(
                self.host_file_broker.grep,
                pattern,
                path,
                glob,
            )
            if result is not None:
                return result
        if self._unrouted_external_path(path):
            return GrepResult(error=self._permission_error(str(path)))
        return await super().agrep(pattern, path=path, glob=glob)

    def can_access_external_path(
        self,
        path: str,
        *,
        access: str,
        allow_missing_leaf: bool = False,
    ) -> bool:
        return bool(
            self.host_file_broker is not None
            and self.host_file_broker.authorize(
                path,
                access=access,
                allow_missing_leaf=allow_missing_leaf,
            )
        )
