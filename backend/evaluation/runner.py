"""Isolated evaluation execution and LangSmith Experiment projection."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from .candidate import (
    ISOLATED_CAPABILITY_PROFILE,
    ISOLATED_WORKSPACE_CORE_TOOLS,
    verify_candidate_snapshot,
)
from .contracts import (
    AgentRunEnvelope,
    EvalCase,
    EvalError,
    EvalExperiment,
    ExperimentStatus,
    ToolCallEvidence,
    TraceEvidence,
    TraceReference,
    new_id,
    utc_now,
)
from .evaluators import evaluator_registry
from .evidence import EvaluationEvidenceCallback
from .langsmith_backend import (
    LangSmithDatasetAdapter,
    _redact,
    langsmith_client_kwargs,
)
from .repository import EvaluationRepository
from .settings import LangSmithSettings


class EvaluationRunner:
    def __init__(self, repository: EvaluationRepository, settings: LangSmithSettings, backend_dir: Path) -> None:
        self.repository = repository
        self.settings = settings
        self.backend_dir = backend_dir.resolve()

    def _runtime_root(self, experiment_id: str) -> Path:
        return self.backend_dir / "data" / "evaluation-runs" / experiment_id

    def _initialize_isolated_runtime(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(root, 0o700)
        os.environ["PUDDINGCLAW_EVALUATION_RUNTIME_ROOT"] = str(root)
        # Provider tracing remains invocation-scoped. Never mutate the normal
        # backend process or rely on its global LangSmith environment flags.
        os.environ["LANGSMITH_TRACING"] = "false"
        from graph.attachment_store import attachment_store
        from graph.deepagents_manager import deepagents_agent_manager
        from graph.session_manager import session_manager
        from projects.registry import project_registry

        session_manager.initialize(root)
        project_registry.initialize(root)
        attachment_store.initialize(root)
        deepagents_agent_manager.initialize(self.backend_dir)

    @staticmethod
    def _event_payload(event: dict[str, str]) -> dict[str, Any]:
        try:
            payload = json.loads(event.get("data") or "{}")
            return payload if isinstance(payload, dict) else {}
        except (TypeError, ValueError):
            return {}

    @staticmethod
    async def _daemon_call(function: Any) -> Any:
        """Run blocking SDK cleanup without letting its thread block worker exit."""

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()

        def finish(value: Any = None, error: BaseException | None = None) -> None:
            if future.done():
                return
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(value)

        def invoke() -> None:
            try:
                value = function()
            except BaseException as exc:
                loop.call_soon_threadsafe(finish, None, exc)
            else:
                loop.call_soon_threadsafe(finish, value, None)

        threading.Thread(target=invoke, name="evaluation-provider-finalize", daemon=True).start()
        return await future

    @staticmethod
    async def _finalize_trace_export(
        tracer: Any | None,
        client: Any | None,
        errors: list[str],
        *,
        timeout_seconds: float,
    ) -> tuple[str, list[TraceReference], str | None]:
        if tracer is None or client is None:
            return "not_started", [], None
        try:
            async with asyncio.timeout(timeout_seconds):
                await EvaluationRunner._daemon_call(tracer.wait_for_futures)
                await EvaluationRunner._daemon_call(client.flush)
            if errors:
                raise RuntimeError(errors[-1])
            latest = getattr(tracer, "latest_run", None)
            trace_id = str(getattr(latest, "id", "") or "")
            if not trace_id:
                raise RuntimeError("LangSmith tracer did not expose a root run id")
            return "synced", [TraceReference(provider="langsmith", trace_id=trace_id)], None
        except Exception as exc:
            message = str(_redact(f"{type(exc).__name__}: {str(exc)[:500]}"))
            return "failed", [], message

    def _prepare_workspace(self, root: Path, case: EvalCase, repetition: int) -> tuple[Path, str]:
        from projects.registry import project_registry

        workspace = root / "workspaces" / case.case_id / f"attempt-{repetition}"
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True)
        for fixture in case.setup.fixtures:
            target = workspace / (fixture.target or fixture.fixture_id)
            resolved = target.resolve()
            if workspace.resolve() not in resolved.parents and resolved != workspace.resolve():
                raise ValueError(f"Fixture target escapes workspace: {fixture.fixture_id}")
            if fixture.kind == "inline":
                target.parent.mkdir(parents=True, exist_ok=True)
                payload = fixture.payload or {}
                target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                raise ValueError(
                    f"Fixture {fixture.fixture_id} is not materialized in the safe evaluation fixture store"
                )
        record = project_registry.register(str(workspace), name=f"Eval {case.name}")
        return workspace, record.project_id

    async def _run_case(
        self,
        experiment: EvalExperiment,
        case: EvalCase,
        repetition: int,
        runtime_root: Path,
        dataset_data_classification: str = "internal",
    ) -> dict[str, Any]:
        from graph.deepagents_manager import deepagents_agent_manager
        from graph.session_manager import session_manager

        attempt_id = self.repository.create_attempt(experiment.experiment_id, case.case_id, repetition)
        self.repository.update_attempt_status(attempt_id, "running")
        session_id = f"eval-{experiment.experiment_id}-{case.case_id}-{repetition}"
        eval_run_id = new_id("evalrun")
        started = utc_now()
        callback = EvaluationEvidenceCallback()
        callbacks: list[Any] = [callback]
        response = ""
        run_id = None
        query_id = None
        event_calls: list[ToolCallEvidence] = []
        trace_client: Any | None = None
        langsmith_tracer: Any | None = None
        trace_errors: list[str] = []
        trace_export_status = (
            "disabled"
            if not self.settings.enabled or not self.settings.api_key
            else "blocked_by_data_policy"
            if dataset_data_classification in {"sensitive", "restricted"}
            or case.data_classification in {"sensitive", "restricted"}
            else "not_started"
        )
        try:
            _, project_id = self._prepare_workspace(runtime_root, case, repetition)
            session_manager.create_session(
                session_id,
                metadata={
                    "runtime_mode": "agent",
                    "evaluation": True,
                    "experiment_id": experiment.experiment_id,
                    "case_id": case.case_id,
                    "repetition": repetition,
                },
            )
            if (
                self.settings.enabled
                and self.settings.api_key
                and dataset_data_classification not in {"sensitive", "restricted"}
                and case.data_classification not in {"sensitive", "restricted"}
            ):
                try:
                    from langchain_core.tracers.langchain import LangChainTracer
                    from langsmith import Client

                    trace_client = Client(
                        **langsmith_client_kwargs(self.settings),
                        anonymizer=lambda value: _redact(
                            value, profile=self.settings.redaction_profile
                        ),
                        hide_inputs=lambda value: _redact(
                            value, profile=self.settings.redaction_profile
                        ),
                        hide_outputs=lambda value: _redact(
                            value, profile=self.settings.redaction_profile
                        ),
                        hide_metadata=lambda value: _redact(
                            value, profile=self.settings.redaction_profile
                        ),
                        tracing_error_callback=lambda error: trace_errors.append(
                            str(_redact(f"{type(error).__name__}: {str(error)[:500]}"))
                        ),
                    )
                    langsmith_tracer = LangChainTracer(
                            client=trace_client,
                            project_name=f"{self.settings.project}-agent-runs",
                            tags=["puddingclaw-evaluation", case.case_id],
                            metadata={
                                "experiment_id": experiment.experiment_id,
                                "case_id": case.case_id,
                                "attempt_id": attempt_id,
                                "eval_run_id": eval_run_id,
                                "session_id": session_id,
                                "candidate_id": experiment.candidate.candidate_id,
                                "candidate_fingerprint": str(experiment.candidate.fingerprint or ""),
                                "dataset_version_id": experiment.dataset_version_id,
                            },
                        )
                    callbacks.append(langsmith_tracer)
                except Exception as exc:
                    trace_export_status = "failed"
                    trace_errors.append(
                        str(_redact(f"{type(exc).__name__}: {str(exc)[:500]}"))
                    )
            actions: list[tuple[str, str]] = []
            if case.input.message:
                actions.append(("user", case.input.message))
            else:
                actions.extend((turn.role, turn.content) for turn in case.input.turns)
            deadline = asyncio.get_running_loop().time() + experiment.execution.timeout_seconds
            for role, message in actions:
                if role == "assistant":
                    session_manager.save_message(session_id, "assistant", message)
                    continue
                if role != "user":
                    raise ValueError(f"Scripted {role} turns require the Phase 2 HITL adapter")
                if case.setup.clock is not None:
                    message = (
                        "[Evaluation deterministic clock] "
                        f"Current time is {case.setup.clock.isoformat()} ({case.setup.timezone}). "
                        "Use this value instead of wall-clock time.\n\n"
                        + message
                    )
                response = ""
                done_seen = False
                turn_outcome: str | None = None
                stream_error: str | None = None
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("Case timeout exhausted")
                async with asyncio.timeout(remaining):
                    async for event in deepagents_agent_manager.astream(
                        message=message or "",
                        session_id=session_id,
                        project_id=project_id,
                        analytics_model_id=experiment.candidate.analytics_model_id,
                        llm_model_id=experiment.candidate.llm_model_id,
                        thinking_level=experiment.candidate.thinking_level,
                        user_id="evaluation_worker",
                        callbacks_override=callbacks,
                        evaluation_tool_allowlist=set(experiment.candidate.config.get("tool_allowlist") or []),
                        disable_mcp=True,
                        evaluation_builtin_tool_allowlist=set(ISOLATED_WORKSPACE_CORE_TOOLS),
                    ):
                        payload = self._event_payload(event)
                        if event.get("event") == "tool_start":
                            event_calls.append(
                                ToolCallEvidence(
                                    name=str(payload.get("tool") or "unknown_tool"),
                                    arguments={},
                                    sequence=len(event_calls),
                                )
                            )
                        elif event.get("event") == "tool_end" and event_calls:
                            event_calls[-1] = event_calls[-1].model_copy(
                                update={"succeeded": not bool(payload.get("is_error"))}
                            )
                        elif event.get("event") == "run_outcome":
                            run_id = str(payload.get("run_id") or run_id or "") or None
                            query_id = str(payload.get("query_id") or query_id or "") or None
                            turn_outcome = str(payload.get("outcome") or "") or None
                        elif event.get("event") == "error":
                            stream_error = str(
                                payload.get("message") or payload.get("error") or "Agent stream error"
                            )
                        elif event.get("event") == "done":
                            response = str(payload.get("content") or "")
                            done_seen = True
                if stream_error:
                    raise RuntimeError(stream_error)
                if turn_outcome and turn_outcome != "completed":
                    raise RuntimeError(f"Agent run ended with outcome={turn_outcome}")
                if not done_seen:
                    raise RuntimeError("Agent stream ended without a terminal done event")
            callback_evidence = callback.evidence(run_id=run_id)
            if langsmith_tracer is not None:
                trace_export_status, trace_refs, trace_export_error = await self._finalize_trace_export(
                    langsmith_tracer,
                    trace_client,
                    trace_errors,
                    timeout_seconds=self.settings.trace_finalize_timeout_seconds,
                )
            else:
                trace_refs = []
                trace_export_error = trace_errors[-1] if trace_errors else None
            callback_names = [call.name for call in callback_evidence.tool_calls]
            event_names = [call.name for call in event_calls]
            tool_sequence_complete = callback_names == event_names
            tool_calls = list(callback_evidence.tool_calls)
            remaining_event_calls = list(event_calls)
            for callback_call in callback_evidence.tool_calls:
                match = next(
                    (
                        index
                        for index, item in enumerate(remaining_event_calls)
                        if item.name == callback_call.name
                    ),
                    None,
                )
                if match is not None:
                    remaining_event_calls.pop(match)
            tool_calls.extend(remaining_event_calls)
            tool_calls = [
                call.model_copy(update={"sequence": index})
                for index, call in enumerate(tool_calls)
            ]
            using_sse_tool_fallback = bool(remaining_event_calls)
            run = AgentRunEnvelope(
                eval_run_id=eval_run_id,
                case_id=case.case_id,
                experiment_id=experiment.experiment_id,
                candidate_id=experiment.candidate.candidate_id,
                repetition=repetition,
                input=case.input,
                session_id=session_id,
                run_id=run_id,
                response=response,
                tool_calls=tool_calls,
                started_at=started,
                finished_at=utc_now(),
                timing={"latency_ms": (utc_now() - started).total_seconds() * 1000},
                trace_refs=trace_refs,
                metadata={"query_id": query_id} if query_id else {},
            )
            evidence = callback_evidence.model_copy(
                update={
                    "available_kinds": set(callback_evidence.available_kinds)
                    | {"final_output"}
                    | (
                        {"tool_name", "tool_order", "tool_status"}
                        if using_sse_tool_fallback
                        else set()
                    ),
                    "tool_calls": tool_calls,
                    "trajectory": [call.name for call in tool_calls],
                    "metadata": {
                        **callback_evidence.metadata,
                        "capture_complete": False
                        if using_sse_tool_fallback
                        else callback_evidence.metadata.get("capture_complete", True),
                        "tool_sequence_complete": tool_sequence_complete,
                        "tool_evidence_source": "sse_fallback"
                        if using_sse_tool_fallback
                        else "callback",
                        "offered_tools": list(ISOLATED_WORKSPACE_CORE_TOOLS),
                        "capability_profile": ISOLATED_CAPABILITY_PROFILE,
                    },
                }
            )
            results = evaluator_registry.run_profile(experiment.profile_id, case, run, evidence)
            for result in results:
                self.repository.save_result(experiment.experiment_id, attempt_id, result)
            self.repository.finish_attempt(attempt_id, status="completed", run=run)
            return {
                "case_id": case.case_id,
                "attempt_id": attempt_id,
                "response": _redact(response)[:8_000],
                "results": _redact([result.model_dump(mode="json") for result in results]),
                "summary": evaluator_registry.summarize(case, results),
                "attempt_status": "completed",
                "agent_trace_export": trace_export_status,
                "agent_trace_error": trace_export_error,
                "trace_refs": [item.model_dump(mode="json") for item in trace_refs],
            }
        except Exception as exc:
            if langsmith_tracer is not None:
                trace_export_status, trace_refs, trace_export_error = await self._finalize_trace_export(
                    langsmith_tracer,
                    trace_client,
                    trace_errors,
                    timeout_seconds=self.settings.trace_finalize_timeout_seconds,
                )
            else:
                trace_refs = []
                trace_export_error = trace_errors[-1] if trace_errors else None
            error = EvalError(
                code="case_execution_failed",
                message=f"{type(exc).__name__}: {str(exc)[:1000]}",
                retryable=isinstance(exc, (TimeoutError, ConnectionError)),
            )
            run = AgentRunEnvelope(
                eval_run_id=eval_run_id,
                case_id=case.case_id,
                experiment_id=experiment.experiment_id,
                candidate_id=experiment.candidate.candidate_id,
                repetition=repetition,
                input=case.input,
                session_id=session_id,
                response=response,
                tool_calls=event_calls,
                started_at=started,
                finished_at=utc_now(),
                timing={"latency_ms": (utc_now() - started).total_seconds() * 1000},
                outcome="failed",
                trace_refs=trace_refs,
                metadata={"query_id": query_id} if query_id else {},
                error=error,
            )
            available_kinds = {"final_output"}
            if event_calls:
                available_kinds.update({"tool_name", "tool_order", "tool_status"})
            evidence = TraceEvidence(
                available_kinds=available_kinds,
                tool_calls=event_calls,
                trajectory=[call.name for call in event_calls],
                metadata={
                    "capture_complete": False,
                    "tool_sequence_complete": False,
                    "offered_tools": list(ISOLATED_WORKSPACE_CORE_TOOLS),
                    "capability_profile": ISOLATED_CAPABILITY_PROFILE,
                },
            )
            results = evaluator_registry.run_profile(experiment.profile_id, case, run, evidence)
            for result in results:
                self.repository.save_result(experiment.experiment_id, attempt_id, result)
            self.repository.finish_attempt(attempt_id, status="failed", run=run, error=error)
            case_summary = evaluator_registry.summarize(case, results)
            case_summary["verdict"] = "fail"
            case_summary["execution_failure"] = True
            return {
                "case_id": case.case_id,
                "attempt_id": attempt_id,
                "response": _redact(response)[:8_000],
                "results": _redact([result.model_dump(mode="json") for result in results]),
                "summary": case_summary,
                "attempt_status": "failed",
                "agent_trace_export": trace_export_status,
                "agent_trace_error": trace_export_error,
                "trace_refs": [item.model_dump(mode="json") for item in trace_refs],
            }

    async def _project_langsmith(
        self,
        experiment: EvalExperiment,
        remote_dataset_name: str,
        local_outputs: dict[str, list[dict[str, Any]]],
    ) -> tuple[str | None, str | None]:
        from langsmith import Client
        from langsmith.evaluation import aevaluate

        client = Client(**langsmith_client_kwargs(self.settings))

        output_cursors: dict[str, int] = defaultdict(int)

        async def target(inputs: dict[str, Any]) -> dict[str, Any]:
            case_id = str(inputs.get("puddingclaw_case_id") or "")
            candidates = local_outputs.get(case_id) or []
            if not candidates:
                raise ValueError(f"No local output exists for enabled Case: {case_id}")
            cursor = output_cursors[case_id]
            output_cursors[case_id] += 1
            return candidates[min(cursor, len(candidates) - 1)]

        evaluators = []
        for spec in evaluator_registry.list_specs():
            evaluator_id = spec.evaluator_id

            def evaluator(
                inputs: dict[str, Any],
                outputs: dict[str, Any],
                reference_outputs: dict[str, Any],
                *,
                _id: str = evaluator_id,
            ) -> dict[str, Any]:
                del inputs, reference_outputs
                result = next((item for item in outputs.get("results", []) if item.get("evaluator_id") == _id), None)
                if result is None:
                    return {"key": _id, "value": "not_evaluated", "comment": "Local evaluator result missing"}
                payload: dict[str, Any] = {
                    "key": _id,
                    "value": result["outcome"],
                    "comment": result["reason"],
                    "metadata": {
                        "evaluator_version": result["evaluator_version"],
                        "dimension": result["dimension"],
                        "error_type": result.get("error_type"),
                        "evidence": result.get("evidence") or [],
                    },
                }
                if result.get("score") is not None:
                    payload["score"] = result["score"]
                return payload

            evaluator.__name__ = evaluator_id.replace(".", "_")
            evaluators.append(evaluator)

        async with asyncio.timeout(self.settings.projection_timeout_seconds):
            async_results = await aevaluate(
                target,
                data=remote_dataset_name,
                evaluators=evaluators,
                client=client,
                max_concurrency=1,
                num_repetitions=experiment.execution.repetitions,
                experiment_prefix=f"puddingclaw-{experiment.name}",
                metadata={
                    "puddingclaw_experiment_id": experiment.experiment_id,
                    "dataset_version_id": experiment.dataset_version_id,
                    "dataset_content_hash": experiment.dataset_content_hash,
                    "candidate_fingerprint": experiment.candidate.fingerprint,
                    "profile_id": experiment.profile_id,
                },
            )
            await async_results.wait()
        return str(async_results.experiment_id), str(async_results.url)

    async def retry_projection(self, experiment_id: str) -> EvalExperiment:
        experiment = self.repository.get_experiment(experiment_id)
        if experiment.status != ExperimentStatus.COMPLETED:
            raise ValueError("Only a completed local Experiment can be projected")
        outbox = self.repository.claim_outbox(
            "langsmith", "experiment_projection", experiment_id
        )
        if outbox is None:
            raise ValueError("No pending LangSmith projection is available to claim")
        outbox_id = str(outbox["outbox_id"])
        try:
            outputs = self.repository.load_projection_outputs(experiment_id)
            for attempts in outputs.values():
                for attempt in attempts:
                    attempt["response"] = _redact(attempt.get("response", ""))
                    attempt["results"] = _redact(attempt.get("results", []))
            if not outputs:
                raise ValueError("Experiment has no local Attempt results")
            bundle = self.repository.export_bundle(
                experiment.dataset_id, experiment.dataset_version
            )
            mapping = LangSmithDatasetAdapter(self.repository, self.settings).sync_dataset(bundle)
            remote_id, remote_url = await self._project_langsmith(
                experiment, mapping["remote_name"], outputs
            )
        except Exception as exc:
            self.repository.release_outbox(
                outbox_id,
                str(_redact(f"{type(exc).__name__}: {str(exc)[:1000]}")),
            )
            raise
        summary = {
            **experiment.summary,
            "dataset_projection": "synced",
            "experiment_projection": "synced",
            "langsmith_projection": "synced",
        }
        summary.pop("langsmith_error", None)
        updated = experiment.model_copy(
            update={
                "remote_experiment_id": remote_id,
                "remote_url": remote_url,
                "summary": summary,
            }
        )
        return self.repository.complete_experiment_projection(updated, outbox_id)

    async def run(self, experiment_id: str) -> EvalExperiment:
        experiment = self.repository.get_experiment(experiment_id)
        experiment = experiment.model_copy(update={"status": ExperimentStatus.RUNNING, "started_at": utc_now()})
        self.repository.update_experiment(experiment, expected_status=ExperimentStatus.QUEUED)
        runtime_root = self._runtime_root(experiment_id)
        try:
            dataset = self.repository.get_dataset(experiment.dataset_id, experiment.dataset_version)
            if dataset.current_version_id != experiment.dataset_version_id:
                raise ValueError("Pinned Dataset version identity mismatch")
            drifted = verify_candidate_snapshot(self.backend_dir, experiment.candidate)
            if drifted:
                raise ValueError(f"Candidate snapshot changed before execution: {', '.join(drifted)}")
            self._initialize_isolated_runtime(runtime_root)
            dataset_data_classification = str(
                dataset.metadata.get("data_classification") or "internal"
            ).lower()
            if dataset_data_classification not in {
                "public",
                "internal",
                "sensitive",
                "restricted",
            }:
                dataset_data_classification = "restricted"
            outputs: dict[str, list[dict[str, Any]]] = defaultdict(list)
            all_attempts: list[dict[str, Any]] = []
            # Phase 1 deliberately serializes execution. Resource-group-aware
            # concurrency comes only after durable locking exists.
            for repetition in range(experiment.execution.repetitions):
                for case in dataset.cases:
                    if not case.enabled:
                        continue
                    latest = self.repository.get_experiment(experiment_id)
                    if latest.status == ExperimentStatus.CANCEL_REQUESTED:
                        cancelled = latest.model_copy(
                            update={"status": ExperimentStatus.CANCELLED, "finished_at": utc_now()}
                        )
                        self.repository.cancel_running_attempts(experiment_id, "Experiment cancellation requested")
                        return self.repository.update_experiment(
                            cancelled, expected_status=ExperimentStatus.CANCEL_REQUESTED
                        )
                    result = await self._run_case(
                        experiment,
                        case,
                        repetition,
                        runtime_root,
                        dataset_data_classification,
                    )
                    all_attempts.append(result)
                    # LangSmith Example identity is per Case. For repetitions,
                    # retain the last public output and keep aggregate locally.
                    outputs[case.case_id].append(result)

            summary = {
                "case_attempts": len(all_attempts),
                "completed_attempts": sum(
                    item.get("attempt_status") == "completed" for item in all_attempts
                ),
                "failed_attempts": sum(
                    item.get("attempt_status") == "failed" for item in all_attempts
                ),
                "cancelled_attempts": sum(
                    item.get("attempt_status") == "cancelled" for item in all_attempts
                ),
                "determinate": sum(item["summary"].get("verdict") != "indeterminate" for item in all_attempts),
                "passed": sum(item["summary"].get("verdict") == "pass" for item in all_attempts),
                "failed": sum(item["summary"].get("verdict") == "fail" for item in all_attempts),
                "critical_failures": sum(bool(item["summary"].get("critical_failure")) for item in all_attempts),
                "indeterminate": sum(item["summary"].get("verdict") == "indeterminate" for item in all_attempts),
                "effective_max_concurrency": 1,
                "requested_max_concurrency": experiment.execution.max_concurrency,
            }
            if not all_attempts:
                summary["empty_execution_set"] = True
                summary["indeterminate"] = 1
            dimension_buckets: dict[str, dict[str, Any]] = {}
            for attempt in all_attempts:
                for result in attempt.get("results", []):
                    dimension = str(result.get("dimension") or "unknown")
                    bucket = dimension_buckets.setdefault(
                        dimension,
                        {
                            "sample_count": 0,
                            "applicable_count": 0,
                            "pass_count": 0,
                            "fail_count": 0,
                            "not_applicable_count": 0,
                            "not_evaluated_count": 0,
                            "error_count": 0,
                            "scores": [],
                            "evaluator_versions": set(),
                        },
                    )
                    outcome = str(result.get("outcome") or "error")
                    bucket["sample_count"] += 1
                    count_key = f"{outcome}_count"
                    if count_key in bucket:
                        bucket[count_key] += 1
                    if outcome in {"pass", "fail"}:
                        bucket["applicable_count"] += 1
                    if result.get("score") is not None:
                        bucket["scores"].append(float(result["score"]))
                    bucket["evaluator_versions"].add(
                        f"{result.get('evaluator_id')}@{result.get('evaluator_version')}"
                    )
            dimensions: dict[str, Any] = {}
            for dimension, bucket in dimension_buckets.items():
                scores = bucket.pop("scores")
                versions = sorted(bucket.pop("evaluator_versions"))
                expected = bucket["sample_count"] - bucket["not_applicable_count"]
                dimensions[dimension] = {
                    **bucket,
                    "score": sum(scores) / len(scores) if scores else None,
                    "coverage": bucket["applicable_count"] / expected if expected else None,
                    "evaluator_versions": versions,
                }
            summary["dimensions"] = dimensions
            summary["applicable_count"] = sum(
                item["applicable_count"] for item in dimensions.values()
            )
            expected_metrics = sum(
                item["sample_count"] - item["not_applicable_count"]
                for item in dimensions.values()
            )
            summary["coverage"] = (
                summary["applicable_count"] / expected_metrics if expected_metrics else None
            )
            trace_statuses = [str(item.get("agent_trace_export") or "not_started") for item in all_attempts]
            if not trace_statuses:
                summary["agent_trace_export"] = "not_started"
            elif all(status == "synced" for status in trace_statuses):
                summary["agent_trace_export"] = "synced"
            elif all(status == trace_statuses[0] for status in trace_statuses):
                summary["agent_trace_export"] = trace_statuses[0]
            else:
                summary["agent_trace_export"] = "partial"
            summary["agent_trace_export_counts"] = {
                status: trace_statuses.count(status) for status in sorted(set(trace_statuses))
            }
            remote_id = None
            remote_url = None
            if not self.settings.enabled or not self.settings.api_key:
                summary["dataset_projection"] = "disabled"
                summary["experiment_projection"] = "disabled"
            elif dataset_data_classification in {"sensitive", "restricted"} or any(
                case.data_classification in {"sensitive", "restricted"}
                for case in dataset.cases
                if case.enabled
            ):
                summary["dataset_projection"] = "blocked_by_data_policy"
                summary["experiment_projection"] = "blocked_by_data_policy"
            else:
                try:
                    bundle = self.repository.export_bundle(
                        experiment.dataset_id, experiment.dataset_version
                    )
                    mapping = LangSmithDatasetAdapter(
                        self.repository, self.settings
                    ).sync_dataset(bundle)
                except Exception as exc:
                    projection_error = str(
                        _redact(f"{type(exc).__name__}: {str(exc)[:1000]}")
                    )
                    self.repository.enqueue_outbox(
                        "langsmith",
                        "experiment_projection",
                        experiment_id,
                        {
                            "experiment_id": experiment_id,
                            "error": projection_error,
                        },
                    )
                    summary["dataset_projection"] = "pending"
                    summary["experiment_projection"] = "not_started"
                    summary["langsmith_error"] = projection_error[:500]
                else:
                    summary["dataset_projection"] = "synced"
                    try:
                        remote_id, remote_url = await self._project_langsmith(
                            experiment, mapping["remote_name"], outputs
                        )
                    except Exception as exc:
                        projection_error = str(
                            _redact(f"{type(exc).__name__}: {str(exc)[:1000]}")
                        )
                        self.repository.enqueue_outbox(
                            "langsmith",
                            "experiment_projection",
                            experiment_id,
                            {"experiment_id": experiment_id, "error": projection_error},
                        )
                        summary["experiment_projection"] = "pending"
                        summary["langsmith_error"] = projection_error[:500]
                    else:
                        summary["experiment_projection"] = "synced"
            summary["langsmith_projection"] = (
                "pending"
                if "pending"
                in {summary["dataset_projection"], summary["experiment_projection"]}
                else summary["experiment_projection"]
            )
            latest = self.repository.get_experiment(experiment_id)
            if latest.status == ExperimentStatus.CANCEL_REQUESTED:
                self.repository.cancel_running_attempts(experiment_id, "Experiment cancellation requested")
                cancelled = latest.model_copy(
                    update={"status": ExperimentStatus.CANCELLED, "finished_at": utc_now()}
                )
                return self.repository.update_experiment(
                    cancelled, expected_status=ExperimentStatus.CANCEL_REQUESTED
                )
            completed = latest.model_copy(
                update={
                    "status": ExperimentStatus.COMPLETED,
                    "verdict": (
                        "fail"
                        if summary["failed"]
                        or summary["critical_failures"]
                        or summary["failed_attempts"]
                        else "indeterminate"
                        if summary["indeterminate"]
                        else "pass"
                    ),
                    "finished_at": utc_now(),
                    "remote_experiment_id": remote_id,
                    "remote_url": remote_url,
                    "summary": summary,
                }
            )
            return self.repository.update_experiment(
                completed, expected_status=ExperimentStatus.RUNNING
            )
        except Exception as exc:
            latest = self.repository.get_experiment(experiment_id)
            if latest.status in {ExperimentStatus.CANCEL_REQUESTED, ExperimentStatus.CANCELLED}:
                if latest.status == ExperimentStatus.CANCEL_REQUESTED:
                    latest = latest.model_copy(
                        update={"status": ExperimentStatus.CANCELLED, "finished_at": utc_now()}
                    )
                    return self.repository.update_experiment(
                        latest, expected_status=ExperimentStatus.CANCEL_REQUESTED
                    )
                return latest
            failed = latest.model_copy(
                update={
                    "status": ExperimentStatus.FAILED,
                    "finished_at": utc_now(),
                    "error": EvalError(
                        code="experiment_failed",
                        message=f"{type(exc).__name__}: {str(exc)[:1000]}",
                        retryable=isinstance(exc, (TimeoutError, ConnectionError)),
                    ),
                }
            )
            return self.repository.update_experiment(
                failed, expected_status=ExperimentStatus.RUNNING
            )
        finally:
            if not experiment.execution.preserve_workspaces:
                shutil.rmtree(runtime_root, ignore_errors=True)
