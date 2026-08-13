import json
from pathlib import Path

import pytest

import evaluation.official_swebench as official_module
import evaluation.runner as runner_module
from evaluation.contracts import (
    AgentRunEnvelope,
    EvalExperiment,
    ExperimentCandidate,
    ExperimentStatus,
    TraceEvidence,
)
from evaluation.evaluators import evaluator_registry
from evaluation.official_swebench import ProcessResult, _read_report, run_official_swebench_harness
from evaluation.repository import EvaluationRepository
from evaluation.runner import EvaluationRunner
from evaluation.settings import LangSmithSettings
from evaluation.swebench_adapter import swebench_dataset_from_rows


def _dataset():
    return swebench_dataset_from_rows(
        [
            {
                "instance_id": "pytest-dev__pytest-1234",
                "repo": "pytest-dev/pytest",
                "base_commit": "b" * 40,
                "problem_statement": "Fix collection",
                "version": "8.0",
                "test_patch": "diff --git a/testing/test_x.py b/testing/test_x.py",
                "FAIL_TO_PASS": "[]",
                "PASS_TO_PASS": "[]",
            }
        ],
        name="SWE managed verifier",
    )


def _experiment(dataset, *, status=ExperimentStatus.RUNNING):
    return EvalExperiment(
        name="SWE experiment",
        dataset_id=dataset.dataset_id,
        dataset_version=1,
        dataset_version_id="dsv_test",
        dataset_content_hash="content-hash",
        candidate=ExperimentCandidate(name="model"),
        profile_id="coding_agent@1",
        status=status,
    )


def _envelopes(experiment, dataset, attempt_id="attempt_test"):
    case = dataset.cases[0]
    return {
        case.case_id: [
            {
                **AgentRunEnvelope(
                    case_id=case.case_id,
                    experiment_id=experiment.experiment_id,
                    candidate_id=experiment.candidate.candidate_id,
                    session_id="session",
                    response="patched",
                    metadata={
                        "code_verification": {
                            "mode": "swebench",
                            "status": "not_evaluated",
                            "patch": "diff --git a/a.py b/a.py",
                            "patch_sha256": "patch-hash",
                            "changed_paths": ["a.py"],
                        }
                    },
                ).model_dump(mode="json"),
                "_attempt_id": attempt_id,
                "_attempt_status": "completed",
            }
        ]
    }


@pytest.mark.asyncio
async def test_managed_official_harness_runs_pinned_package_and_parses_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dataset = _dataset()
    experiment = _experiment(dataset)
    envelopes = _envelopes(experiment, dataset)

    async def docker_probe(environment, root):
        return True, "29.6.2"

    async def fake_process(
        argv, *, cwd, environment, timeout_seconds, log_path, isolate_process_group=False, pid_path=None
    ):
        del timeout_seconds
        assert isolate_process_group is True
        assert pid_path == cwd / "harness.pid"
        assert "LANGSMITH_API_KEY" not in environment
        assert argv[1:3] == ["-m", "puddingclaw_swebench_entry"]
        assert environment["PYTHONPATH"] == str(cwd)
        guard = (cwd / "sitecustomize.py").read_text(encoding="utf-8")
        assert 'kwargs["network_disabled"] = True' in guard
        assert 'kwargs["cap_drop"] = ["ALL"]' in guard
        assert 'kwargs["pids_limit"]' in guard
        assert 'kwargs["storage_opt"]' in guard
        run_id = argv[argv.index("--run_id") + 1]
        report = {
            "completed_ids": ["pytest-dev__pytest-1234"],
            "resolved_ids": ["pytest-dev__pytest-1234"],
            "unresolved_ids": [],
            "error_ids": [],
        }
        (cwd / f"model.{run_id}.json").write_text(json.dumps(report), encoding="utf-8")
        log_path.write_text("official harness complete", encoding="utf-8")
        return ProcessResult(exit_code=0, output_tail="official harness complete")

    monkeypatch.setattr(official_module, "_docker_probe", docker_probe)
    monkeypatch.setattr(official_module, "_run_process", fake_process)
    monkeypatch.setattr(official_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("LANGSMITH_API_KEY", "must-not-enter-official-harness")

    result = await run_official_swebench_harness(experiment, dataset, envelopes, tmp_path)

    assert result["status"] == "completed"
    assert result["results"]["pytest-dev__pytest-1234"]["status"] == "passed"
    assert result["receipt"]["provenance"] == "platform_managed_official_harness"
    assert result["receipt"]["report_sha256"]
    assert "diff --git" not in json.dumps(result["receipt"])


@pytest.mark.asyncio
async def test_managed_official_harness_fails_closed_when_docker_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dataset = _dataset()
    experiment = _experiment(dataset)

    async def docker_probe(environment, root):
        return False, "Docker daemon is unavailable"

    monkeypatch.setattr(official_module, "_docker_probe", docker_probe)
    result = await run_official_swebench_harness(
        experiment,
        dataset,
        _envelopes(experiment, dataset),
        tmp_path,
    )

    assert result["status"] == "error"
    assert result["results"]["pytest-dev__pytest-1234"]["status"] == "error"


def test_official_report_reader_rejects_symlink(tmp_path: Path):
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "report.json"
    link.symlink_to(target)

    with pytest.raises(OSError):
        _read_report(link)


@pytest.mark.asyncio
async def test_managed_official_harness_rejects_inconsistent_aggregate_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dataset = _dataset()
    experiment = _experiment(dataset)

    async def docker_probe(environment, root):
        return True, "29.6.2"

    async def fake_process(
        argv, *, cwd, environment, timeout_seconds, log_path, isolate_process_group=False, pid_path=None
    ):
        del environment, timeout_seconds, log_path, pid_path
        assert isolate_process_group is True
        run_id = argv[argv.index("--run_id") + 1]
        report = {
            "completed_ids": ["pytest-dev__pytest-1234"],
            "resolved_ids": ["pytest-dev__pytest-1234"],
            "unresolved_ids": ["pytest-dev__pytest-1234"],
            "error_ids": [],
        }
        (cwd / f"model.{run_id}.json").write_text(json.dumps(report), encoding="utf-8")
        return ProcessResult(exit_code=0, output_tail="inconsistent report")

    monkeypatch.setattr(official_module, "_docker_probe", docker_probe)
    monkeypatch.setattr(official_module, "_run_process", fake_process)

    result = await run_official_swebench_harness(
        experiment,
        dataset,
        _envelopes(experiment, dataset),
        tmp_path,
    )

    assert result["status"] == "error"
    assert result["results"]["pytest-dev__pytest-1234"]["status"] == "error"
    assert "inconsistent" in result["results"]["pytest-dev__pytest-1234"]["reason"].lower()


@pytest.mark.asyncio
async def test_runner_applies_platform_managed_official_result_to_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    draft = repository.create_dataset(_dataset())
    bundle = repository.publish_dataset(draft.dataset_id, draft.revision)
    experiment = repository.create_experiment(
        _experiment(bundle.dataset).model_copy(
            update={
                "dataset_id": draft.dataset_id,
                "dataset_version_id": bundle.version_id,
                "dataset_content_hash": bundle.checksum,
            }
        )
    )
    case = bundle.dataset.cases[0]
    attempt_id = repository.create_attempt(experiment.experiment_id, case.case_id, 0)
    envelope = _envelopes(experiment, bundle.dataset, attempt_id)[case.case_id][0]
    run = AgentRunEnvelope.model_validate({key: value for key, value in envelope.items() if not key.startswith("_")})
    repository.finish_attempt(attempt_id, status="completed", run=run)
    code_evaluator = evaluator_registry.get_registered("code_verification.v1")
    assert code_evaluator is not None
    initial = code_evaluator[1](case, run, TraceEvidence(available_kinds={"code_patch", "code_verification"}))
    repository.save_result(experiment.experiment_id, attempt_id, initial)
    all_attempts = [
        {
            "case_id": case.case_id,
            "attempt_id": attempt_id,
            "attempt_status": "completed",
            "results": [initial.model_dump(mode="json")],
            "summary": evaluator_registry.summarize(case, [initial]),
        }
    ]

    async def fake_official(experiment, dataset, run_envelopes, runtime_root):
        del experiment, dataset, run_envelopes, runtime_root
        return {
            "status": "completed",
            "reason": "Official SWE-bench Harness completed",
            "results": {
                "pytest-dev__pytest-1234": {
                    "status": "passed",
                    "resolved": True,
                    "reason": "Official SWE-bench Harness resolved the instance",
                }
            },
            "receipt": {
                "provenance": "platform_managed_official_harness",
                "package": "swebench==4.1.0",
                "run_id": "run",
                "report_sha256": "report",
                "predictions_sha256": "predictions",
                "patch_sha256": {"pytest-dev__pytest-1234": "patch"},
                "source_snapshot_sha256": "snapshot",
                "aggregate": {"resolved_ids": ["pytest-dev__pytest-1234"]},
            },
        }

    monkeypatch.setattr(runner_module, "run_official_swebench_harness", fake_official)
    runner = EvaluationRunner(repository, LangSmithSettings(enabled=False), tmp_path)
    summary = await runner._run_and_apply_official_swebench(
        experiment,
        bundle.dataset,
        tmp_path,
        all_attempts,
    )

    assert summary["status"] == "completed"
    assert summary["resolved"] == 1
    saved_run = repository.load_run_envelopes(experiment.experiment_id)[case.case_id][0]
    assert saved_run["metadata"]["code_verification"]["status"] == "passed"
    saved_code_result = next(
        row["result"]
        for row in repository.list_results(experiment.experiment_id)
        if row["result"] and row["result"]["evaluator_id"] == "code_verification.v1"
    )
    assert saved_code_result["outcome"] == "pass"
    assert all_attempts[0]["summary"]["verdict"] == "pass"
