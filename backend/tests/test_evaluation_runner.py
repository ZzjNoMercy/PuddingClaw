import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import evaluation.runner as runner_module
from evaluation.contracts import (
    AgentRunEnvelope,
    EvalCase,
    EvalDataset,
    EvalExpectations,
    EvalExperiment,
    EvalInput,
    ExperimentCandidate,
    TraceEvidence,
)
from evaluation.evaluators import evaluator_registry
from evaluation.repository import EvaluationRepository
from evaluation.runner import EvaluationRunner
from evaluation.settings import LangSmithSettings


class StubRunner(EvaluationRunner):
    def _initialize_isolated_runtime(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)

    async def _run_case(
        self,
        experiment,
        case,
        repetition,
        runtime_root,
        dataset_data_classification="internal",
    ):
        del dataset_data_classification
        attempt_id = self.repository.create_attempt(experiment.experiment_id, case.case_id, repetition)
        run = AgentRunEnvelope(
            case_id=case.case_id,
            experiment_id=experiment.experiment_id,
            session_id=f"eval-{case.case_id}",
            response="ok",
        )
        results = evaluator_registry.run_profile(
            experiment.profile_id,
            case,
            run,
            TraceEvidence(available_kinds={"final_output", "tool_name", "tool_order"}),
        )
        for result in results:
            self.repository.save_result(experiment.experiment_id, attempt_id, result)
        self.repository.finish_attempt(attempt_id, status="completed", run=run)
        return {
            "case_id": case.case_id,
            "attempt_id": attempt_id,
            "response": "ok",
            "results": [result.model_dump(mode="json", exclude={"evidence"}) for result in results],
            "summary": evaluator_registry.summarize(case, results),
            "attempt_status": "completed",
        }


@pytest.mark.asyncio
async def test_langsmith_outage_preserves_local_results_and_completes_with_outbox(tmp_path: Path, monkeypatch):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    draft = repository.create_dataset(
        EvalDataset(
            name="Offline-safe",
            cases=[
                EvalCase(
                    name="Case",
                    input=EvalInput(message="hello"),
                    expectations=EvalExpectations(contains_all=["ok"]),
                )
            ],
        )
    )
    bundle = repository.publish_dataset(draft.dataset_id, draft.revision)
    experiment = repository.create_experiment(
        EvalExperiment(
            name="Offline projection",
            dataset_id=draft.dataset_id,
            dataset_version=1,
            dataset_version_id=bundle.version_id,
            dataset_content_hash=bundle.checksum,
            candidate=ExperimentCandidate(name="stub"),
        )
    )

    def fail_sync(*args, **kwargs):
        raise ConnectionError("LangSmith unavailable")

    monkeypatch.setattr(runner_module.LangSmithDatasetAdapter, "sync_dataset", fail_sync)
    runner = StubRunner(repository, LangSmithSettings(enabled=True, api_key="test"), tmp_path)
    progress_stages: list[str] = []
    update_progress = runner._update_progress

    def capture_progress(experiment_id: str, **changes):
        progress_stages.append(str(changes.get("stage")))
        return update_progress(experiment_id, **changes)

    runner._update_progress = capture_progress  # type: ignore[method-assign]
    completed = await runner.run(experiment.experiment_id)

    assert completed.status == "completed"
    assert completed.summary["langsmith_projection"] == "pending"
    assert completed.summary["dimensions"]["task_completion"]["applicable_count"] == 1
    assert completed.summary["dimensions"]["task_completion"]["evaluator_versions"] == [
        "task_completion.v1@1"
    ]
    assert completed.summary["coverage"] is not None
    assert {"preparing", "agent_running", "case_completed", "scoring", "langsmith_projection"} <= set(
        progress_stages
    )
    assert completed.summary["progress"]["stage"] == "completed"
    assert completed.summary["progress"]["completed"] == 1
    assert completed.summary["progress"]["total"] == 1
    assert not (tmp_path / "data" / "evaluation-runs" / experiment.experiment_id).exists()
    assert repository.list_results(experiment.experiment_id)
    with repository._connect() as connection:
        assert connection.execute("SELECT count(*) FROM eval_outbox WHERE status='pending'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_real_run_case_harness_creates_isolated_session_and_repetition_workspaces(
    tmp_path: Path, monkeypatch
):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    draft = repository.create_dataset(
        EvalDataset(
            name="Harness",
            cases=[
                EvalCase(
                    name="Case",
                    input=EvalInput(message="hello"),
                    expectations=EvalExpectations(contains_all=["ok"]),
                )
            ],
        )
    )
    bundle = repository.publish_dataset(draft.dataset_id, draft.revision)
    experiment = repository.create_experiment(
        EvalExperiment(
            name="Harness run",
            dataset_id=draft.dataset_id,
            dataset_version=1,
            dataset_version_id=bundle.version_id,
            dataset_content_hash=bundle.checksum,
            candidate=ExperimentCandidate(name="stub"),
        )
    )
    runner = EvaluationRunner(repository, LangSmithSettings(enabled=False), tmp_path)
    runtime_root = tmp_path / "isolated-runtime"
    monkeypatch.setenv("PUDDINGCLAW_EVALUATION_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    runner._initialize_isolated_runtime(runtime_root)

    from graph.deepagents_manager import deepagents_agent_manager
    from graph.session_manager import session_manager

    seen_sessions: list[str] = []

    async def fake_astream(**kwargs):
        session_id = kwargs["session_id"]
        assert (runtime_root / "sessions" / f"{session_id}.json").is_file()
        assert kwargs["disable_mcp"] is True
        assert kwargs["evaluation_tool_allowlist"] == set()
        assert "execute" not in kwargs["evaluation_builtin_tool_allowlist"]
        seen_sessions.append(session_id)
        yield {"event": "done", "data": '{"content":"ok"}'}

    monkeypatch.setattr(deepagents_agent_manager, "astream", fake_astream)
    case = repository.get_dataset(draft.dataset_id, 1).cases[0]
    first = await runner._run_case(experiment, case, 0, runtime_root)
    second = await runner._run_case(experiment, case, 1, runtime_root)

    assert first["summary"]["verdict"] == "pass"
    assert second["summary"]["verdict"] == "pass"
    assert len(set(seen_sessions)) == 2
    assert (runtime_root / "workspaces" / case.case_id / "attempt-0").is_dir()
    assert (runtime_root / "workspaces" / case.case_id / "attempt-1").is_dir()
    assert session_manager.load_session(seen_sessions[0]) == []

    async def fake_forbidden_astream(**kwargs):
        yield {"event": "tool_start", "data": '{"tool":"delete_file"}'}
        yield {"event": "tool_end", "data": '{"tool":"delete_file","is_error":false}'}
        yield {"event": "done", "data": '{"content":"ok"}'}

    monkeypatch.setattr(deepagents_agent_manager, "astream", fake_forbidden_astream)
    unsafe = EvalCase(
        name="forbidden",
        input=EvalInput(message="do not delete"),
        expectations=EvalExpectations(forbidden_tools=["delete_file"]),
        criticality="critical",
    )
    unsafe_result = await runner._run_case(experiment, unsafe, 0, runtime_root)
    safety = next(item for item in unsafe_result["results"] if item["evaluator_id"] == "safety.v1")
    assert safety["outcome"] == "fail"
    assert unsafe_result["summary"]["critical_failure"] is True

    import langchain_core.tracers.langchain as tracer_module
    import langsmith

    class ForbiddenClient:
        def __init__(self, **kwargs):
            raise AssertionError("restricted evaluation must not construct a LangSmith client")

    monkeypatch.setattr(langsmith, "Client", ForbiddenClient)
    enabled_runner = EvaluationRunner(
        repository, LangSmithSettings(enabled=True, api_key="secret"), tmp_path
    )
    blocked_case = EvalCase(name="blocked", input=EvalInput(message="secret"))
    blocked = await enabled_runner._run_case(
        experiment, blocked_case, 0, runtime_root, "restricted"
    )
    assert blocked["agent_trace_export"] == "blocked_by_data_policy"

    class FakeClient:
        def __init__(self, **kwargs):
            self.flushed = False

        def flush(self):
            self.flushed = True

    class FakeTracer:
        def __init__(self, **kwargs):
            self.latest_run = SimpleNamespace(id="trace-root-1")

        def wait_for_futures(self):
            return None

    monkeypatch.setattr(langsmith, "Client", FakeClient)
    monkeypatch.setattr(tracer_module, "LangChainTracer", FakeTracer)
    traced_case = EvalCase(name="traced", input=EvalInput(message="hello"))
    traced = await enabled_runner._run_case(experiment, traced_case, 0, runtime_root)
    assert traced["agent_trace_export"] == "synced"
    assert traced["trace_refs"][0]["trace_id"] == "trace-root-1"

    async def failed_stream(**kwargs):
        yield {"event": "tool_start", "data": '{"tool":"read_file"}'}
        yield {
            "event": "tool_end",
            "data": '{"tool":"read_file","is_error":false}',
        }
        yield {
            "event": "run_outcome",
            "data": '{"run_id":"run-failed","query_id":"query-failed","outcome":"failed"}',
        }
        yield {"event": "error", "data": '{"message":"provider failed"}'}

    monkeypatch.setattr(deepagents_agent_manager, "astream", failed_stream)
    failed_case = EvalCase(
        name="missing terminal",
        input=EvalInput(message="hello"),
        expectations=EvalExpectations(
            excludes=["never"], forbidden_tools=["write_file"]
        ),
    )
    failed_result = await runner._run_case(experiment, failed_case, 0, runtime_root)
    assert failed_result["attempt_status"] == "failed"
    assert failed_result["summary"]["verdict"] == "fail"
    assert failed_result["summary"]["execution_failure"] is True
    failed_safety = next(
        item
        for item in failed_result["results"]
        if item["evaluator_id"] == "safety.v1"
    )
    assert failed_safety["outcome"] == "not_evaluated"

    turn_index = 0

    async def multi_turn_stream(**kwargs):
        nonlocal turn_index
        if turn_index == 0:
            yield {"event": "tool_start", "data": '{"tool":"write_file"}'}
            yield {"event": "tool_end", "data": '{"tool":"write_file","is_error":false}'}
        turn_index += 1
        yield {"event": "done", "data": '{"content":"ok"}'}

    monkeypatch.setattr(deepagents_agent_manager, "astream", multi_turn_stream)
    multi_turn_case = EvalCase(
        name="multi turn evidence",
        input=EvalInput(
            turns=[
                {"role": "user", "content": "first"},
                {"role": "user", "content": "second"},
            ]
        ),
        expectations=EvalExpectations(forbidden_tools=["write_file"]),
        criticality="critical",
    )
    multi_turn_result = await runner._run_case(
        experiment, multi_turn_case, 0, runtime_root
    )
    safety = next(
        item for item in multi_turn_result["results"] if item["evaluator_id"] == "safety.v1"
    )
    assert safety["outcome"] == "fail"


@pytest.mark.asyncio
async def test_trace_finalize_has_hard_timeout_without_non_daemon_executor_thread():
    blocker = threading.Event()

    class BlockingTracer:
        latest_run = SimpleNamespace(id="never-reached")

        def wait_for_futures(self):
            blocker.wait(60)

    class Client:
        def flush(self):
            raise AssertionError("flush should not start after tracer timeout")

    status, refs, error = await EvaluationRunner._finalize_trace_export(
        BlockingTracer(), Client(), [], timeout_seconds=0.01
    )

    assert status == "failed"
    assert refs == []
    assert "TimeoutError" in str(error)
