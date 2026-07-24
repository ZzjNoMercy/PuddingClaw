"""Hash-bound validation receipt helpers for filesystem publication gates."""

import hashlib
import json
import logging
import posixpath
from pathlib import Path, PurePosixPath
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

from observability import emit_harness_metric
from tools.filesystem.inspect import digest, read_all
from tools.filesystem.schemas import (
    ValidateArtifactContractInput,
    ValidateHtmlReportInput,
)

logger = logging.getLogger(__name__)

CODE_LIKE_SUFFIXES = frozenset(
    {
        ".c", ".cc", ".cpp", ".cs", ".css", ".dart", ".go", ".h", ".hpp",
        ".htm", ".html", ".java", ".js", ".jsx", ".cjs", ".mjs", ".kt", ".kts",
        ".php", ".py", ".rb", ".rs", ".scss", ".sh", ".sql", ".swift", ".ts",
        ".tsx", ".cts", ".mts", ".vue",
    }
)


def code_like_target(file_path: str) -> bool:
    name = PurePosixPath(file_path.replace("\\", "/")).name.lower()
    return any(name.endswith(suffix) for suffix in CODE_LIKE_SUFFIXES)


def receipt_passed(receipt: dict[str, Any]) -> bool:
    return (
        str(receipt.get("status") or "passed") == "passed"
        and int(receipt.get("exit_code", -1)) == 0
        and int(receipt.get("checks_failed") or 0) == 0
    )


def receipt_authorizes_commit(receipt: dict[str, Any]) -> bool:
    """Return whether Harness vouches for this validator as a commit gate."""

    return bool(receipt.get("commit_authority")) and receipt_passed(receipt)


def receipt_obligation_key(receipt: dict[str, Any]) -> str:
    explicit = str(receipt.get("obligation_key") or "").strip()
    if explicit:
        return explicit
    return (
        f"{str(receipt.get('validator_kind') or 'unknown')}:"
        f"{str(receipt.get('validator_version') or 'unknown')}"
    )


def receipt_matches_target_hash(
    receipt: dict[str, Any],
    *,
    target_path: str,
    content_sha256: str,
) -> bool:
    normalized_target = str(PurePosixPath(target_path.replace("\\", "/")))
    return any(
        isinstance(ref, dict)
        and str(ref.get("content_sha256") or "") == content_sha256
        and str(PurePosixPath(str(ref.get("path") or "").replace("\\", "/")))
        == normalized_target
        for ref in receipt.get("artifact_refs") or []
    )


def accepted_receipts_for_target(
    receipts: list[dict[str, Any]],
    *,
    target_path: str,
    content_sha256: str,
    selected_receipt_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve accepted successes and still-active failures per obligation."""

    matching = [
        receipt
        for receipt in receipts
        if bool(receipt.get("commit_authority"))
        and receipt_matches_target_hash(
            receipt,
            target_path=target_path,
            content_sha256=content_sha256,
        )
    ]
    accepted = [
        receipt
        for receipt in matching
        if str(receipt.get("validation_receipt_id") or "") in selected_receipt_ids
        and receipt_authorizes_commit(receipt)
    ]
    latest_success_by_obligation: dict[str, float] = {}
    latest_failure_by_obligation: dict[str, tuple[float, dict[str, Any]]] = {}
    for receipt in matching:
        key = receipt_obligation_key(receipt)
        created_at = float(receipt.get("created_at") or 0)
        if receipt_passed(receipt):
            latest_success_by_obligation[key] = max(
                latest_success_by_obligation.get(key, 0.0), created_at
            )
        elif bool(receipt.get("blocking", True)):
            previous = latest_failure_by_obligation.get(key)
            if previous is None or created_at >= previous[0]:
                latest_failure_by_obligation[key] = (created_at, receipt)
    blocking = [
        receipt
        for key, (failed_at, receipt) in latest_failure_by_obligation.items()
        if (
            str(receipt.get("failure_class") or "artifact_failure")
            == "artifact_failure"
            or failed_at >= latest_success_by_obligation.get(key, 0.0)
        )
    ]
    return accepted, blocking


def persisted_validation_receipts(
    session_manager: Any,
    *,
    session_id: str,
    run_id: str,
    goal_id: str,
    goal_revision: Any,
) -> list[dict[str, Any]]:
    receipts: dict[str, dict[str, Any]] = {}

    def collect(refs: Any) -> None:
        for ref in refs if isinstance(refs, list) else []:
            if not isinstance(ref, dict) or ref.get("kind") != "validation_receipt":
                continue
            receipt_id = str(ref.get("validation_receipt_id") or "")
            if receipt_id:
                receipts[receipt_id] = ref

    run = session_manager.get_run_state(session_id, run_id)
    for activation in run.get("verification_activations") or [] if isinstance(run, dict) else []:
        if isinstance(activation, dict):
            collect(activation.get("evidence_refs"))
    if goal_id:
        goal = session_manager.get_goal_state(session_id, goal_id)
        if isinstance(goal, dict) and goal.get("objective_revision") == goal_revision:
            collect(goal.get("evidence_refs"))
    return list(receipts.values())

def build_validation_tools(backend: Any) -> list[StructuredTool]:
    def validate_html_report(
        html_file_path: str,
        timeout: int,
        runtime: ToolRuntime[Any, Any],
        browser_e2e: bool | None = None,
    ) -> ToolMessage:
        """Run contract-selected HTML validation without model-authored shell."""

        validate = getattr(backend, "validate_html_report", None)
        if not callable(validate):
            result = {
                "status": "infrastructure_error",
                "error_code": "html_validator_backend_unavailable",
                "failure_class": "infrastructure_failure",
                "error": "this Backend does not support HTML validation",
            }
        else:
            result = validate(
                html_file_path,
                browser_e2e=browser_e2e,
                timeout=timeout,
            )
        status = str(result.get("status") or "infrastructure_error")
        return ToolMessage(
            content=json.dumps(result, ensure_ascii=False, sort_keys=True),
            name="validate_html_report",
            tool_call_id=runtime.tool_call_id,
            status="success" if status == "completed" else "error",
        )

    def validate_artifact_contract(
        contract_id: str,
        html_file_path: str,
        javascript_file_path: str,
        runtime: ToolRuntime[Any, Any],
    ) -> ToolMessage:
        from graph.session_manager import session_manager
        from harness.artifact_contracts import validate_heatmap_year_contract
        from harness.models import ValidationReceipt, VerificationActivation

        if contract_id != "heatmap_year_contract/v1":
            return ToolMessage(
                content=f"Error: unknown artifact contract {contract_id}",
                name="validate_artifact_contract",
                tool_call_id=runtime.tool_call_id,
                status="error",
            )
        html, html_error = read_all(backend, html_file_path)
        javascript, javascript_error = read_all(
            backend, javascript_file_path
        )
        if html is None or javascript is None:
            return ToolMessage(
                content=f"Error: {html_error or javascript_error or 'unable to read contract inputs'}",
                name="validate_artifact_contract",
                tool_call_id=runtime.tool_call_id,
                status="error",
            )
        context = runtime.context if isinstance(runtime.context, dict) else {}
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        query_id = str(context.get("query_id") or "")
        goal_id = str(context.get("goal_id") or "")
        goal_revision = context.get("goal_revision")

        def formal_target(observed_path: str) -> str | None:
            normalized = posixpath.normpath(observed_path.replace("\\", "/"))
            for lease in session_manager.list_external_artifact_leases(session_id):
                if posixpath.normpath(str(lease.get("staged_path") or "")) == normalized:
                    return str(Path(str(lease.get("target_path") or "")).expanduser().resolve())
            roots = sorted(
                (
                    (
                        posixpath.normpath(str(lease.get("staged_dir") or "")),
                        lease,
                    )
                    for lease in session_manager.list_external_directory_leases(session_id)
                    if lease.get("staged_dir") and lease.get("directory_path")
                ),
                key=lambda item: len(item[0]),
                reverse=True,
            )
            for staged_root, lease in roots:
                if normalized.startswith(f"{staged_root}/"):
                    relative = posixpath.relpath(normalized, staged_root)
                    return str(
                        (
                            Path(str(lease["directory_path"])).expanduser().resolve()
                            / relative
                        ).resolve()
                    )
            if normalized.startswith("/scratch/"):
                return None
            return normalized

        input_pairs = [
            (html_file_path, html),
            (javascript_file_path, javascript),
        ]
        artifact_refs = []
        for observed_path, content in input_pairs:
            target_path = formal_target(observed_path)
            if target_path is None:
                return ToolMessage(
                    content=(
                        "Error: contract input is an unbound scratch file; validate files from an "
                        "active external artifact/directory lease."
                    ),
                    name="validate_artifact_contract",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            artifact_refs.append(
                {
                    "artifact_id": "artifact-"
                    + hashlib.sha256(f"external\0{target_path}".encode()).hexdigest()[:20],
                    "content_sha256": digest(content),
                    "path": target_path,
                    "observed_path": posixpath.normpath(
                        observed_path.replace("\\", "/")
                    ),
                }
            )
        result = validate_heatmap_year_contract(
            html=html,
            javascript=javascript,
            javascript_filename=Path(artifact_refs[1]["path"]).name,
        )
        passed = bool(result.get("passed"))
        if not passed:
            emit_harness_metric(
                logger,
                "artifact_ui_contract_failure_count",
                session_id=session_id,
                contract_id=contract_id,
            )
        receipt_seed = json.dumps(
            {
                "run_id": run_id,
                "contract_id": contract_id,
                "artifact_refs": artifact_refs,
                "passed": passed,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        receipt = ValidationReceipt(
            validation_receipt_id="validation-"
            + hashlib.sha256(receipt_seed.encode()).hexdigest()[:20],
            run_id=run_id,
            goal_id=goal_id or None,
            goal_revision=(
                int(goal_revision) if goal_revision is not None else None
            ),
            validator_kind="artifact_ui_contract",
            validator_version=contract_id,
            artifact_refs=artifact_refs,
            command_evidence_ref="sha256:"
            + hashlib.sha256(
                json.dumps(result, sort_keys=True).encode()
            ).hexdigest(),
            exit_code=0 if passed else 1,
            checks_passed=sum(
                1 for value in result.get("checks", {}).values() if value
            ),
            checks_failed=sum(
                1 for value in result.get("checks", {}).values() if not value
            ),
            status="passed" if passed else "failed",
            failure_class=None if passed else "artifact_failure",
            content_observed=True,
            blocking=True,
            commit_authority=True,
            obligation_key=f"artifact_ui_contract:{contract_id}",
        )
        activation = VerificationActivation(
            activation_id="activation-" + receipt.validation_receipt_id,
            run_id=run_id,
            query_id=query_id,
            tool_call_id=runtime.tool_call_id,
            tool_name="validate_artifact_contract",
            pack="artifact",
            status="succeeded",
            evidence_refs=[
                {
                    "kind": "validation_receipt",
                    **receipt.model_dump(mode="json"),
                    "contract_result": result,
                    "material": True,
                }
            ],
        )
        try:
            session_manager.append_run_verification_activation(
                session_id,
                run_id,
                activation.model_dump(mode="json"),
            )
        except (FileNotFoundError, ValueError):
            return ToolMessage(
                content="Error: artifact contract validation requires an active persisted Run",
                name="validate_artifact_contract",
                tool_call_id=runtime.tool_call_id,
                status="error",
            )
        return ToolMessage(
            content=json.dumps(
                {
                    **result,
                    "validation_receipt_id": receipt.validation_receipt_id,
                    "artifact_refs": artifact_refs,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            name="validate_artifact_contract",
            tool_call_id=runtime.tool_call_id,
            status="success" if passed else "error",
            artifact={
                "validation_receipt": receipt.model_dump(mode="json"),
                "contract_result": result,
            },
        )

    return [
        StructuredTool.from_function(
            name="validate_html_report",
            description=(
                "Validate one HTML report against its current hash. Ordinary "
                "runs perform lightweight structure, duplicate-ID, and local-"
                "resource checks. The frozen verification contract enables "
                "PuddingClaw's fixed offline Chromium adapter only for explicit "
                "browser E2E requirements. Omit browser_e2e; no model-authored "
                "shell or per-call HITL is required after directory read "
                "permission exists. Returns an exact-hash ValidationReceipt."
            ),
            func=validate_html_report,
            args_schema=ValidateHtmlReportInput,
            infer_schema=False,
        ),
        StructuredTool.from_function(
            name="validate_artifact_contract",
            description=(
                "Run a registered deterministic cross-file artifact contract and persist a "
                "ValidationReceipt bound to every exact input path/hash. Use "
                "heatmap_year_contract/v1 for heatmap selector/data/default/matrix consistency."
            ),
            func=validate_artifact_contract,
            args_schema=ValidateArtifactContractInput,
            infer_schema=False,
        ),
    ]
