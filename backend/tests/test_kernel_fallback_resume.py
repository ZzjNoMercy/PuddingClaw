from __future__ import annotations

import asyncio

import pytest

from graph.kernel_fallback_resume import KernelFallbackResumeRegistry
from graph.session_manager import SessionManager


def _request_kwargs() -> dict[str, object]:
    return {
        "session_id": "session-one",
        "run_id": "run-one",
        "query_id": "query-one",
        "tool_call_id": "tool-one",
        "workspace_identity": "sha256:workspace",
        "configured_mode": "kernel",
        "availability_class": "stable",
        "reason_code": "probe_failed",
        "reason": "Kernel unsupported on this host",
        "probe_fingerprint": "sha256:probe",
    }


def test_kernel_fallback_registry_replays_same_probe_request() -> None:
    async def scenario() -> None:
        registry = KernelFallbackResumeRegistry()

        first = registry.create(**_request_kwargs())
        second = registry.create(**_request_kwargs())

        assert second["request_id"] == first["request_id"]
        assert second["status"] == "pending"

    asyncio.run(scenario())


def test_kernel_fallback_registry_rejects_stale_or_conflicting_resolution() -> None:
    async def scenario() -> None:
        registry = KernelFallbackResumeRegistry()
        request = registry.create(**_request_kwargs())

        with pytest.raises(RuntimeError, match="stale"):
            registry.resolve(request["request_id"], "fallback_once", request_version=0)

        decision, resumed = registry.resolve(
            request["request_id"],
            "fallback_once",
            request_version=request["version"],
        )

        assert decision == {"action": "fallback_once"}
        assert resumed is True
        assert await registry.wait(request["request_id"]) == {"action": "fallback_once"}

        with pytest.raises(RuntimeError, match="different action"):
            registry.resolve(
                request["request_id"],
                "reject",
                request_version=request["version"],
            )

    asyncio.run(scenario())


def test_run_execution_fallback_is_persisted_as_audit_record(tmp_path) -> None:
    manager = SessionManager()
    manager.initialize(tmp_path)
    manager.create_session(
        "session-one",
        metadata={
            "harness": {
                "runs": {
                    "run-one": {
                        "run_id": "run-one",
                        "status": "running",
                        "config_snapshot": {
                            "execution": {
                                "backend_mode": "kernel",
                                "configured_mode": "kernel",
                            }
                        },
                    }
                },
                "run_order": ["run-one"],
            }
        },
    )
    request = {
        **_request_kwargs(),
        "id": "kernel-fallback-one",
        "request_id": "kernel-fallback-one",
    }

    run = manager.record_run_execution_fallback(
        "session-one",
        "run-one",
        scope="run",
        request=request,
    )

    execution = run["config_snapshot"]["execution"]
    assert execution["effective_runner"] == "spawn"
    assert execution["fallback_scope"] == "run"
    assert execution["fallback_request_id"] == "kernel-fallback-one"
    assert execution["kernel_fallback"]["probe_fingerprint"] == "sha256:probe"
