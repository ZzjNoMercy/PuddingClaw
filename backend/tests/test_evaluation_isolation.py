import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from evaluation.candidate import CandidateRequest, resolve_candidate, verify_candidate_snapshot
from evaluation.evidence import EvaluationEvidenceCallback
from graph.deepagents_manager import DeepAgentsAgentManager, EvaluationToolBoundaryMiddleware


def test_evaluation_hooks_are_opt_in_and_normal_memory_path_is_unchanged(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PUDDINGCLAW_EVALUATION_RUNTIME_ROOT", raising=False)
    manager = DeepAgentsAgentManager()
    manager.initialize(tmp_path / "backend", user_root=tmp_path)
    assert manager._memory_dir_for(None) == tmp_path / "memory" / "global"

    signature = inspect.signature(manager.astream)
    assert signature.parameters["callbacks_override"].default is None
    assert signature.parameters["evaluation_tool_allowlist"].default is None
    assert signature.parameters["disable_mcp"].default is False
    assert signature.parameters["evaluation_builtin_tool_allowlist"].default is None

    isolated = tmp_path / "isolated"
    monkeypatch.setenv("PUDDINGCLAW_EVALUATION_RUNTIME_ROOT", str(isolated))
    assert manager._memory_dir_for("project") == isolated / "memory" / "projects" / "project"


def test_evaluation_callback_marks_unstructured_tool_inputs_incomplete():
    callback = EvaluationEvidenceCallback()
    callback.on_tool_start({"name": "search"}, "truncated input", run_id="run-1", inputs=None)
    callback.on_tool_end("output", run_id="run-1")
    evidence = callback.evidence()
    assert [call.name for call in evidence.tool_calls] == ["search"]
    assert "tool_arguments" not in evidence.available_kinds
    assert evidence.metadata["capture_complete"] is False


def test_phase_one_candidate_cannot_enable_production_custom_tools():
    with pytest.raises(ValidationError):
        CandidateRequest(name="unsafe", tool_allowlist=["terminal"])
    with pytest.raises(ValidationError):
        CandidateRequest(name="unsafe-project", project_id="prod-project")
    with pytest.raises(ValidationError):
        CandidateRequest(name="phantom-config", config={"temperature": 0})


def test_candidate_fingerprint_captures_effective_model_binding(tmp_path: Path, monkeypatch):
    import config

    monkeypatch.setattr(
        config,
        "get_fallback_llm_config",
        lambda model_id_override=None, thinking_level=None: {
            "model": model_id_override or "default-model",
            "thinking_level": thinking_level,
            "api_key": "never-fingerprint-secrets",
        },
    )
    first = resolve_candidate(tmp_path, CandidateRequest(name="A", llm_model_id="model-a"))
    second = resolve_candidate(tmp_path, CandidateRequest(name="A", llm_model_id="model-b"))

    assert first.fingerprint != second.fingerprint
    assert first.config["effective_llm"]["api_key"] == "[REDACTED]"
    assert first.config["capability_profile"] == "isolated_workspace_core@1"

    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "policy.py").write_text("VERSION = 2", encoding="utf-8")
    assert "source_manifest_hash" in verify_candidate_snapshot(tmp_path, first)


def test_candidate_fingerprint_captures_user_home_skills(tmp_path: Path, monkeypatch):
    import config

    home = tmp_path / ".puddingclaw"
    skill = home / "skills" / "user-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# User Skill\n\nversion one\n", encoding="utf-8")
    monkeypatch.setenv("PUDDINGCLAW_HOME", str(home))
    monkeypatch.setattr(
        config,
        "get_fallback_llm_config",
        lambda **_kwargs: {"model": "fixture"},
    )

    candidate = resolve_candidate(tmp_path, CandidateRequest(name="skills"))
    assert candidate.config["user_skill_hash"]
    assert candidate.config["bundled_skill_hash"]

    skill.write_text("# User Skill\n\nversion two\n", encoding="utf-8")
    drift = verify_candidate_snapshot(tmp_path, candidate)
    assert "user_skill_hash" in drift
    assert "skill_hash" in drift

    changed = resolve_candidate(tmp_path, CandidateRequest(name="skills"))
    (skill.parent / "run.js").write_text("export default 1;\n", encoding="utf-8")
    script_drift = verify_candidate_snapshot(tmp_path, changed)
    assert "user_skill_hash" in script_drift
    assert "skill_hash" in script_drift


def test_evaluation_boundary_filters_and_rejects_deepagents_builtin_tools():
    middleware = EvaluationToolBoundaryMiddleware({"read_file"})

    class Request:
        tools = [{"name": "read_file"}, {"name": "execute"}]
        tool_call = {"name": "execute", "id": "call-1"}

        def override(self, **updates):
            clone = Request()
            clone.tools = updates.get("tools", self.tools)
            return clone

    visible = middleware._filter_request(Request())
    denied = middleware.wrap_tool_call(Request(), lambda request: "executed")

    assert visible.tools == [{"name": "read_file"}]
    assert denied.status == "error"
    assert "disabled" in str(denied.content)
