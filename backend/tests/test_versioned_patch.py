import json
from types import SimpleNamespace

import pytest
from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import ExecuteResponse
from langchain_core.messages import ToolMessage

from graph.middlewares.versioned_patch import (
    ReplacementHunk,
    VersionedPatchMiddleware,
    _digest,
    _safe_staged_filename,
)
from graph.session_manager import SessionManager
from harness.deterministic_checks import _evaluate_code_validation
from harness.models import RunRecord, RunStatus, ValidationReceipt, VerificationActivation
from harness.verification_activations import (
    build_verification_activations,
    verification_packs_for_tool,
)


def _runtime(call_id: str = "call-1", **context):
    return SimpleNamespace(tool_call_id=call_id, context=context)


def test_committed_external_artifact_supersedes_older_staged_lease(tmp_path) -> None:
    manager = SessionManager()
    state = tmp_path / "state"
    state.mkdir()
    manager.initialize(state)
    manager.create_session("lease-order-session")
    common = {
        "target_path": "/external/report.html",
        "goal_id": "goal-1",
        "goal_revision": 2,
        "run_id": "run-1",
        "query_id": "query-1",
    }
    manager.upsert_external_artifact_lease(
        "lease-order-session",
        {
            **common,
            "lease_id": "artifact-lease-old",
            "status": "staged",
            "created_at": 10.0,
            "expected_source_sha256": "sha256:old-source",
        },
    )
    manager.upsert_external_artifact_lease(
        "lease-order-session",
        {
            **common,
            "lease_id": "artifact-lease-committed",
            "status": "committed",
            "created_at": 20.0,
            "committed_at": 30.0,
            "expected_source_sha256": "sha256:old-source",
            "committed_sha256": "sha256:new-source",
        },
    )

    found = manager.find_staged_external_artifact_lease(
        "lease-order-session",
        run_id="run-2",
        query_id="query-2",
        target_path="/external/report.html",
        goal_id="goal-1",
        goal_revision=2,
    )

    assert found is None


def test_versioned_patch_rebases_once_and_reports_structured_conflicts(tmp_path):
    path = tmp_path / "report.html"
    path.write_text("A\nB\n", encoding="utf-8")
    middleware = VersionedPatchMiddleware(
        FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    )
    patch_tool = next(tool for tool in middleware.tools if tool.name == "patch_file")

    rebased = patch_tool.func(
        file_path="/report.html",
        expected_sha256="sha256:stale",
        replacements=[ReplacementHunk(old_string="A", new_string="C")],
        runtime=_runtime(),
    )
    assert rebased.status == "success"
    rebased_payload = json.loads(rebased.content)
    assert rebased_payload["rebased"] is True
    assert rebased_payload["rebased_from_sha256"] == "sha256:stale"
    assert rebased_payload["rebased_to_sha256"] == _digest("A\nB\n")
    assert rebased_payload["mutation_receipt_id"] == ""
    assert rebased_payload["validation_receipt_ids"] == []
    assert path.read_text(encoding="utf-8") == "C\nB\n"

    conflict = patch_tool.func(
        file_path="/report.html",
        expected_sha256="sha256:older",
        replacements=[ReplacementHunk(old_string="missing", new_string="D")],
        runtime=_runtime("call-conflict"),
    )
    assert conflict.status == "error"
    conflict_payload = json.loads(conflict.content)
    assert conflict_payload["error_code"] == "patch_rebase_conflict"
    assert conflict_payload["next_action"] == "inspect_conflicting_region"
    assert path.read_text(encoding="utf-8") == "C\nB\n"

    current = path.read_text(encoding="utf-8")
    applied = patch_tool.func(
        file_path="/report.html",
        expected_sha256=_digest(current),
        replacements=[
            ReplacementHunk(old_string="C", new_string="E"),
            ReplacementHunk(old_string="B", new_string="D"),
        ],
        runtime=_runtime("call-2"),
    )
    assert applied.status == "success"
    assert path.read_text(encoding="utf-8") == "E\nD\n"


def test_staged_filename_preserves_cjk_and_scratch_upsert_requires_compare_and_swap(tmp_path):
    assert _safe_staged_filename("产品配置分析_2026.html") == "产品配置分析_2026.html"
    middleware = VersionedPatchMiddleware(
        FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    )
    tool = next(item for item in middleware.tools if item.name == "upsert_scratch_file")

    created = tool.func(
        file_path="/scratch/validate.py",
        content="print('v1')",
        runtime=_runtime("scratch-create"),
    )
    refused = tool.func(
        file_path="/scratch/validate.py",
        content="print('v2')",
        runtime=_runtime("scratch-refuse"),
    )
    replaced = tool.func(
        file_path="/scratch/validate.py",
        content="print('v2')",
        expected_sha256=_digest("print('v1')"),
        runtime=_runtime("scratch-replace"),
    )

    assert created.status == "success"
    assert refused.status == "error"
    assert "already exists" in refused.content
    assert replaced.status == "success"
    assert (tmp_path / "scratch" / "validate.py").read_text(encoding="utf-8") == "print('v2')"


def test_validation_obligations_do_not_supersede_other_validator_families() -> None:
    from graph.middlewares.versioned_patch import _accepted_receipts_for_target

    target = "/external/app.js"
    digest = "sha256:" + "a" * 64

    def receipt(
        receipt_id: str,
        *,
        kind: str,
        version: str,
        status: str,
        created_at: float,
    ) -> dict:
        return {
            "validation_receipt_id": receipt_id,
            "validator_kind": kind,
            "validator_version": version,
            "obligation_key": f"{kind}:{version}",
            "commit_authority": True,
            "artifact_refs": [{"path": target, "content_sha256": digest}],
            "status": status,
            "exit_code": 0 if status == "passed" else 1,
            "checks_failed": 0 if status == "passed" else 1,
            "blocking": True,
            "failure_class": (
                "invocation_failure" if status == "failed" else None
            ),
            "created_at": created_at,
        }

    node_failed = receipt(
        "node-failed",
        kind="javascript_syntax",
        version="node-check/v1",
        status="failed",
        created_at=1.0,
    )
    ui_passed = receipt(
        "ui-passed",
        kind="artifact_ui_contract",
        version="heatmap_year_contract/v1",
        status="passed",
        created_at=2.0,
    )
    accepted, blocking = _accepted_receipts_for_target(
        [node_failed, ui_passed],
        target_path=target,
        content_sha256=digest,
        selected_receipt_ids={"ui-passed"},
    )
    assert [item["validation_receipt_id"] for item in accepted] == ["ui-passed"]
    assert [item["validation_receipt_id"] for item in blocking] == ["node-failed"]

    node_passed = receipt(
        "node-passed",
        kind="javascript_syntax",
        version="node-check/v1",
        status="passed",
        created_at=3.0,
    )
    _accepted, blocking = _accepted_receipts_for_target(
        [node_failed, ui_passed, node_passed],
        target_path=target,
        content_sha256=digest,
        selected_receipt_ids={"node-passed", "ui-passed"},
    )
    assert blocking == []


def test_only_controlled_single_command_validators_receive_commit_authority() -> None:
    from harness.verification_activations import _validation_receipt_for_result

    def build(command: str, call_id: str):
        return _validation_receipt_for_result(
            session_id="",
            run_id="run-validation-authority",
            goal_id=None,
            goal_revision=None,
            tool_call_id=call_id,
            tool_name="execute",
            args={"command": command},
            result=ToolMessage(
                content="Exit code: 0",
                name="execute",
                tool_call_id=call_id,
                status="success",
            ),
            workspace_path="",
        )

    direct = build("node --check /scratch/external/lease/app.js", "direct")
    masked = build("node --check /scratch/external/lease/app.js || true", "masked")
    noop = build(
        "python3 /scratch/validation/noop.py /scratch/external/lease/app.js",
        "noop",
    )
    forced_success = build(
        "ruff check --exit-zero /scratch/external/lease/app.py",
        "forced-success",
    )
    html_diagnostic_wrapper = build(
        "pwd && ls -la && node "
        "/opt/puddingclaw/bin/validate-html-report-e2e.mjs report.html",
        "html-diagnostic-wrapper",
    )
    unregistered_wrapper = build(
        "pwd && find . -maxdepth 2 && node "
        "/opt/puddingclaw/bin/validate-html-report-e2e.mjs report.html",
        "unregistered-wrapper",
    )

    assert direct is not None and direct.commit_authority is True
    assert direct.validator_version == "node-check/v1"
    assert masked is not None and masked.commit_authority is False
    assert noop is not None and noop.commit_authority is False
    assert forced_success is not None and forced_success.commit_authority is False
    assert html_diagnostic_wrapper is not None
    assert html_diagnostic_wrapper.commit_authority is True
    assert html_diagnostic_wrapper.validator_kind == "browser_runtime"
    assert verification_packs_for_tool(
        "execute_external_directory",
        {
            "command": (
                "pwd && ls -la && node "
                "/opt/puddingclaw/bin/validate-html-report-e2e.mjs report.html"
            )
        },
    ) == ["code"]
    assert unregistered_wrapper is not None
    assert unregistered_wrapper.commit_authority is False


class _HtmlValidatorExecutionBackend:
    def __init__(self, *, exit_code: int = 0, output: str | None = None):
        self.exit_code = exit_code
        self.output = output or json.dumps(
            {
                "passed": exit_code == 0,
                "page": {
                    "echartsAvailable": True,
                    "chartContainerCount": 1,
                    "initializedChartCount": 1,
                },
                "failures": [],
            }
        )
        self.commands: list[tuple[str, int]] = []

    def execute(self, command: str, *, timeout: int):
        self.commands.append((command, timeout))
        return ExecuteResponse(output=self.output, exit_code=self.exit_code)


def test_validate_html_report_emits_hash_bound_browser_receipt(tmp_path) -> None:
    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
    from graph.session_manager import session_manager
    from harness.rubric_compiler import RubricBuildContext, RunRubricCompiler

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state.mkdir()
    workspace.mkdir()
    report = workspace / "report.html"
    report.write_text("<!doctype html><title>Report</title>", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("html-validator-session")
    contract = RunRubricCompiler.compile(
        RubricBuildContext(
            user_message=f"生成 {report} 并执行 E2E 测试",
            force_required=True,
        )
    )
    assert contract is not None and contract.browser_e2e_required is True
    run = RunRecord(
        run_id="run-html-validator",
        query_id="query-html-validator",
        session_id="html-validator-session",
        objective=f"生成 {report}",
        status=RunStatus.PREPARING,
        declared_artifact_targets=[str(report)],
        declared_verification_contract=contract,
        verification_contract=contract,
    )
    session_manager.start_harness_run(
        "html-validator-session",
        run.model_dump(mode="json"),
    )
    session_manager.transition_run_status(
        "html-validator-session",
        run.run_id,
        RunStatus.RUNNING.value,
    )
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="html-validator-session",
        run_id=run.run_id,
        query_id=run.query_id,
        workspace_root=workspace,
    )
    execution = _HtmlValidatorExecutionBackend()
    backend.execution_backend = execution
    tool = next(
        item
        for item in VersionedPatchMiddleware(backend).tools
        if item.name == "validate_html_report"
    )

    result = tool.func(
        html_file_path=str(report),
        timeout=120,
        runtime=_runtime(
            "validate-html",
            session_id="html-validator-session",
            run_id=run.run_id,
            query_id=run.query_id,
        ),
    )

    assert result.status == "success"
    assert execution.commands == [
        (
            "node /opt/puddingclaw/bin/validate-html-report-e2e.mjs "
            "/workspace/report.html",
            120,
        )
    ]
    activations = build_verification_activations(
        run_id=run.run_id,
        query_id=run.query_id,
        tool_call_id="validate-html",
        tool_name="validate_html_report",
        args={"html_file_path": str(report), "timeout": 120},
        result=result,
        session_id="html-validator-session",
        workspace_path=str(workspace),
    )
    receipt = next(
        ref
        for activation in activations
        for ref in activation.evidence_refs
        if ref.get("kind") == "validation_receipt"
    )
    assert receipt["status"] == "passed"
    assert receipt["validator_kind"] == "browser_runtime"
    assert receipt["validator_version"] == "puddingclaw-html-e2e/v1"
    assert receipt["commit_authority"] is True
    assert receipt["failure_class"] is None
    assert receipt["artifact_refs"] == [
        {
            "artifact_id": receipt["artifact_refs"][0]["artifact_id"],
            "content_sha256": _digest(report.read_text(encoding="utf-8")),
            "path": str(report.resolve()),
            "observed_path": str(report.resolve()),
        }
    ]


def test_validate_html_report_defaults_to_lightweight_structure_mode(
    tmp_path,
) -> None:
    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
    from graph.session_manager import session_manager
    from harness.rubric_compiler import RubricBuildContext, RunRubricCompiler

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state.mkdir()
    workspace.mkdir()
    report = workspace / "report.html"
    script = workspace / "charts.js"
    script.write_text("const ready = true;", encoding="utf-8")
    report.write_text(
        "<!doctype html><html><body><div id='chart'></div>"
        "<script src='charts.js'></script></body></html>",
        encoding="utf-8",
    )
    session_manager.initialize(state)
    session_manager.create_session("html-structure-session")
    contract = RunRubricCompiler.compile(
        RubricBuildContext(
            user_message=f"生成普通 HTML 报告 {report}",
            force_required=True,
        )
    )
    assert contract is not None and contract.browser_e2e_required is False
    run = RunRecord(
        run_id="run-html-structure",
        query_id="query-html-structure",
        session_id="html-structure-session",
        objective=f"生成普通 HTML 报告 {report}",
        status=RunStatus.PREPARING,
        declared_artifact_targets=[str(report)],
        declared_verification_contract=contract,
        verification_contract=contract,
    )
    session_manager.start_harness_run(
        "html-structure-session",
        run.model_dump(mode="json"),
    )
    session_manager.transition_run_status(
        "html-structure-session",
        run.run_id,
        RunStatus.RUNNING.value,
    )
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="html-structure-session",
        run_id=run.run_id,
        query_id=run.query_id,
        workspace_root=workspace,
    )
    tool = next(
        item
        for item in VersionedPatchMiddleware(backend).tools
        if item.name == "validate_html_report"
    )

    result = tool.func(
        html_file_path=str(report),
        timeout=120,
        runtime=_runtime(
            "validate-html-structure",
            session_id="html-structure-session",
            run_id=run.run_id,
            query_id=run.query_id,
        ),
    )

    assert result.status == "success"
    payload = json.loads(result.content)
    assert payload["validator_kind"] == "html_structure"
    assert payload["browser_e2e_required"] is False
    assert json.loads(payload["output"])["passed"] is True
    activations = build_verification_activations(
        run_id=run.run_id,
        query_id=run.query_id,
        tool_call_id="validate-html-structure",
        tool_name="validate_html_report",
        args={"html_file_path": str(report), "timeout": 120},
        result=result,
        session_id="html-structure-session",
        workspace_path=str(workspace),
    )
    receipt = next(
        ref
        for activation in activations
        for ref in activation.evidence_refs
        if ref.get("kind") == "validation_receipt"
    )
    assert receipt["validator_kind"] == "html_structure"
    assert receipt["validator_version"] == "puddingclaw-html-structure/v1"
    assert receipt["commit_authority"] is True

    refused_upgrade = tool.func(
        html_file_path=str(report),
        browser_e2e=True,
        timeout=120,
        runtime=_runtime(
            "refuse-html-e2e-upgrade",
            session_id="html-structure-session",
            run_id=run.run_id,
            query_id=run.query_id,
        ),
    )
    assert refused_upgrade.status == "error"
    assert json.loads(refused_upgrade.content)["error_code"] == (
        "html_validation_mode_contract_mismatch"
    )


def test_legacy_html_diagnostic_wrapper_emits_browser_receipt_for_exact_hash(
    tmp_path,
) -> None:
    from graph.session_manager import session_manager

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    external = tmp_path / "reports"
    for directory in (state, workspace, external):
        directory.mkdir()
    report = external / "report.html"
    report.write_text("<!doctype html><title>Report</title>", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("legacy-html-wrapper-session")
    run = RunRecord(
        run_id="run-legacy-html-wrapper",
        query_id="query-legacy-html-wrapper",
        session_id="legacy-html-wrapper-session",
        objective=f"验证 {report}",
        status=RunStatus.PREPARING,
        declared_artifact_targets=[str(report)],
    )
    session_manager.start_harness_run(
        "legacy-html-wrapper-session",
        run.model_dump(mode="json"),
    )
    session_manager.transition_run_status(
        "legacy-html-wrapper-session",
        run.run_id,
        RunStatus.RUNNING.value,
    )
    command = (
        "pwd && ls -la && node "
        "/opt/puddingclaw/bin/validate-html-report-e2e.mjs report.html"
    )
    activations = build_verification_activations(
        run_id=run.run_id,
        query_id=run.query_id,
        tool_call_id="legacy-html-wrapper",
        tool_name="execute_external_directory",
        args={
            "directory_path": str(external),
            "command": command,
            "mode": "read_only",
        },
        result=ToolMessage(
            content=json.dumps(
                {
                    "status": "completed",
                    "directory_path": str(external),
                    "exit_code": 0,
                    "output": json.dumps({"passed": True, "failures": []}),
                }
            ),
            tool_call_id="legacy-html-wrapper",
            name="execute_external_directory",
            status="success",
        ),
        session_id="legacy-html-wrapper-session",
        workspace_path=str(workspace),
    )

    receipt = next(
        ref
        for activation in activations
        for ref in activation.evidence_refs
        if ref.get("kind") == "validation_receipt"
    )
    assert receipt["validator_kind"] == "browser_runtime"
    assert receipt["commit_authority"] is True
    assert receipt["status"] == "passed"
    assert receipt["artifact_refs"] == [
        {
            "artifact_id": receipt["artifact_refs"][0]["artifact_id"],
            "content_sha256": _digest(report.read_text(encoding="utf-8")),
            "path": str(report.resolve()),
            "observed_path": str(report.resolve()),
        }
    ]


@pytest.mark.parametrize(
    ("failure_class", "expected_kind"),
    [
        ("invocation_failure", "validator_protocol_error"),
        ("infrastructure_failure", "infrastructure_error"),
    ],
)
def test_html_control_failure_class_is_not_a_task_gap(
    tmp_path,
    failure_class: str,
    expected_kind: str,
) -> None:
    from graph.session_manager import session_manager

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state.mkdir()
    workspace.mkdir()
    report = workspace / "report.html"
    report.write_text("<!doctype html><title>Report</title>", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("html-control-failure-session")
    run = RunRecord(
        run_id="run-html-control-failure",
        query_id="query-html-control-failure",
        session_id="html-control-failure-session",
        objective=f"生成 {report}",
        status=RunStatus.PREPARING,
        declared_artifact_targets=[str(report)],
    )
    session_manager.start_harness_run(
        "html-control-failure-session",
        run.model_dump(mode="json"),
    )
    session_manager.transition_run_status(
        "html-control-failure-session",
        run.run_id,
        RunStatus.RUNNING.value,
    )
    write_activations = build_verification_activations(
        run_id=run.run_id,
        query_id=run.query_id,
        tool_call_id="write-html",
        tool_name="write_file",
        args={"file_path": str(report), "content": report.read_text()},
        result=ToolMessage(
            content=f"Wrote {report}",
            tool_call_id="write-html",
            name="write_file",
            status="success",
        ),
        session_id="html-control-failure-session",
        workspace_path=str(workspace),
    )
    failure = ToolMessage(
        content=json.dumps(
            {
                "status": "io_error",
                "failure_class": failure_class,
                "html_file_path": str(report),
                "exit_code": 1,
                "output": "validator unavailable",
            }
        ),
        tool_call_id="validate-html-failed",
        name="validate_html_report",
        status="error",
    )
    validation_activations = build_verification_activations(
        run_id=run.run_id,
        query_id=run.query_id,
        tool_call_id="validate-html-failed",
        tool_name="validate_html_report",
        args={"html_file_path": str(report), "timeout": 120},
        result=failure,
        session_id="html-control-failure-session",
        workspace_path=str(workspace),
    )

    evaluation = _evaluate_code_validation(
        "code_validation",
        {
            "verification_activations": [
                item.model_dump(mode="json")
                for item in [*write_activations, *validation_activations]
            ]
        },
        {},
    )
    assert evaluation.passed is False
    assert evaluation.failure_kind.value == expected_kind


@pytest.mark.parametrize(
    ("failure_class", "expected_passed", "expected_kind"),
    [
        ("invocation_failure", True, None),
        ("artifact_failure", False, "task_gap"),
    ],
)
def test_same_hash_success_only_supersedes_validator_invocation_failure(
    tmp_path,
    failure_class: str,
    expected_passed: bool,
    expected_kind: str | None,
) -> None:
    from graph.session_manager import session_manager

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state.mkdir()
    workspace.mkdir()
    report = workspace / "report.html"
    report.write_text("<!doctype html><title>Report</title>", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("same-hash-validation-session")
    run = RunRecord(
        run_id="run-same-hash",
        query_id="query-same-hash",
        session_id="same-hash-validation-session",
        objective=f"生成 {report}",
        status=RunStatus.PREPARING,
        declared_artifact_targets=[str(report)],
    )
    session_manager.start_harness_run(
        "same-hash-validation-session",
        run.model_dump(mode="json"),
    )
    session_manager.transition_run_status(
        "same-hash-validation-session",
        run.run_id,
        RunStatus.RUNNING.value,
    )
    write = build_verification_activations(
        run_id=run.run_id,
        query_id=run.query_id,
        tool_call_id="write-html",
        tool_name="write_file",
        args={"file_path": str(report), "content": report.read_text()},
        result=ToolMessage(
            content=f"Wrote {report}",
            tool_call_id="write-html",
            name="write_file",
            status="success",
        ),
        session_id="same-hash-validation-session",
        workspace_path=str(workspace),
    )
    failed = build_verification_activations(
        run_id=run.run_id,
        query_id=run.query_id,
        tool_call_id="validate-html-failed",
        tool_name="validate_html_report",
        args={"html_file_path": str(report), "timeout": 120},
        result=ToolMessage(
            content=json.dumps(
                {
                    "status": "io_error",
                    "failure_class": failure_class,
                    "html_file_path": str(report),
                    "exit_code": 1,
                    "output": "failed",
                }
            ),
            tool_call_id="validate-html-failed",
            name="validate_html_report",
            status="error",
        ),
        session_id="same-hash-validation-session",
        workspace_path=str(workspace),
    )
    passed = build_verification_activations(
        run_id=run.run_id,
        query_id=run.query_id,
        tool_call_id="validate-html-passed",
        tool_name="validate_html_report",
        args={"html_file_path": str(report), "timeout": 120},
        result=ToolMessage(
            content=json.dumps(
                {
                    "status": "completed",
                    "html_file_path": str(report),
                    "exit_code": 0,
                    "output": json.dumps({"passed": True, "failures": []}),
                }
            ),
            tool_call_id="validate-html-passed",
            name="validate_html_report",
            status="success",
        ),
        session_id="same-hash-validation-session",
        workspace_path=str(workspace),
    )

    evaluation = _evaluate_code_validation(
        "code_validation",
        {
            "verification_activations": [
                item.model_dump(mode="json")
                for item in [*write, *failed, *passed]
            ]
        },
        {},
    )
    assert evaluation.passed is expected_passed
    assert (
        evaluation.failure_kind.value if evaluation.failure_kind is not None else None
    ) == expected_kind


def test_long_command_output_tail_classifies_missing_host_path_as_invocation_failure(
    tmp_path,
) -> None:
    from graph.session_manager import session_manager

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state.mkdir()
    workspace.mkdir()
    target = workspace / "charts.js"
    target.write_text("const value = 1;\n", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("long-validation-tail-session")
    run = RunRecord(
        run_id="run-long-tail",
        query_id="query-long-tail",
        session_id="long-validation-tail-session",
        objective=f"修改 {target}",
        status=RunStatus.PREPARING,
        declared_artifact_targets=[str(target)],
    )
    session_manager.start_harness_run(
        "long-validation-tail-session",
        run.model_dump(mode="json"),
    )
    session_manager.transition_run_status(
        "long-validation-tail-session",
        run.run_id,
        RunStatus.RUNNING.value,
    )
    write_activation = next(
        item
        for item in build_verification_activations(
            run_id=run.run_id,
            query_id=run.query_id,
            tool_call_id="write-long-tail",
            tool_name="write_file",
            args={"file_path": str(target), "content": target.read_text()},
            result=ToolMessage(
                content=f"Wrote {target}",
                tool_call_id="write-long-tail",
                name="write_file",
                status="success",
            ),
            session_id=run.session_id,
            workspace_path=str(workspace),
        )
        if item.pack == "code"
    )
    session_manager.append_run_verification_activation(
        run.session_id,
        run.run_id,
        write_activation.model_dump(mode="json"),
    )
    noisy_failure = (
        ("grpc warning\n" * 300)
        + f"Error: Cannot find module '{target}'\n"
        + "code: 'MODULE_NOT_FOUND'\nExit code: 1"
    )
    validation_activation = next(
        item
        for item in build_verification_activations(
            run_id=run.run_id,
            query_id=run.query_id,
            tool_call_id="validate-long-tail",
            tool_name="execute_external_directory",
            args={
                "directory_path": str(workspace),
                "command": "node --check charts.js",
            },
            result=ToolMessage(
                content=noisy_failure,
                tool_call_id="validate-long-tail",
                name="execute_external_directory",
                status="error",
            ),
            session_id=run.session_id,
            workspace_path=str(workspace),
        )
        if item.pack == "code"
    )
    receipt = next(
        item
        for item in validation_activation.evidence_refs
        if item.get("kind") == "validation_receipt"
    )

    assert receipt["failure_class"] == "invocation_failure"
    assert receipt["content_observed"] is False
    execution = next(
        item
        for item in validation_activation.evidence_refs
        if item.get("kind") == "tool_execution"
    )
    assert execution["attempted_artifact_refs"][0]["path"] == str(target)


def test_heatmap_contract_receipt_binds_both_exact_drafts_and_authorizes_commits(
    tmp_path,
) -> None:
    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
    from graph.session_manager import session_manager

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    external = tmp_path / "external"
    for path in (state, workspace, scratch, external):
        path.mkdir()
    html_target = external / "report.html"
    js_target = external / "charts.js"
    html_target.write_text("old", encoding="utf-8")
    js_target.write_text("old", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("contract-session")
    run = RunRecord(
        run_id="run-contract",
        query_id="query-contract",
        session_id="contract-session",
        objective="update heatmap",
        status=RunStatus.PREPARING,
    )
    session_manager.start_harness_run("contract-session", run.model_dump(mode="json"))
    session_manager.transition_run_status(
        "contract-session", run.run_id, RunStatus.RUNNING.value
    )
    for target in (html_target, js_target):
        for grant_type, capability in (
            ("external_file_read", "read"),
            ("external_file_write", "write"),
        ):
            session_manager.add_permission_grant(
                "contract-session",
                grant_type=grant_type,
                target_kind="exact_file",
                target=str(target.resolve()),
                capabilities=[capability, "external_path"],
            )
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    middleware = VersionedPatchMiddleware(
        PermissionedCompositeBackend(
            default=workspace_backend,
            routes={
                "/workspace/": workspace_backend,
                "/scratch/": FilesystemBackend(root_dir=scratch, virtual_mode=True),
            },
            session_id="contract-session",
            workspace_root=workspace,
        )
    )
    tools = {item.name: item for item in middleware.tools}
    runtime_context = {
        "session_id": "contract-session",
        "run_id": "run-contract",
        "query_id": "query-contract",
    }
    staged = {}
    for index, target in enumerate((html_target, js_target), start=1):
        result = tools["stage_external_artifact"].func(
            file_path=str(target.resolve()),
            runtime=_runtime(f"stage-{index}", **runtime_context),
        )
        assert result.status == "success"
        lease_id = result.content.split("lease_id=", 1)[1].split(";", 1)[0]
        staged[target] = session_manager.get_external_artifact_lease(
            "contract-session", lease_id
        )

    html = """<select id="heatmapYearSelect">
    <option value="2025">2025</option><option value="2026" selected>2026</option>
    </select><script src="charts.js"></script>"""
    matrix = [[0] * 10 for _ in range(8)]
    javascript = (
        "const heatmapByYear = "
        + json.dumps({"2025": matrix, "2026": matrix})
        + '; let currentHeatYear = "2026"; '
        + 'selector.addEventListener("change", function () {'
        + "const values = heatmapByYear[currentHeatYear]; });"
    )
    for target, content in ((html_target, html), (js_target, javascript)):
        lease = staged[target]
        host = scratch / str(lease["staged_path"]).removeprefix("/scratch/")
        host.write_text(content, encoding="utf-8")

    validated = tools["validate_artifact_contract"].func(
        contract_id="heatmap_year_contract/v1",
        html_file_path=staged[html_target]["staged_path"],
        javascript_file_path=staged[js_target]["staged_path"],
        runtime=_runtime("validate-contract", **runtime_context),
    )
    assert validated.status == "success"
    receipt_id = validated.artifact["validation_receipt"]["validation_receipt_id"]
    assert {
        item["path"]
        for item in validated.artifact["validation_receipt"]["artifact_refs"]
    } == {str(html_target.resolve()), str(js_target.resolve())}

    for index, (target, content) in enumerate(
        ((html_target, html), (js_target, javascript)), start=1
    ):
        lease = staged[target]
        committed = tools["commit_external_artifact"].func(
            lease_id=lease["lease_id"],
            file_path=str(target.resolve()),
            expected_draft_sha256=_digest(content),
            validation_receipt_ids=[receipt_id],
            runtime=_runtime(f"commit-{index}", **runtime_context),
        )
        assert committed.status == "success"
        assert target.read_text(encoding="utf-8") == content
    assert {
        contract_id
        for item in session_manager.list_delivered_artifacts("contract-session")
        for contract_id in item["contract_ids"]
    } == {"heatmap_year_contract/v1"}
    registered = session_manager.list_delivered_artifacts("contract-session")
    registered_ids = {item["artifact_id"] for item in registered}
    assert len(registered_ids) == 2
    assert all(
        set(item["related_artifact_ids"]) == registered_ids - {item["artifact_id"]}
        for item in registered
    )


def test_external_artifact_lease_stages_validates_and_commits_exact_target(tmp_path):
    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
    from graph.session_manager import session_manager

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    # Plain-text artifacts intentionally remain compatible with validation-free
    # commits. Code-like targets are covered by the fail-closed tests below.
    external = tmp_path / "external" / "report.txt"
    workspace.mkdir()
    scratch.mkdir()
    state.mkdir()
    external.parent.mkdir()
    external.write_text("before", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("lease-session")
    for grant_type, capabilities in (
        ("external_file_read", ["read", "external_path"]),
        ("external_file_write", ["write", "external_path"]),
    ):
        session_manager.add_permission_grant(
            "lease-session",
            grant_type=grant_type,
            target_kind="exact_file",
            target=str(external.resolve()),
            capabilities=capabilities,
        )

    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={
            "/workspace/": workspace_backend,
            "/scratch/": FilesystemBackend(root_dir=scratch, virtual_mode=True),
        },
        session_id="lease-session",
        workspace_root=workspace,
    )
    middleware = VersionedPatchMiddleware(backend)
    stage_tool = next(tool for tool in middleware.tools if tool.name == "stage_external_artifact")
    commit_tool = next(tool for tool in middleware.tools if tool.name == "commit_external_artifact")

    staged = stage_tool.func(
        file_path=str(external.resolve()),
        runtime=_runtime(
            "call-stage",
            session_id="lease-session",
            run_id="run-1",
            query_id="query-1",
        ),
    )
    assert staged.status == "success"
    lease_id = staged.content.split("lease_id=", 1)[1].split(";", 1)[0]
    lease = session_manager.get_external_artifact_lease("lease-session", lease_id)
    assert lease is not None
    assert lease["target_path"] == str(external.resolve())
    staged_host_path = scratch / str(lease["staged_path"]).removeprefix("/scratch/")
    assert staged_host_path.read_text(encoding="utf-8") == "before"
    staged_host_path.write_text("after", encoding="utf-8")

    reused = stage_tool.func(
        file_path=str(external.resolve()),
        runtime=_runtime(
            "call-stage-retry",
            session_id="lease-session",
            run_id="run-1",
            query_id="query-1",
        ),
    )
    assert reused.status == "success"
    assert "reused" in reused.content
    assert f"lease_id={lease_id}" in reused.content
    assert staged_host_path.read_text(encoding="utf-8") == "after"

    external.write_text("concurrent", encoding="utf-8")
    replay_conflict = stage_tool.func(
        file_path=str(external.resolve()),
        runtime=_runtime(
            "call-stage",
            session_id="lease-session",
            run_id="run-1",
            query_id="query-1",
        ),
    )
    assert replay_conflict.status == "error"
    assert "Stage conflict" in replay_conflict.content
    assert session_manager.get_external_artifact_lease(
        "lease-session", lease_id
    )["expected_source_sha256"] == lease["expected_source_sha256"]

    conflict = commit_tool.func(
        lease_id=lease_id,
        file_path=str(external.resolve()),
        expected_source_sha256=lease["expected_source_sha256"],
        runtime=_runtime(
            "call-conflict",
            session_id="lease-session",
            run_id="run-1",
            query_id="query-1",
        ),
    )
    assert conflict.status == "error"
    assert "external target changed after staging" in conflict.content
    assert external.read_text(encoding="utf-8") == "concurrent"

    external.write_text("before", encoding="utf-8")
    committed = commit_tool.func(
        lease_id=lease_id,
        file_path=str(external.resolve()),
        runtime=_runtime(
            "call-commit",
            session_id="lease-session",
            run_id="run-1",
            query_id="query-1",
        ),
    )
    assert committed.status == "success"
    assert external.read_text(encoding="utf-8") == "after"
    persisted = session_manager.get_external_artifact_lease("lease-session", lease_id)
    assert persisted is not None
    assert persisted["status"] == "committed"

    forged_replay = commit_tool.func(
        lease_id=lease_id,
        file_path="/workspace/not-the-authoritative-target.html",
        expected_source_sha256="sha256:forged",
        runtime=_runtime(
            "call-forged",
            session_id="lease-session",
            run_id="run-1",
            query_id="query-1",
        ),
    )
    assert forged_replay.status == "error"
    assert "exact target" in forged_replay.content


def test_code_external_artifact_commit_requires_matching_draft_receipt(tmp_path):
    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
    from graph.session_manager import session_manager

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    external = tmp_path / "external" / "app.js"
    for path in (state, workspace, scratch, external.parent):
        path.mkdir()
    external.write_text("const value = 1;\n", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("validated-lease-session")
    run = RunRecord(
        run_id="run-1",
        query_id="query-1",
        session_id="validated-lease-session",
        objective="update app.js",
        status=RunStatus.PREPARING,
    )
    session_manager.start_harness_run(
        "validated-lease-session", run.model_dump(mode="json")
    )
    session_manager.transition_run_status(
        "validated-lease-session", run.run_id, RunStatus.RUNNING.value
    )
    for grant_type, capabilities in (
        ("external_file_read", ["read", "external_path"]),
        ("external_file_write", ["write", "external_path"]),
    ):
        session_manager.add_permission_grant(
            "validated-lease-session",
            grant_type=grant_type,
            target_kind="exact_file",
            target=str(external.resolve()),
            capabilities=capabilities,
        )

    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    middleware = VersionedPatchMiddleware(
        PermissionedCompositeBackend(
            default=workspace_backend,
            routes={
                "/workspace/": workspace_backend,
                "/scratch/": FilesystemBackend(root_dir=scratch, virtual_mode=True),
            },
            session_id="validated-lease-session",
            workspace_root=workspace,
        )
    )
    stage_tool = next(item for item in middleware.tools if item.name == "stage_external_artifact")
    commit_tool = next(item for item in middleware.tools if item.name == "commit_external_artifact")
    runtime = _runtime(
        "call-stage",
        session_id="validated-lease-session",
        run_id="run-1",
        query_id="query-1",
    )
    staged = stage_tool.func(file_path=str(external.resolve()), runtime=runtime)
    lease_id = staged.content.split("lease_id=", 1)[1].split(";", 1)[0]
    lease = session_manager.get_external_artifact_lease(
        "validated-lease-session", lease_id
    )
    assert lease is not None
    staged_host = scratch / str(lease["staged_path"]).removeprefix("/scratch/")
    draft = "const value = 2;\n"
    staged_host.write_text(draft, encoding="utf-8")
    draft_sha = _digest(draft)

    missing = commit_tool.func(
        lease_id=lease_id,
        file_path=str(external.resolve()),
        expected_draft_sha256=draft_sha,
        validation_receipt_ids=[],
        runtime=_runtime(
            "call-missing-receipt",
            session_id="validated-lease-session",
            run_id="run-1",
            query_id="query-1",
        ),
    )
    assert missing.status == "error"
    assert "ValidationReceipt" in missing.content
    assert external.read_text(encoding="utf-8") == "const value = 1;\n"

    mismatched = commit_tool.func(
        lease_id=lease_id,
        file_path=str(external.resolve()),
        expected_draft_sha256="sha256:" + "0" * 64,
        validation_receipt_ids=[],
        runtime=_runtime(
            "call-wrong-draft",
            session_id="validated-lease-session",
            run_id="run-1",
            query_id="query-1",
        ),
    )
    assert mismatched.status == "error"
    assert "draft" in mismatched.content.lower()

    def persist_receipt(receipt: ValidationReceipt, *, call_id: str) -> None:
        activation = VerificationActivation(
            activation_id=f"activation-{call_id}",
            run_id="run-1",
            query_id="query-1",
            tool_call_id=call_id,
            tool_name="execute",
            pack="code",
                status="succeeded",
            evidence_refs=[
                {"kind": "validation_receipt", **receipt.model_dump(mode="json"), "material": True}
            ],
        )
        session_manager.append_run_verification_activation(
            "validated-lease-session",
            "run-1",
            activation.model_dump(mode="json"),
        )

    failed = ValidationReceipt(
        validation_receipt_id="validation-failed",
        run_id="run-1",
        validator_kind="javascript_syntax",
        artifact_refs=[
            {
                "artifact_id": "artifact-app-js",
                "content_sha256": draft_sha,
                "path": str(external.resolve()),
            }
        ],
        command_evidence_ref="sha256:failed",
        exit_code=1,
        checks_failed=1,
        status="failed",
        failure_class="invocation_failure",
        blocking=True,
        commit_authority=True,
        obligation_key="javascript_syntax:node-check/v1",
        created_at=1.0,
    )
    persist_receipt(failed, call_id="call-failed")
    blocked = commit_tool.func(
        lease_id=lease_id,
        file_path=str(external.resolve()),
        expected_draft_sha256=draft_sha,
        validation_receipt_ids=[failed.validation_receipt_id],
        runtime=_runtime(
            "call-blocked",
            session_id="validated-lease-session",
            run_id="run-1",
            query_id="query-1",
        ),
    )
    assert blocked.status == "error"
    assert "failed" in blocked.content.lower() or "失败" in blocked.content

    different_obligation = ValidationReceipt(
        validation_receipt_id="validation-ui-passed",
        run_id="run-1",
        validator_kind="artifact_ui_contract",
        validator_version="heatmap_year_contract/v1",
        artifact_refs=failed.artifact_refs,
        command_evidence_ref="sha256:ui-passed",
        exit_code=0,
        status="passed",
        commit_authority=True,
        obligation_key="artifact_ui_contract:heatmap_year_contract/v1",
        created_at=2.0,
    )
    persist_receipt(different_obligation, call_id="call-ui-passed")
    still_blocked = commit_tool.func(
        lease_id=lease_id,
        file_path=str(external.resolve()),
        expected_draft_sha256=draft_sha,
        validation_receipt_ids=[different_obligation.validation_receipt_id],
        runtime=_runtime(
            "call-still-blocked",
            session_id="validated-lease-session",
            run_id="run-1",
            query_id="query-1",
        ),
    )
    assert still_blocked.status == "error"
    assert failed.validation_receipt_id in still_blocked.content

    passed = ValidationReceipt(
        validation_receipt_id="validation-passed",
        run_id="run-1",
        validator_kind="javascript_syntax",
        artifact_refs=failed.artifact_refs,
        command_evidence_ref="sha256:passed",
        exit_code=0,
        checks_failed=0,
        status="passed",
        blocking=True,
        commit_authority=True,
        obligation_key="javascript_syntax:node-check/v1",
        created_at=3.0,
    )
    persist_receipt(passed, call_id="call-passed")
    unrelated = ValidationReceipt(
        validation_receipt_id="validation-unrelated",
        run_id="run-1",
        validator_kind="artifact_ui_contract",
        validator_version="other-contract/v1",
        artifact_refs=[
            {
                "artifact_id": "artifact-other-js",
                "content_sha256": draft_sha,
                "path": str((external.parent / "other.js").resolve()),
            }
        ],
        command_evidence_ref="sha256:unrelated",
        exit_code=0,
        status="passed",
        commit_authority=True,
        obligation_key="artifact_ui_contract:other-contract/v1",
        created_at=3.5,
    )
    persist_receipt(unrelated, call_id="call-unrelated")
    committed = commit_tool.func(
        lease_id=lease_id,
        file_path=str(external.resolve()),
        expected_draft_sha256=draft_sha,
        validation_receipt_ids=[
            passed.validation_receipt_id,
            unrelated.validation_receipt_id,
        ],
        runtime=_runtime(
            "call-commit-validated",
            session_id="validated-lease-session",
            run_id="run-1",
            query_id="query-1",
        ),
    )
    assert committed.status == "success"
    assert external.read_text(encoding="utf-8") == draft
    delivered = session_manager.list_delivered_artifacts("validated-lease-session")
    assert len(delivered) == 1
    assert delivered[0]["target_path"] == str(external.resolve())
    assert delivered[0]["content_sha256"] == draft_sha
    assert delivered[0]["validation_receipt_ids"] == [passed.validation_receipt_id]

    commit_activation = next(
        item
        for item in build_verification_activations(
            run_id="run-1",
            query_id="query-1",
            tool_call_id="call-commit-validated",
            tool_name="commit_external_artifact",
            args={
                "lease_id": lease_id,
                "file_path": str(external.resolve()),
                "expected_draft_sha256": draft_sha,
                "validation_receipt_ids": [passed.validation_receipt_id],
            },
            result=committed,
            session_id="validated-lease-session",
            workspace_path=str(workspace),
        )
        if item.pack == "code"
    )
    assert [
        item["validation_receipt_id"]
        for item in commit_activation.evidence_refs
        if item.get("kind") == "validation_receipt"
    ] == [passed.validation_receipt_id]

    evaluation = _evaluate_code_validation(
        "code_validation",
        {"verification_activations": [commit_activation.model_dump(mode="json")]},
        {},
    )
    assert evaluation.passed is True


def test_artifact_failure_cannot_be_cleared_by_same_hash_success() -> None:
    from graph.middlewares.versioned_patch import _accepted_receipts_for_target

    target = "/external/report.html"
    digest = "sha256:" + "d" * 64
    artifact_ref = [{"path": target, "content_sha256": digest}]
    failed = {
        "validation_receipt_id": "browser-artifact-failed",
        "validator_kind": "browser_runtime",
        "validator_version": "puddingclaw-html-e2e/v1",
        "obligation_key": "browser_runtime:puddingclaw-html-e2e/v1",
        "commit_authority": True,
        "artifact_refs": artifact_ref,
        "status": "failed",
        "failure_class": "artifact_failure",
        "content_observed": True,
        "exit_code": 1,
        "checks_failed": 1,
        "blocking": True,
        "created_at": 1.0,
    }
    passed = {
        **failed,
        "validation_receipt_id": "browser-later-passed",
        "status": "passed",
        "failure_class": None,
        "exit_code": 0,
        "checks_failed": 0,
        "created_at": 2.0,
    }

    accepted, blocking = _accepted_receipts_for_target(
        [failed, passed],
        target_path=target,
        content_sha256=digest,
        selected_receipt_ids={"browser-later-passed"},
    )

    assert [item["validation_receipt_id"] for item in accepted] == [
        "browser-later-passed"
    ]
    assert [item["validation_receipt_id"] for item in blocking] == [
        "browser-artifact-failed"
    ]


def test_external_artifact_lease_can_continue_in_another_run_of_same_goal(tmp_path):
    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
    from graph.session_manager import session_manager

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    external = tmp_path / "external" / "report.txt"
    state.mkdir()
    workspace.mkdir()
    scratch.mkdir()
    external.parent.mkdir()
    external.write_text("before", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("cross-run-lease-session")
    for grant_type, capabilities in (
        ("external_file_read", ["read", "external_path"]),
        ("external_file_write", ["write", "external_path"]),
    ):
        session_manager.add_permission_grant(
            "cross-run-lease-session",
            grant_type=grant_type,
            target_kind="exact_file",
            target=str(external.resolve()),
            capabilities=capabilities,
        )
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    middleware = VersionedPatchMiddleware(
        PermissionedCompositeBackend(
            default=workspace_backend,
            routes={
                "/workspace/": workspace_backend,
                "/scratch/": FilesystemBackend(root_dir=scratch, virtual_mode=True),
            },
            session_id="cross-run-lease-session",
            workspace_root=workspace,
        )
    )
    stage_tool = next(tool for tool in middleware.tools if tool.name == "stage_external_artifact")
    commit_tool = next(tool for tool in middleware.tools if tool.name == "commit_external_artifact")
    staged = stage_tool.func(
        file_path=str(external.resolve()),
        runtime=_runtime(
            "call-stage-cross-run",
            session_id="cross-run-lease-session",
            run_id="run-1",
            query_id="query-1",
            goal_id="goal-1",
            goal_revision=2,
        ),
    )
    lease_id = staged.content.split("lease_id=", 1)[1].split(";", 1)[0]
    lease = session_manager.get_external_artifact_lease("cross-run-lease-session", lease_id)
    wrong_revision = commit_tool.func(
        lease_id=lease_id,
        file_path=str(external.resolve()),
        expected_source_sha256=lease["expected_source_sha256"],
        runtime=_runtime(
            "call-commit-cross-run",
            session_id="cross-run-lease-session",
            run_id="run-2",
            query_id="query-2",
            goal_id="goal-1",
            goal_revision=3,
        ),
    )
    assert wrong_revision.status == "error"
    assert "different execution scope" in wrong_revision.content
    assert external.read_text(encoding="utf-8") == "before"

    continued = commit_tool.func(
        lease_id=lease_id,
        file_path=str(external.resolve()),
        expected_source_sha256=lease["expected_source_sha256"],
        runtime=_runtime(
            "call-commit-next-run",
            session_id="cross-run-lease-session",
            run_id="run-2",
            query_id="query-2",
            goal_id="goal-1",
            goal_revision=2,
        ),
    )
    assert continued.status == "success"
    assert external.read_text(encoding="utf-8") == "before"


def test_expired_external_artifact_lease_can_be_renewed_without_losing_staged_edits(
    tmp_path,
):
    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
    from graph.session_manager import session_manager

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    external = tmp_path / "external" / "report.txt"
    state.mkdir()
    workspace.mkdir()
    scratch.mkdir()
    external.parent.mkdir()
    external.write_text("before", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("expired-lease-session")
    for grant_type, capabilities in (
        ("external_file_read", ["read", "external_path"]),
        ("external_file_write", ["write", "external_path"]),
    ):
        session_manager.add_permission_grant(
            "expired-lease-session",
            grant_type=grant_type,
            target_kind="exact_file",
            target=str(external.resolve()),
            capabilities=capabilities,
        )

    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    middleware = VersionedPatchMiddleware(
        PermissionedCompositeBackend(
            default=workspace_backend,
            routes={
                "/workspace/": workspace_backend,
                "/scratch/": FilesystemBackend(root_dir=scratch, virtual_mode=True),
            },
            session_id="expired-lease-session",
            workspace_root=workspace,
        )
    )
    stage_tool = next(
        tool for tool in middleware.tools if tool.name == "stage_external_artifact"
    )
    commit_tool = next(
        tool for tool in middleware.tools if tool.name == "commit_external_artifact"
    )
    context = {
        "session_id": "expired-lease-session",
        "run_id": "run-1",
        "query_id": "query-1",
    }
    staged = stage_tool.func(
        file_path=str(external.resolve()),
        runtime=_runtime("call-stage", **context),
    )
    lease_id = staged.content.split("lease_id=", 1)[1].split(";", 1)[0]
    lease = session_manager.get_external_artifact_lease(
        "expired-lease-session",
        lease_id,
    )
    assert lease is not None
    staged_host_path = scratch / str(lease["staged_path"]).removeprefix(
        "/scratch/"
    )
    staged_host_path.write_text("after", encoding="utf-8")
    lease["expires_at"] = 0
    session_manager.upsert_external_artifact_lease(
        "expired-lease-session",
        lease,
    )

    expired = commit_tool.func(
        lease_id=lease_id,
        file_path=str(external.resolve()),
        expected_source_sha256=lease["expected_source_sha256"],
        runtime=_runtime("call-expired", **context),
    )
    assert expired.status == "error"
    assert "expired" in expired.content

    renewed = stage_tool.func(
        file_path=str(external.resolve()),
        runtime=_runtime("call-restage", **context),
    )
    assert renewed.status == "success"
    assert "renewed after expiry" in renewed.content
    assert f"lease_id={lease_id}" in renewed.content
    assert staged_host_path.read_text(encoding="utf-8") == "after"

    committed = commit_tool.func(
        lease_id=lease_id,
        file_path=str(external.resolve()),
        expected_source_sha256=lease["expected_source_sha256"],
        runtime=_runtime("call-commit", **context),
    )
    assert committed.status == "success"
    assert external.read_text(encoding="utf-8") == "after"


def test_missing_external_artifact_draft_is_rehydrated_from_current_source(tmp_path):
    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
    from graph.session_manager import session_manager

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    external = tmp_path / "external" / "report.html"
    for path in (state, workspace, scratch, external.parent):
        path.mkdir(exist_ok=True)
    external.write_text("source", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("missing-draft-session")
    session_manager.add_permission_grant(
        "missing-draft-session",
        grant_type="external_file_read",
        target_kind="exact_file",
        target=str(external.resolve()),
        capabilities=["read", "external_path"],
    )
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    middleware = VersionedPatchMiddleware(
        PermissionedCompositeBackend(
            default=workspace_backend,
            routes={
                "/workspace/": workspace_backend,
                "/scratch/": FilesystemBackend(root_dir=scratch, virtual_mode=True),
            },
            session_id="missing-draft-session",
            workspace_root=workspace,
        )
    )
    stage_tool = next(
        tool for tool in middleware.tools if tool.name == "stage_external_artifact"
    )
    context = {
        "session_id": "missing-draft-session",
        "run_id": "run-1",
        "query_id": "query-1",
        "goal_id": "goal-1",
        "goal_revision": 1,
    }
    staged = stage_tool.func(
        file_path=str(external.resolve()),
        runtime=_runtime("call-stage", **context),
    )
    lease_id = staged.content.split("lease_id=", 1)[1].split(";", 1)[0]
    lease = session_manager.get_external_artifact_lease(
        "missing-draft-session",
        lease_id,
    )
    assert lease is not None
    staged_host = scratch / str(lease["staged_path"]).removeprefix("/scratch/")
    staged_host.unlink()

    recovered = stage_tool.func(
        file_path=str(external.resolve()),
        runtime=_runtime("call-restage", **context),
    )

    assert recovered.status == "success"
    assert "rehydrated from the current source" in recovered.content
    assert staged_host.read_text(encoding="utf-8") == "source"
