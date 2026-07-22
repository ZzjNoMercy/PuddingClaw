from __future__ import annotations

import hashlib
import shlex
from pathlib import Path

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import ExecuteResponse
from langchain_core.messages import ToolMessage

from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
from graph.session_manager import session_manager
from harness.coordinators import HarnessRunCoordinator
from harness.models import RunStatus
from harness.verification_activations import build_verification_activations


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _setup(tmp_path: Path):
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    outside = tmp_path / "outside"
    for directory in (state, workspace, external, outside):
        directory.mkdir()
    session_manager.initialize(state)
    session_manager.create_session("broker-session")
    coordinator = HarnessRunCoordinator(session_manager)
    run, _goal = coordinator.start_run(
        session_id="broker-session",
        query_id="broker-query",
        objective="edit the authorized external directory",
        goal_mode=False,
        verification_enabled=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    for access in ("read", "write"):
        session_manager.add_permission_grant(
            "broker-session",
            grant_type=f"external_directory_{access}",
            target_kind="exact_directory",
            target=str(external.resolve()),
            capabilities=[access, "recursive", "external_path"],
            scope="run",
            metadata={"run_id": run.run_id},
        )
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="broker-session",
        run_id=run.run_id,
        query_id=run.query_id,
        workspace_root=workspace,
    )
    return backend, external, outside, run


def test_authorized_directory_uses_direct_host_file_tools_and_receipts(tmp_path: Path) -> None:
    backend, external, _outside, run = _setup(tmp_path)
    target = external / "report.html"
    target.write_text("<option>2024</option>\n", encoding="utf-8")

    read = backend.read(str(target))
    assert read.error is None
    assert "2024" in str((read.file_data or {}).get("content") or "")

    listing = backend.ls(str(external))
    assert listing.error is None
    assert {item["path"] for item in listing.entries or []} == {str(target)}

    matches = backend.grep("2024", path=str(external), glob="*.html")
    assert matches.error is None
    assert [item["path"] for item in matches.matches or []] == [str(target)]

    edited = backend.edit(str(target), "2024", "2026")
    assert edited.error is None
    assert edited.path == str(target)
    assert target.read_text(encoding="utf-8") == "<option>2026</option>\n"

    created = backend.write(str(external / "notes.txt"), "verified\n")
    assert created.error is None
    assert (external / "notes.txt").read_text(encoding="utf-8") == "verified\n"

    receipts = session_manager.list_external_mutation_receipts(
        "broker-session",
        run_id=run.run_id,
    )
    assert [item["operation"] for item in receipts] == ["edit", "create"]
    assert all(item["atomic"] is True for item in receipts)
    assert all(item["permission_grant_id"] for item in receipts)


def test_authorized_directory_rejects_symlink_escape(tmp_path: Path) -> None:
    backend, external, outside, _run = _setup(tmp_path)
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = external / "escape.txt"
    link.symlink_to(secret)

    assert backend.can_access_external_path(str(link), access="read") is False
    assert backend.can_access_external_path(str(link), access="write") is False


class _ValidationBackend:
    def __init__(self, scratch: Path) -> None:
        self.scratch = scratch

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        del timeout
        candidate = shlex.split(command)[-1]
        assert candidate.startswith("/scratch/")
        content = (self.scratch / candidate.removeprefix("/scratch/")).read_text(
            encoding="utf-8"
        )
        if "INVALID" in content:
            return ExecuteResponse(output="SyntaxError: invalid candidate", exit_code=1)
        return ExecuteResponse(output="syntax ok", exit_code=0)


def test_broker_validation_bridge_binds_formal_hash_and_blocks_bad_bytes(
    tmp_path: Path,
) -> None:
    backend, external, _outside, run = _setup(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    backend.execution_backend = _ValidationBackend(scratch)
    backend.execution_scratch_host_path = str(scratch)
    target = external / "app.js"
    target.write_text("const value = 1;\n", encoding="utf-8")

    valid = backend.edit(str(target), "1", "2")
    assert valid.error is None
    assert target.read_text(encoding="utf-8") == "const value = 2;\n"
    receipt = session_manager.list_external_mutation_receipts(
        "broker-session",
        run_id=run.run_id,
    )[-1]
    validation = receipt["validation_receipt"]
    assert validation["status"] == "passed"
    assert validation["commit_authority"] is True
    assert validation["artifact_refs"] == [
        {
            "artifact_id": validation["artifact_refs"][0]["artifact_id"],
            "path": str(target),
            "content_sha256": receipt["after_sha256"],
        }
    ]
    assert not (scratch / "validation").exists()
    activations = build_verification_activations(
        run_id=run.run_id,
        query_id=run.query_id,
        tool_call_id="call-edit",
        tool_name="edit_file",
        args={"file_path": str(target), "old_string": "1", "new_string": "2"},
        result=ToolMessage(
            content=f"Updated {target}",
            tool_call_id="call-edit",
            name="edit_file",
            status="success",
        ),
        session_id="broker-session",
        workspace_path=str(tmp_path / "workspace"),
    )
    assert any(
        ref.get("validation_receipt_id") == validation["validation_receipt_id"]
        for activation in activations
        for ref in activation.evidence_refs
    )

    invalid = backend.edit(str(target), "2", "INVALID")
    assert invalid.error is not None
    assert invalid.error.startswith("validation_failed:")
    assert target.read_text(encoding="utf-8") == "const value = 2;\n"
    assert len(
        session_manager.list_external_mutation_receipts(
            "broker-session",
            run_id=run.run_id,
        )
    ) == 1


def test_rewind_restores_only_current_run_when_hashes_still_match(tmp_path: Path) -> None:
    backend, external, _outside, _run = _setup(tmp_path)
    target = external / "report.txt"
    created = external / "notes.txt"
    target.write_text("before\n", encoding="utf-8")

    assert backend.edit(str(target), "before", "after").error is None
    assert backend.write(str(created), "created\n").error is None
    result = backend.rewind_external_file_changes()

    assert result["status"] == "completed"
    assert len(result["rewound_receipt_ids"]) == 2
    assert target.read_text(encoding="utf-8") == "before\n"
    assert not created.exists()
    assert backend.rewind_external_file_changes()["status"] == "noop"


def test_rewind_refuses_to_overwrite_concurrent_host_change(tmp_path: Path) -> None:
    backend, external, _outside, _run = _setup(tmp_path)
    target = external / "report.txt"
    target.write_text("before\n", encoding="utf-8")
    assert backend.edit(str(target), "before", "after").error is None
    target.write_text("concurrent\n", encoding="utf-8")

    result = backend.rewind_external_file_changes()

    assert result["status"] == "conflict"
    assert result["error"].startswith("conflict:")
    assert target.read_text(encoding="utf-8") == "concurrent\n"


def test_multi_file_transaction_is_all_or_nothing_and_journaled(tmp_path: Path) -> None:
    backend, external, _outside, run = _setup(tmp_path)
    first = external / "first.txt"
    second = external / "second.txt"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")

    completed = backend.apply_external_file_transaction(
        [
            {
                "file_path": str(first),
                "expected_sha256": _digest("one\n"),
                "content": "ONE\n",
            },
            {
                "file_path": str(second),
                "expected_sha256": _digest("two\n"),
                "content": "TWO\n",
            },
        ]
    )
    assert completed["status"] == "completed"
    assert first.read_text(encoding="utf-8") == "ONE\n"
    assert second.read_text(encoding="utf-8") == "TWO\n"
    receipts = [
        item
        for item in session_manager.list_external_mutation_receipts(
            "broker-session",
            run_id=run.run_id,
        )
        if item.get("transaction_id") == completed["transaction_id"]
    ]
    assert len(receipts) == 2
    assert all(item["diff"] for item in receipts)

    conflict = backend.apply_external_file_transaction(
        [
            {
                "file_path": str(first),
                "expected_sha256": _digest("ONE\n"),
                "content": "changed-one\n",
            },
            {
                "file_path": str(second),
                "expected_sha256": _digest("stale\n"),
                "content": "changed-two\n",
            },
        ]
    )
    assert conflict["status"] == "conflict"
    assert first.read_text(encoding="utf-8") == "ONE\n"
    assert second.read_text(encoding="utf-8") == "TWO\n"


def test_multi_file_transaction_validates_every_candidate_before_commit(
    tmp_path: Path,
) -> None:
    backend, external, _outside, _run = _setup(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    backend.execution_backend = _ValidationBackend(scratch)
    backend.execution_scratch_host_path = str(scratch)
    first = external / "first.js"
    second = external / "second.js"
    first.write_text("const first = 1;\n", encoding="utf-8")
    second.write_text("const second = 2;\n", encoding="utf-8")
    result = backend.apply_external_file_transaction(
        [
            {
                "file_path": str(first),
                "expected_sha256": _digest("const first = 1;\n"),
                "content": "const first = 10;\n",
            },
            {
                "file_path": str(second),
                "expected_sha256": _digest("const second = 2;\n"),
                "content": "INVALID\n",
            },
        ]
    )

    assert result["status"] == "validation_failed"
    assert first.read_text(encoding="utf-8") == "const first = 1;\n"
    assert second.read_text(encoding="utf-8") == "const second = 2;\n"
