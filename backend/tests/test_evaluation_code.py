from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation.code_eval import _read_receipt, _strict_json_equal, prepare_code_repository, verify_code_case
from evaluation.contracts import (
    CodeEvaluationSpec,
    CodeRepositorySpec,
    CodeVerificationCommand,
    CodeVerificationSpec,
    EvalCase,
    EvalDataset,
    EvalExperiment,
    EvalInput,
    ExperimentCandidate,
)
from evaluation.evaluators import evaluator_registry
from evaluation.repository import EvaluationRepository
from evaluation.runner import EvaluationRunner
from evaluation.settings import LangSmithSettings
from evaluation.swebench_adapter import prediction_jsonl, swebench_dataset_from_rows
from evaluation.validation import validate_dataset


def _inline_code() -> CodeEvaluationSpec:
    return CodeEvaluationSpec(
        repository=CodeRepositorySpec(
            kind="inline",
            files={"solution.py": "def add(a, b):\n    raise NotImplementedError\n"},
        ),
        verification=CodeVerificationSpec(
            mode="commands",
            commands=[
                CodeVerificationCommand(
                    command_id="hidden-tests",
                    command="hidden_cases.json",
                )
            ],
            hidden_files={
                "hidden_cases.json": '{"callable":"solution:add","cases":[{"args":[2,3],"expected":5}]}'
            },
        ),
    )


def test_coding_dataset_publishes_with_required_code_evaluator(tmp_path: Path):
    repository = EvaluationRepository(tmp_path / "evaluation.sqlite3")
    dataset = repository.create_dataset(
        EvalDataset(
            name="Coding",
            default_profile="coding_agent@1",
            cases=[
                EvalCase(
                    name="Implement add",
                    input=EvalInput(message="Implement add"),
                    code=_inline_code(),
                )
            ],
        )
    )

    validation = validate_dataset(dataset)
    assert validation.valid is True
    bundle = repository.publish_dataset(dataset.dataset_id, dataset.revision)
    bindings = bundle.dataset.cases[0].resolved_evaluator_bindings
    assert any(item.evaluator_id == "code_verification.v1" for item in bindings)
    assert evaluator_registry.get_registered("code_verification.v1") is not None


def test_code_case_cannot_use_general_profile():
    dataset = EvalDataset(
        name="Wrong profile",
        cases=[EvalCase(name="Code", input=EvalInput(message="fix"), code=_inline_code())],
    )
    validation = validate_dataset(dataset)
    assert validation.valid is False
    assert "coding_profile_required" in {issue.code for issue in validation.issues}


def test_hidden_verifier_files_are_not_exposed_to_agent_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = _inline_code()
    prepare_code_repository(workspace, spec)
    assert not (workspace / ".git").exists()
    assert (workspace.parent / ".workspace-evaluation-git").is_dir()
    (workspace / "solution.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    class FakeVerifier:
        @classmethod
        def probe(cls):
            return True, "ok"

        def execute(self, command: str, *, timeout: int):
            import json
            import shlex

            argv = shlex.split(command)
            assert argv[1] == "-I"
            assert argv[4] == "solution:add"
            assert timeout == 120
            assert not (workspace / "hidden_cases.json").exists()
            Path(argv[6]).write_text(
                json.dumps({"completed": True, "actual": 5}),
                encoding="utf-8",
            )
            return SimpleNamespace(exit_code=0, output="OK", truncated=False)

    import harness.kernel_sandbox as kernel_sandbox

    monkeypatch.setattr(
        kernel_sandbox,
        "kernel_runner_for_profile",
        lambda profile, runtime_root=None: FakeVerifier(),
    )
    result = verify_code_case(workspace, tmp_path / "attempt", spec)

    assert result["status"] == "passed"
    assert result["passed"] is True
    assert result["changed_paths"] == ["solution.py"]
    assert "return a + b" in result["patch"]


def test_candidate_cannot_own_trusted_expectation_comparison(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = _inline_code()
    prepare_code_repository(workspace, spec)
    (workspace / "solution.py").write_text(
        "def add(a, b):\n    return 999\n",
        encoding="utf-8",
    )

    class InspectingVerifier:
        @classmethod
        def probe(cls):
            return True, "ok"

        def execute(self, command: str, *, timeout: int):
            import json
            import shlex

            argv = shlex.split(command)
            runner = Path(argv[2]).read_text(encoding="utf-8")
            assert "expected" not in runner
            assert argv[1] == "-I"
            assert argv[2].endswith("candidate-control/python_callable_runner.py")
            assert argv[3].endswith("verifier-workspace")
            Path(argv[6]).write_text(
                json.dumps({"completed": True, "actual": 999}),
                encoding="utf-8",
            )
            return SimpleNamespace(exit_code=0, output="", truncated=False)

    import harness.kernel_sandbox as kernel_sandbox

    monkeypatch.setattr(
        kernel_sandbox,
        "kernel_runner_for_profile",
        lambda profile, runtime_root=None: InspectingVerifier(),
    )
    result = verify_code_case(workspace, tmp_path / "attempt", spec)
    assert result["status"] == "failed"
    assert result["passed"] is False


def test_receipt_reader_rejects_symlink_and_json_comparison_is_type_strict(tmp_path: Path):
    target = tmp_path / "outside.json"
    target.write_text('{"completed":true,"actual":5}', encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    receipt.symlink_to(target)
    assert _read_receipt(receipt) == {}
    assert _strict_json_equal(True, 1) is False
    assert _strict_json_equal({"value": [True]}, {"value": [1]}) is False


def test_verifier_kernel_probe_failure_is_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = _inline_code()
    prepare_code_repository(workspace, spec)
    (workspace / "solution.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    class UnavailableVerifier:
        @classmethod
        def probe(cls):
            return False, "sandbox unavailable"

    import harness.kernel_sandbox as kernel_sandbox

    monkeypatch.setattr(
        kernel_sandbox,
        "kernel_runner_for_profile",
        lambda profile, runtime_root=None: UnavailableVerifier(),
    )
    result = verify_code_case(workspace, tmp_path / "attempt", spec)
    assert result["status"] == "error"
    assert "unavailable" in result["reason"]


def test_unsafe_code_paths_are_rejected():
    with pytest.raises(ValueError, match="escapes"):
        prepare_code_repository(
            Path.cwd(),
            CodeEvaluationSpec(
                repository=CodeRepositorySpec(kind="inline", files={"../escape.py": "bad"}),
                verification=CodeVerificationSpec(
                    commands=[CodeVerificationCommand(command_id="x", command="test.json")]
                ),
            ),
        )


def test_agent_symlink_is_reconstructed_without_following_host_target(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = _inline_code()
    prepare_code_repository(workspace, spec)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must-not-be-copied", encoding="utf-8")
    (workspace / "candidate_link.txt").symlink_to(outside)

    class UnavailableVerifier:
        @classmethod
        def probe(cls):
            return False, "test stop"

    import harness.kernel_sandbox as kernel_sandbox

    original = kernel_sandbox.kernel_runner_for_profile
    try:
        kernel_sandbox.kernel_runner_for_profile = lambda profile, runtime_root=None: UnavailableVerifier()
        result = verify_code_case(workspace, tmp_path / "attempt", spec)
    finally:
        kernel_sandbox.kernel_runner_for_profile = original

    assert result["status"] == "error"
    copied_link = tmp_path / "attempt" / "verifier-workspace" / "candidate_link.txt"
    assert copied_link.is_symlink()


def test_swebench_import_omits_gold_patch_and_exports_official_prediction():
    gold = "THIS_GOLD_PATCH_MUST_NEVER_BE_STORED"
    dataset = swebench_dataset_from_rows(
        [
            {
                "instance_id": "django__django-12345",
                "repo": "django/django",
                "base_commit": "a" * 40,
                "problem_statement": "Fix the regression",
                "patch": gold,
                "test_patch": "diff --git a/test.py b/test.py",
                "FAIL_TO_PASS": '["tests.test_bug"]',
                "PASS_TO_PASS": '["tests.test_ok"]',
                "version": "4.2",
            }
        ],
        name="Verified sample",
    )
    serialized = dataset.model_dump_json()
    assert gold not in serialized
    assert dataset.default_profile == "coding_agent@1"
    case = dataset.cases[0]
    assert case.case_id == "django__django-12345"
    assert case.code is not None
    assert case.code.repository.swebench is not None
    assert case.code.repository.swebench.fail_to_pass == ["tests.test_bug"]

    output = prediction_jsonl(
        dataset,
        {
            case.case_id: [
                {
                    "outcome": "completed",
                    "metadata": {
                        "code_verification": {
                            "mode": "swebench",
                            "status": "not_evaluated",
                            "patch": "diff --git a/a.py b/a.py",
                        }
                    },
                }
            ]
        },
        model_name_or_path="test-model",
    )
    assert '"instance_id": "django__django-12345"' in output
    assert '"model_patch": "diff --git a/a.py b/a.py"' in output
    assert '"model_name_or_path": "test-model"' in output


def test_partial_swebench_prediction_export_is_explicit_opt_in():
    dataset = swebench_dataset_from_rows(
        [
            {
                "instance_id": instance_id,
                "repo": "django/django",
                "base_commit": commit * 40,
                "problem_statement": "Fix the regression",
                "test_patch": "diff --git a/test.py b/test.py",
                "FAIL_TO_PASS": "[]",
                "PASS_TO_PASS": "[]",
                "version": "4.2",
            }
            for instance_id, commit in (
                ("django__django-12345", "a"),
                ("django__django-12346", "b"),
            )
        ],
        name="Partial sample",
    )
    first = dataset.cases[0]
    envelopes = {
        first.case_id: [
            {
                "outcome": "completed",
                "metadata": {
                    "code_verification": {
                        "mode": "swebench",
                        "status": "not_evaluated",
                        "patch": "diff --git a/a.py b/a.py",
                    }
                },
            }
        ]
    }

    with pytest.raises(ValueError, match="predictions are incomplete"):
        prediction_jsonl(dataset, envelopes, model_name_or_path="test-model")

    output = prediction_jsonl(
        dataset,
        envelopes,
        model_name_or_path="test-model",
        allow_partial=True,
    )
    assert "django__django-12345" in output
    assert "django__django-12346" not in output


@pytest.mark.asyncio
async def test_coding_runner_exposes_execute_and_forces_kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repository = EvaluationRepository(tmp_path / "evaluation.sqlite3")
    draft = repository.create_dataset(
        EvalDataset(
            name="Coding runner",
            default_profile="coding_agent@1",
            cases=[EvalCase(name="Fix", input=EvalInput(message="fix"), code=_inline_code())],
        )
    )
    bundle = repository.publish_dataset(draft.dataset_id, draft.revision)
    experiment = repository.create_experiment(
        EvalExperiment(
            name="Code run",
            dataset_id=draft.dataset_id,
            dataset_version=1,
            dataset_version_id=str(bundle.version_id),
            dataset_content_hash=str(bundle.checksum),
            candidate=ExperimentCandidate(name="stub"),
            profile_id="coding_agent@1",
        )
    )
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("PUDDINGCLAW_HOME", str(tmp_path / "home"))
    # Register both process-wide evaluation flags with monkeypatch before the
    # runner initializes them, so this direct unit-level runtime setup cannot
    # leak into later DeepAgents path tests.
    monkeypatch.setenv("PUDDINGCLAW_EVALUATION_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    runner = EvaluationRunner(repository, LangSmithSettings(enabled=False), tmp_path)
    runner._initialize_isolated_runtime(runtime_root)

    from graph.deepagents_manager import deepagents_agent_manager
    from projects.registry import project_registry

    async def fake_astream(**kwargs):
        assert "execute" in kwargs["evaluation_builtin_tool_allowlist"]
        assert "patch_file" in kwargs["evaluation_builtin_tool_allowlist"]
        assert "edit_file" not in kwargs["evaluation_builtin_tool_allowlist"]
        assert kwargs["evaluation_required_toolset"] == kwargs["evaluation_builtin_tool_allowlist"]
        assert kwargs["disable_mcp"] is True
        assert project_registry.get_execution_mode(kwargs["project_id"]) == "kernel"
        assert kwargs["interaction_mode"] == "auto"
        yield {"event": "done", "data": '{"content":"implemented"}'}

    monkeypatch.setattr(deepagents_agent_manager, "astream", fake_astream)
    monkeypatch.setattr(
        "evaluation.runner.verify_code_case",
        lambda workspace, attempt_root, spec: {
            "status": "passed",
            "passed": True,
            "reason": "hidden tests passed",
            "patch": "diff --git a/solution.py b/solution.py",
            "patch_sha256": "abc",
            "changed_paths": ["solution.py"],
            "commands": [],
        },
    )
    case = repository.get_dataset(draft.dataset_id, 1).cases[0]
    result = await runner._run_case(experiment, case, 0, runtime_root)

    assert result["summary"]["verdict"] == "pass"
    code_result = next(item for item in result["results"] if item["evaluator_id"] == "code_verification.v1")
    assert code_result["outcome"] == "pass"
