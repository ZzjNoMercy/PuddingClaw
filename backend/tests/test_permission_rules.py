from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ToolCallRequest

from graph.permission_policy import (
    PermissionRuleDecision,
    PermissionRuleError,
    RunPermissionContext,
    compile_permission_rules,
    evaluate_permission_rules,
    resolve_filesystem_mode,
)
from harness.tool_execution import ToolExecutionPipeline


@pytest.mark.parametrize(
    ("approval_mode", "backend_mode", "configured_mode", "expected"),
    [
        ("smart", "spawn", None, "unrestricted"),
        ("smart", "kernel", None, "unrestricted"),
        ("smart", "spawn", "restricted", "restricted"),
        ("strict", "spawn", "unrestricted", "restricted"),
        ("smart", "docker", "unrestricted", "restricted"),
    ],
)
def test_filesystem_mode_resolution_is_shared_by_every_run_surface(
    approval_mode: str,
    backend_mode: str,
    configured_mode: str | None,
    expected: str,
) -> None:
    assert resolve_filesystem_mode(
        approval_mode=approval_mode,
        backend_mode=backend_mode,
        configured_mode=configured_mode,
    ) == expected


def test_allow_rule_is_constrained_by_effect_envelope() -> None:
    rules = compile_permission_rules(
        [
            {
                "tool": "execute",
                "pattern": "pdftotext *",
                "decision": "allow",
                "scope": "project",
                "constraints": {
                    "network": False,
                    "credentials": False,
                    "destructive": False,
                    "package_install": False,
                    "write_scope": "workspace_or_scratch",
                },
            }
        ],
        source="project",
    )

    assert evaluate_permission_rules(
        rules,
        tool="execute",
        pattern="pdftotext report.pdf *",
        effects={"write_scope": "none"},
    ) is PermissionRuleDecision.ALLOW
    assert evaluate_permission_rules(
        rules,
        tool="execute",
        pattern="pdftotext report.pdf *",
        effects={"network": True, "write_scope": "none"},
    ) is None


@pytest.mark.parametrize("pattern", ["python3 *", "python3 -c *", "bash -c *"])
def test_allow_rule_rejects_unbounded_interpreter_pattern(pattern: str) -> None:
    with pytest.raises(PermissionRuleError):
        compile_permission_rules(
            [{"tool": "execute", "pattern": pattern, "decision": "allow"}]
        )


def test_interpreter_commands_never_receive_reusable_pattern_scope(tmp_path) -> None:
    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode="spawn",
        permission_context=RunPermissionContext.from_config_snapshot(
            {"permissions": {"approval_mode": "smart"}, "execution": {"backend_mode": "spawn"}}
        ),
    )
    request = ToolCallRequest(
        tool_call={
            "id": "call-python",
            "name": "execute",
            "args": {"command": "python3 -c 'print(1)'"},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )

    assert pipeline._session_grant_scope(request) is None


def test_shell_credentials_are_redacted_before_permission_preview() -> None:
    preview = ToolExecutionPipeline._redact_shell_preview(
        "curl -H 'Authorization: Bearer super-secret' --token=another-secret https://api.example"
    )

    assert "super-secret" not in preview
    assert "another-secret" not in preview
    assert "<redacted>" in preview


def test_literal_shell_credential_and_network_are_never_auto_allowed(tmp_path) -> None:
    pipeline = ToolExecutionPipeline(known_tools={"execute"}, backend_mode="spawn")
    request = ToolCallRequest(
        tool_call={
            "id": "call-secret-network",
            "name": "execute",
            "args": {"command": "curl -H 'Authorization: Bearer super-secret' https://api.example"},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )

    result = pipeline._preflight(request)

    assert result.decision.value == "ask"
    assert result.reason == "credential_network_coupling:literal_secret"
