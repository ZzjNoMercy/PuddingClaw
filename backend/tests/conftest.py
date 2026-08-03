"""Global test isolation for process-external Agent runtimes."""

from __future__ import annotations

from copy import deepcopy

import pytest

from harness.task_profiles import TaskProfileClassifier

_ORIGINAL_RUBRIC_PROFILE_CLASSIFIER = None


@pytest.fixture(autouse=True)
def isolate_deepagents_from_real_docker(monkeypatch):
    """Keep unit/E2E agent tests from inheriting a developer's Docker setting.

    DockerWorkspaceBackend has dedicated contract tests that call its builder
    directly. Tests exercising DeepAgents orchestration must stay hermetic even
    when ``backend/config.json`` enables the real reusable project sandbox.
    """

    from graph import deepagents_manager as manager_module

    global _ORIGINAL_RUBRIC_PROFILE_CLASSIFIER
    if _ORIGINAL_RUBRIC_PROFILE_CLASSIFIER is None:
        _ORIGINAL_RUBRIC_PROFILE_CLASSIFIER = (
            manager_module.DeepAgentsAgentManager._classify_rubric_profile
        )

    original = manager_module.build_workspace_execution_backend

    def build_isolated(workspace_path, terminal_config):
        isolated = deepcopy(terminal_config)
        isolated.pop("sandbox_mode", None)
        isolated["docker_enabled"] = False
        return original(workspace_path, isolated)

    monkeypatch.setattr(
        manager_module,
        "build_workspace_execution_backend",
        build_isolated,
    )

    async def classify_without_external_model(
        _self,
        *,
        objective,
        analytics_model_id,
        model_override,
    ):
        del model_override
        return TaskProfileClassifier.classify(
            message=objective,
            analytics_model_id=analytics_model_id,
        )

    monkeypatch.setattr(
        manager_module.DeepAgentsAgentManager,
        "_classify_rubric_profile",
        classify_without_external_model,
    )


@pytest.fixture
def use_real_rubric_profile_classifier(
    monkeypatch,
    isolate_deepagents_from_real_docker,
):
    """Opt a focused test into the production semantic classifier path."""

    del isolate_deepagents_from_real_docker
    from graph import deepagents_manager as manager_module

    monkeypatch.setattr(
        manager_module.DeepAgentsAgentManager,
        "_classify_rubric_profile",
        _ORIGINAL_RUBRIC_PROFILE_CLASSIFIER,
    )
