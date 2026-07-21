"""Global test isolation for process-external Agent runtimes."""

from __future__ import annotations

from copy import deepcopy

import pytest


@pytest.fixture(autouse=True)
def isolate_deepagents_from_real_docker(monkeypatch):
    """Keep unit/E2E agent tests from inheriting a developer's Docker setting.

    DockerWorkspaceBackend has dedicated contract tests that call its builder
    directly. Tests exercising DeepAgents orchestration must stay hermetic even
    when ``backend/config.json`` enables the real reusable project sandbox.
    """

    from graph import deepagents_manager as manager_module

    original = manager_module.build_workspace_execution_backend

    def build_isolated(workspace_path, terminal_config):
        isolated = deepcopy(terminal_config)
        isolated["docker_enabled"] = False
        return original(workspace_path, isolated)

    monkeypatch.setattr(
        manager_module,
        "build_workspace_execution_backend",
        build_isolated,
    )
