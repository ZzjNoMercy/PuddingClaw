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
    capability_for_profile,
    verify_candidate_snapshot,
)
from .code_eval import prepare_code_repository, verify_code_case
from .contracts import (
    AgentRunEnvelope,
    EvalCase,
    EvalError,
    EvalExperiment,
    EvaluationResult,
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
from .official_swebench import run_official_swebench_harness
from .repository import ConflictError, EvaluationRepository
from .settings import LangSmithSettings
from .swebench_adapter import swebench_prediction_manifest


class EvaluationRunner:
    def __init__(self, repository: EvaluationRepository, settings: LangSmithSettings, backend_dir: Path) -> None:
        self.repository = repository
        self.settings = settings
        self.backend_dir = backend_dir.resolve()

    def _runtime_root(self, experiment_id: str) -> Path:
        from runtime_identity.paths import PuddingClawPaths

        return PuddingClawPaths.from_environment().data() / "evaluation-runs" / experiment_id

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

        session_manager.initialize(sessions_dir=root / "sessions")
        project_registry.initialize(root)
        attachment_store.initialize(root)
        deepagents_agent_manager.initialize(self.backend_dir, user_root=root)

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
        if case.code is not None:
            prepare_code_repository(workspace, case.code)
        record = project_registry.register(
            str(workspace),
            name=f"Eval {case.name}",
            trusted=True,
        )
        if case.code is not None:
            # Code execution is never allowed to inherit the production host-spawn mode.
            # A missing kernel runner fails closed in DeferredKernelWorkspaceBackend.
            project_registry.set_execution_mode(record.project_id, "kernel")
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
        capability_profile, offered_tools = capability_for_profile(experiment.profile_id)
        candidate_workspace_backend: Any | None = None
        workspace: Path | None = None
        deadline = 0.0
        turn_outcome: str | None = None
        try:
            async with asyncio.timeout(experiment.execution.timeout_seconds):
                workspace, project_id = await self._daemon_call(
                    lambda: self._prepare_workspace(runtime_root, case, repetition)
                )
            if (
                case.code is not None
                and case.code.repository.swebench is not None
            ):
                from .swebench_agent_backend import prepare_swebench_agent_backend

                self._update_progress(
                    experiment.experiment_id,
                    stage="candidate_environment",
                    message="正在准备与官方 TestSpec 一致的 Agent 依赖环境",
                    current_case_id=case.case_id,
                    current_case_name=case.name,
                )
                scratch_path = runtime_root / "agent-scratch" / case.case_id / f"attempt-{repetition}"
                candidate_workspace_backend = await prepare_swebench_agent_backend(
                    case,
                    workspace_path=workspace,
                    scratch_path=scratch_path,
                    experiment_id=experiment.experiment_id,
                )
                self._update_progress(
                    experiment.experiment_id,
                    stage="agent_running",
                    message="Agent 正在官方依赖环境中处理 Case",
                    current_case_id=case.case_id,
                    current_case_name=case.name,
                )
            # Environment/materialization time is platform setup, not Agent
            # reasoning time.  Start the Case budget only after the exact
            # candidate runtime is ready.
            deadline = asyncio.get_running_loop().time() + experiment.execution.timeout_seconds
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
                        anonymizer=lambda value: _redact(value, profile=self.settings.redaction_profile),
                        hide_inputs=lambda value: _redact(value, profile=self.settings.redaction_profile),
                        hide_outputs=lambda value: _redact(value, profile=self.settings.redaction_profile),
                        hide_metadata=lambda value: _redact(value, profile=self.settings.redaction_profile),
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
                    trace_errors.append(str(_redact(f"{type(exc).__name__}: {str(exc)[:500]}")))
            actions: list[tuple[str, str]] = []
            if case.input.message:
                actions.append(("user", case.input.message))
            else:
                actions.extend((turn.role, turn.content) for turn in case.input.turns)
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
                        "Use this value instead of wall-clock time.\n\n" + message
                    )
                response = ""
                done_seen = False
                turn_outcome = None
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
                        credential_name=experiment.candidate.credential_name,
                        user_id="evaluation_worker",
                        interaction_mode="auto",
                        callbacks_override=callbacks,
                        evaluation_tool_allowlist=set(experiment.candidate.config.get("tool_allowlist") or []),
                        disable_mcp=True,
                        evaluation_builtin_tool_allowlist=set(offered_tools),
                        evaluation_required_toolset=set(offered_tools),
                        evaluation_workspace_backend=candidate_workspace_backend,
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
                            stream_error = str(payload.get("message") or payload.get("error") or "Agent stream error")
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
                    (index for index, item in enumerate(remaining_event_calls) if item.name == callback_call.name),
                    None,
                )
                if match is not None:
                    remaining_event_calls.pop(match)
            tool_calls.extend(remaining_event_calls)
            tool_calls = [call.model_copy(update={"sequence": index}) for index, call in enumerate(tool_calls)]
            using_sse_tool_fallback = bool(remaining_event_calls)
            code_verification: dict[str, Any] | None = None
            if case.code is not None:
                verification_root = runtime_root / "verification" / case.case_id / f"attempt-{repetition}"
                verification_root.mkdir(parents=True, exist_ok=True)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("Case timeout exhausted before code verification")
                async with asyncio.timeout(remaining):
                    code_verification = await self._daemon_call(
                        lambda: verify_code_case(
                            workspace,
                            verification_root,
                            case.code,
                        )
                    )
            run_metadata: dict[str, Any] = {"query_id": query_id} if query_id else {}
            if code_verification is not None:
                run_metadata["code_verification"] = code_verification
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
                artifacts=(
                    [
                        {
                            "kind": "code_patch",
                            "sha256": code_verification.get("patch_sha256"),
                            "changed_paths": code_verification.get("changed_paths") or [],
                        }
                    ]
                    if code_verification is not None
                    else []
                ),
                metadata=run_metadata,
            )
            evidence = callback_evidence.model_copy(
                update={
                    "available_kinds": set(callback_evidence.available_kinds)
                    | {"final_output"}
                    | ({"tool_name", "tool_order", "tool_status"} if using_sse_tool_fallback else set()),
                    "tool_calls": tool_calls,
                    "trajectory": [call.name for call in tool_calls],
                    "metadata": {
                        **callback_evidence.metadata,
                        "capture_complete": False
                        if using_sse_tool_fallback
                        else callback_evidence.metadata.get("capture_complete", True),
                        "tool_sequence_complete": tool_sequence_complete,
                        "tool_evidence_source": "sse_fallback" if using_sse_tool_fallback else "callback",
                        "offered_tools": list(offered_tools),
                        "capability_profile": capability_profile,
                    },
                }
            )
            if code_verification is not None:
                evidence = evidence.model_copy(
                    update={
                        "available_kinds": set(evidence.available_kinds) | {"code_patch", "code_verification"},
                        "artifacts": [
                            *evidence.artifacts,
                            {
                                "kind": "code_patch",
                                "summary": (
                                    f"sha256={code_verification.get('patch_sha256')}; "
                                    f"changed={code_verification.get('changed_paths') or []}"
                                )[:500],
                            },
                        ],
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
            # SWE-bench scores the candidate repository at the Agent boundary.
            # If the Agent already produced a patch, submit that exact bounded
            # workspace when either the wall-clock budget or the configured
            # model-call budget ends.  A missing final prose response is not a
            # reason to discard a benchmark prediction.
            budget_ended = isinstance(exc, TimeoutError) or turn_outcome == "budget_exceeded"
            if (
                budget_ended
                and workspace is not None
                and case.code is not None
                and case.code.repository.swebench is not None
            ):
                try:
                    verification_root = (
                        runtime_root / "verification" / case.case_id / f"attempt-{repetition}"
                    )
                    verification_root.mkdir(parents=True, exist_ok=True)
                    async with asyncio.timeout(180):
                        timeout_verification = await self._daemon_call(
                            lambda: verify_code_case(workspace, verification_root, case.code)
                        )
                except Exception:
                    timeout_verification = None
                if timeout_verification is not None and str(
                    timeout_verification.get("patch") or ""
                ).strip():
                    last_tool = event_calls[-1].name if event_calls else None
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
                        tool_calls=event_calls,
                        started_at=started,
                        finished_at=utc_now(),
                        timing={"latency_ms": (utc_now() - started).total_seconds() * 1000},
                        outcome="completed",
                        trace_refs=trace_refs,
                        artifacts=[
                            {
                                "kind": "code_patch",
                                "sha256": timeout_verification.get("patch_sha256"),
                                "changed_paths": timeout_verification.get("changed_paths") or [],
                            }
                        ],
                        metadata={
                            **({"query_id": query_id} if query_id else {}),
                            "code_verification": timeout_verification,
                            "agent_budget": {
                                "exhausted": True,
                                "reason": "timeout" if isinstance(exc, TimeoutError) else "model_call_limit",
                                "timeout_seconds": experiment.execution.timeout_seconds,
                                "tool_events": len(event_calls),
                                "last_tool": last_tool,
                                "submission": "workspace_patch_at_budget_boundary",
                            },
                        },
                    )
                    sequence_complete = not event_calls or event_calls[-1].succeeded is not None
                    evidence = TraceEvidence(
                        available_kinds={
                            "final_output",
                            "tool_name",
                            "tool_order",
                            "tool_status",
                            "code_patch",
                            "code_verification",
                        },
                        tool_calls=event_calls,
                        trajectory=[call.name for call in event_calls],
                        artifacts=[
                            {
                                "kind": "code_patch",
                                "summary": (
                                    "budget-boundary submission; "
                                    f"sha256={timeout_verification.get('patch_sha256')}; "
                                    f"changed={timeout_verification.get('changed_paths') or []}"
                                )[:500],
                            }
                        ],
                        metadata={
                            "capture_complete": sequence_complete,
                            "tool_sequence_complete": sequence_complete,
                            "offered_tools": list(offered_tools),
                            "capability_profile": capability_profile,
                            "agent_budget_exhausted": True,
                        },
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
                        "agent_budget_exhausted": True,
                        "agent_trace_export": trace_export_status,
                        "agent_trace_error": trace_export_error,
                        "trace_refs": [item.model_dump(mode="json") for item in trace_refs],
                    }
            if isinstance(exc, TimeoutError):
                last_tool = event_calls[-1].name if event_calls else "none"
                error_message = (
                    f"Case exceeded the {experiment.execution.timeout_seconds}s Agent budget "
                    f"after {len(event_calls)} tool events; last tool={last_tool}; "
                    "no non-empty patch was available for budget-boundary submission"
                )
            else:
                error_message = f"{type(exc).__name__}: {str(exc)[:1000]}"
            error = EvalError(
                code="case_execution_failed",
                message=error_message,
                retryable=isinstance(exc, (TimeoutError, ConnectionError)),
                details={
                    "stage": "agent_running" if deadline else "candidate_environment",
                    "timeout_seconds": experiment.execution.timeout_seconds,
                    "tool_events": len(event_calls),
                    "last_tool": event_calls[-1].name if event_calls else None,
                },
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
                    "offered_tools": list(offered_tools),
                    "capability_profile": capability_profile,
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
        finally:
            if candidate_workspace_backend is not None:
                await self._daemon_call(candidate_workspace_backend.close)

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

    async def _run_and_apply_official_swebench(
        self,
        experiment: EvalExperiment,
        dataset: Any,
        runtime_root: Path,
        all_attempts: list[dict[str, Any]],
        *,
        allow_partial_predictions: bool = False,
    ) -> dict[str, Any]:
        envelopes = self.repository.load_run_envelopes(experiment.experiment_id)
        prediction_manifest = swebench_prediction_manifest(
            dataset,
            envelopes,
            model_name_or_path=(experiment.candidate.llm_model_id or experiment.candidate.name),
        )
        if prediction_manifest["missing_instance_ids"] and not allow_partial_predictions:
            return {
                "status": "not_started",
                "reason": "Agent did not produce a valid patch for every enabled SWE-bench Case",
                "missing_instance_ids": prediction_manifest["missing_instance_ids"],
            }
        latest = self.repository.get_experiment(experiment.experiment_id)
        self.repository.update_experiment(
            latest.model_copy(
                update={
                    "summary": {
                        **latest.summary,
                        "progress": {
                            **dict(latest.summary.get("progress") or {}),
                            "stage": "official_verifier",
                            "message": "Agent patch 已生成，正在使用官方 Docker Harness 判卷",
                            "completed": 0,
                            "current_index": 0,
                            "failed": 0,
                            "total": len(prediction_manifest["predictions"]),
                            "current_case_id": None,
                            "current_case_name": None,
                            "updated_at": utc_now().isoformat(),
                        },
                        "swebench_official_harness": {
                            "status": "running",
                            "total": len(prediction_manifest["predictions"]),
                        },
                    }
                }
            ),
            expected_status=ExperimentStatus.RUNNING,
        )
        official = await run_official_swebench_harness(
            experiment,
            dataset,
            envelopes,
            runtime_root,
            allow_partial_predictions=allow_partial_predictions,
        )
        case_by_instance = {
            case.code.repository.swebench.instance_id: case
            for case in dataset.cases
            if case.enabled and case.code is not None and case.code.repository.swebench is not None
        }
        attempt_by_instance = {
            item["instance_id"]: item["attempt_id"] for item in prediction_manifest["predictions"]
        }
        code_evaluator = evaluator_registry.get_registered("code_verification.v1")
        assert code_evaluator is not None
        for instance_id, instance_result in official["results"].items():
            attempt_id = attempt_by_instance[instance_id]
            case = case_by_instance[instance_id]
            envelope = next(
                item
                for item in envelopes[case.case_id]
                if str(item.get("_attempt_id") or "") == attempt_id
            )
            run = AgentRunEnvelope.model_validate(
                {key: value for key, value in envelope.items() if not key.startswith("_")}
            )
            verification = dict(run.metadata.get("code_verification") or {})
            verification.update(
                {
                    "status": instance_result["status"],
                    "passed": instance_result.get("resolved"),
                    "reason": instance_result["reason"],
                    "official_harness": {
                        "provenance": "platform_managed_official_harness",
                        "package": official["receipt"].get("package"),
                        "run_id": official["receipt"].get("run_id"),
                        "report_sha256": official["receipt"].get("report_sha256"),
                        "predictions_sha256": official["receipt"].get("predictions_sha256"),
                        "patch_sha256": official["receipt"].get("patch_sha256", {}).get(instance_id),
                        "result": instance_result,
                    },
                }
            )
            run = run.model_copy(
                update={"metadata": {**run.metadata, "code_verification": verification}}
            )
            result = code_evaluator[1](
                case,
                run,
                TraceEvidence(available_kinds={"code_patch", "code_verification"}),
            )
            self.repository.update_attempt_run(attempt_id, run)
            self.repository.save_result(experiment.experiment_id, attempt_id, result)
            attempt = next(item for item in all_attempts if item["attempt_id"] == attempt_id)
            attempt["results"] = [
                result.model_dump(mode="json")
                if item.get("evaluator_id") == "code_verification.v1"
                else item
                for item in attempt["results"]
            ]
            typed_results = [EvaluationResult.model_validate(item) for item in attempt["results"]]
            attempt["summary"] = evaluator_registry.summarize(case, typed_results)
        receipt = dict(official["receipt"])
        aggregate = receipt.get("aggregate") or {}
        return {
            "status": official["status"],
            "reason": official["reason"],
            "package": receipt.get("package"),
            "provenance": receipt.get("provenance"),
            "run_id": receipt.get("run_id"),
            "total": len(official["results"]),
            "resolved": len(aggregate.get("resolved_ids") or []),
            "unresolved": len(aggregate.get("unresolved_ids") or []),
            "errors": sum(item["status"] == "error" for item in official["results"].values()),
            "instance_results": {
                instance_id: {
                    "status": item.get("status"),
                    "resolved": item.get("resolved"),
                }
                for instance_id, item in official["results"].items()
            },
            "resolve_rate": (
                len(aggregate.get("resolved_ids") or []) / len(official["results"])
                if official["results"]
                else None
            ),
            "duration_seconds": receipt.get("duration_seconds"),
            "report_sha256": receipt.get("report_sha256"),
            "predictions_sha256": receipt.get("predictions_sha256"),
            "source_snapshot_sha256": receipt.get("source_snapshot_sha256"),
            "missing_instance_ids": list(receipt.get("missing_instance_ids") or []),
            "docker_server_version": receipt.get("docker_server_version"),
            "docker_architecture": receipt.get("docker_architecture"),
            "container_policy": receipt.get("container_policy"),
            "output_tail": str(receipt.get("output_tail") or "")[-4_000:],
        }

    async def retry_projection(self, experiment_id: str) -> EvalExperiment:
        experiment = self.repository.get_experiment(experiment_id)
        if experiment.status != ExperimentStatus.COMPLETED:
            raise ValueError("Only a completed local Experiment can be projected")
        outbox = self.repository.claim_outbox("langsmith", "experiment_projection", experiment_id)
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
            bundle = self.repository.export_bundle(experiment.dataset_id, experiment.dataset_version)
            mapping = LangSmithDatasetAdapter(self.repository, self.settings).sync_dataset(bundle)
            remote_id, remote_url = await self._project_langsmith(experiment, mapping["remote_name"], outputs)
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

    def _update_progress(self, experiment_id: str, **changes: Any) -> EvalExperiment:
        """Durably expose coarse-grained progress without weakening cancellation CAS."""

        latest = self.repository.get_experiment(experiment_id)
        if latest.status != ExperimentStatus.RUNNING:
            return latest
        progress = {
            **dict(latest.summary.get("progress") or {}),
            **changes,
            "updated_at": utc_now().isoformat(),
        }
        try:
            return self.repository.update_experiment(
                latest.model_copy(update={"summary": {**latest.summary, "progress": progress}}),
                expected_status=ExperimentStatus.RUNNING,
            )
        except ConflictError:
            return self.repository.get_experiment(experiment_id)

    def _load_persisted_attempts_for_scoring(
        self,
        experiment_id: str,
        dataset: Any,
    ) -> list[dict[str, Any]]:
        case_by_id = {case.case_id: case for case in dataset.cases}
        grouped: dict[str, dict[str, Any]] = {}
        for row in self.repository.list_results(experiment_id):
            attempt_id = str(row["attempt_id"])
            attempt = grouped.setdefault(
                attempt_id,
                {
                    "case_id": str(row["case_id"]),
                    "attempt_id": attempt_id,
                    "attempt_status": str(row["attempt_status"]),
                    "results": [],
                },
            )
            if row.get("result") is not None:
                attempt["results"].append(row["result"])
        attempts = list(grouped.values())
        for attempt in attempts:
            case = case_by_id.get(attempt["case_id"])
            if case is None:
                raise ValueError(f"Persisted Attempt references unknown Case: {attempt['case_id']}")
            typed_results = [EvaluationResult.model_validate(item) for item in attempt["results"]]
            attempt["summary"] = evaluator_registry.summarize(case, typed_results)
        return attempts

    def _effective_swebench_attempts_for_scoring(
        self,
        experiment: EvalExperiment,
        dataset: Any,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        """Select one effective Attempt per Case while retaining full history.

        A missing-Case resume appends a new Attempt instead of deleting the
        failed one. For SWE-bench Cases, the Attempt selected by the immutable
        prediction manifest is authoritative; Cases still lacking a patch use
        their latest Attempt so execution failures remain visible.
        """

        history = self._load_persisted_attempts_for_scoring(
            experiment.experiment_id,
            dataset,
        )
        manifest = swebench_prediction_manifest(
            dataset,
            self.repository.load_run_envelopes(experiment.experiment_id),
            model_name_or_path=(experiment.candidate.llm_model_id or experiment.candidate.name),
        )
        selected_by_case_id: dict[str, str] = {}
        case_by_instance = {
            case.code.repository.swebench.instance_id: case.case_id
            for case in dataset.cases
            if case.enabled
            and case.code is not None
            and case.code.repository.swebench is not None
        }
        for prediction in manifest["predictions"]:
            case_id = case_by_instance.get(str(prediction["instance_id"]))
            if case_id is not None:
                selected_by_case_id[case_id] = str(prediction["attempt_id"])

        attempts_by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for attempt in history:
            attempts_by_case[str(attempt["case_id"])].append(attempt)
        effective: list[dict[str, Any]] = []
        for case in dataset.cases:
            if not case.enabled:
                continue
            candidates = attempts_by_case.get(case.case_id) or []
            if not candidates:
                continue
            selected_attempt_id = selected_by_case_id.get(case.case_id)
            selected = next(
                (
                    item
                    for item in candidates
                    if str(item["attempt_id"]) == selected_attempt_id
                ),
                candidates[-1],
            )
            effective.append(selected)
        return effective, history, manifest

    @staticmethod
    def _refresh_attempt_scoring_summary(
        base_summary: dict[str, Any],
        all_attempts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Recompute score-bearing aggregates without touching Agent evidence."""

        summary = dict(base_summary)
        summary.update(
            {
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
                "determinate": sum(
                    item["summary"].get("verdict") != "indeterminate" for item in all_attempts
                ),
                "passed": sum(
                    item["summary"].get("verdict") == "pass" for item in all_attempts
                ),
                "failed": sum(
                    item["summary"].get("verdict") == "fail" for item in all_attempts
                ),
                "critical_failures": sum(
                    bool(item["summary"].get("critical_failure")) for item in all_attempts
                ),
                "indeterminate": sum(
                    item["summary"].get("verdict") == "indeterminate" for item in all_attempts
                ),
            }
        )
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
        return summary

    @staticmethod
    def _verdict_from_summary(summary: dict[str, Any]) -> str:
        if summary.get("failed") or summary.get("critical_failures") or summary.get("failed_attempts"):
            return "fail"
        if summary.get("indeterminate"):
            return "indeterminate"
        return "pass"

    async def run_official_verifier_replay(self, experiment_id: str) -> EvalExperiment:
        """Re-score persisted SWE-bench patches without executing the Agent."""

        experiment = self.repository.get_experiment(experiment_id)
        replay = dict(experiment.summary.get("swebench_verifier_replay") or {})
        total = int(replay.get("total") or 0)
        running = experiment.model_copy(
            update={
                "status": ExperimentStatus.RUNNING,
                "started_at": utc_now(),
                "finished_at": None,
                "error": None,
                "summary": {
                    **experiment.summary,
                    "swebench_verifier_replay": {
                        **replay,
                        "status": "running",
                        "started_at": utc_now().isoformat(),
                    },
                    "progress": {
                        "stage": "official_verifier",
                        "message": "正在复用已保存的 Agent patch 重新判卷",
                        "total": total,
                        "completed": 0,
                        "failed": 0,
                        "updated_at": utc_now().isoformat(),
                    },
                },
            }
        )
        self.repository.update_experiment(
            running,
            expected_status=ExperimentStatus.QUEUED,
        )
        runtime_root = self._runtime_root(experiment_id)
        try:
            dataset = self.repository.get_dataset(
                running.dataset_id,
                running.dataset_version,
            )
            if dataset.current_version_id != running.dataset_version_id:
                raise ValueError("Pinned Dataset version identity mismatch")
            envelopes = self.repository.load_run_envelopes(experiment_id)
            manifest = swebench_prediction_manifest(
                dataset,
                envelopes,
                model_name_or_path=(running.candidate.llm_model_id or running.candidate.name),
            )
            if not manifest["predictions"]:
                raise ValueError("Experiment has no persisted SWE-bench patch to verify")
            selected_attempt_ids = {
                str(item["attempt_id"]) for item in manifest["predictions"]
            }
            all_attempts = self._load_persisted_attempts_for_scoring(
                experiment_id,
                dataset,
            )
            completed_attempt_ids = {
                str(item["attempt_id"])
                for item in all_attempts
                if item.get("attempt_status") == "completed"
            }
            if not selected_attempt_ids <= completed_attempt_ids:
                raise ValueError("Persisted SWE-bench patches do not belong to completed Attempts")
            self._initialize_isolated_runtime(runtime_root)
            official = await self._run_and_apply_official_swebench(
                running,
                dataset,
                runtime_root,
                all_attempts,
                allow_partial_predictions=True,
            )
            latest = self.repository.get_experiment(experiment_id)
            if latest.status == ExperimentStatus.CANCEL_REQUESTED:
                cancelled = latest.model_copy(
                    update={"status": ExperimentStatus.CANCELLED, "finished_at": utc_now()}
                )
                return self.repository.update_experiment(
                    cancelled,
                    expected_status=ExperimentStatus.CANCEL_REQUESTED,
                )
            summary = self._refresh_attempt_scoring_summary(latest.summary, all_attempts)
            completed_at = utc_now()
            replay_result = {
                **dict(summary.get("swebench_verifier_replay") or {}),
                "status": official["status"],
                "completed_at": completed_at.isoformat(),
                "docker_architecture": official.get("docker_architecture"),
                "resolved": official.get("resolved"),
                "total": official.get("total", total),
                "report_sha256": official.get("report_sha256"),
                "predictions_sha256": official.get("predictions_sha256"),
                "instance_results": official.get("instance_results"),
                "missing_instance_ids": list(manifest["missing_instance_ids"]),
            }
            history = list(summary.get("swebench_verifier_replay_history") or [])
            history.append(replay_result)
            summary.update(
                {
                    "swebench_official_harness": official,
                    "swebench_predictions_available": not manifest["missing_instance_ids"],
                    "swebench_predictions_count": len(manifest["predictions"]),
                    "swebench_missing_predictions": len(manifest["missing_instance_ids"]),
                    "swebench_verifier_partial": bool(manifest["missing_instance_ids"]),
                    "swebench_verifier_replay": replay_result,
                    "swebench_verifier_replay_history": history,
                    "progress": {
                        "stage": "completed",
                        "message": (
                            f"已有 {len(manifest['predictions'])} 份 patch 判卷完成；"
                            f"{len(manifest['missing_instance_ids'])} 个 Case 缺少 patch"
                            if manifest["missing_instance_ids"]
                            else "已复用 Agent patch 完成 Docker 重新判卷"
                        ),
                        "total": total,
                        "completed": total,
                        "failed": official.get("errors", 0),
                        "updated_at": completed_at.isoformat(),
                    },
                }
            )
            projection_was_published = bool(replay.get("langsmith_projection_was_published"))
            projection_pending = (
                projection_was_published and self.settings.enabled and bool(self.settings.api_key)
            )
            if projection_was_published:
                summary["experiment_projection"] = (
                    "pending" if projection_pending else "stale_after_verifier_replay"
                )
                summary["langsmith_projection"] = (
                    "pending" if projection_pending else "stale_after_verifier_replay"
                )
            completed = latest.model_copy(
                update={
                    "status": ExperimentStatus.COMPLETED,
                    "verdict": self._verdict_from_summary(summary),
                    "finished_at": completed_at,
                    "summary": summary,
                    "error": None,
                }
            )
            completed = self.repository.update_experiment(
                completed,
                expected_status=ExperimentStatus.RUNNING,
            )
            if projection_pending:
                self.repository.enqueue_outbox(
                    "langsmith",
                    "experiment_projection",
                    experiment_id,
                    {
                        "experiment_id": experiment_id,
                        "reason": "official_verifier_replay_updated_scores",
                    },
                )
            return completed
        except Exception as exc:
            latest = self.repository.get_experiment(experiment_id)
            if latest.status in {ExperimentStatus.CANCEL_REQUESTED, ExperimentStatus.CANCELLED}:
                if latest.status == ExperimentStatus.CANCEL_REQUESTED:
                    cancelled = latest.model_copy(
                        update={"status": ExperimentStatus.CANCELLED, "finished_at": utc_now()}
                    )
                    return self.repository.update_experiment(
                        cancelled,
                        expected_status=ExperimentStatus.CANCEL_REQUESTED,
                    )
                return latest
            failed_at = utc_now()
            failed_replay = {
                **dict(latest.summary.get("swebench_verifier_replay") or replay),
                "status": "failed",
                "completed_at": failed_at.isoformat(),
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
            failed = latest.model_copy(
                update={
                    "status": ExperimentStatus.FAILED,
                    "finished_at": failed_at,
                    "summary": {
                        **latest.summary,
                        "swebench_verifier_replay": failed_replay,
                        "progress": {
                            **dict(latest.summary.get("progress") or {}),
                            "stage": "failed",
                            "message": "Docker 重新判卷失败；Agent patch 已保留",
                            "updated_at": failed_at.isoformat(),
                        },
                    },
                    "error": EvalError(
                        code="swebench_verifier_replay_failed",
                        message=f"{type(exc).__name__}: {str(exc)[:1000]}",
                        retryable=isinstance(exc, (TimeoutError, ConnectionError)),
                    ),
                }
            )
            return self.repository.update_experiment(
                failed,
                expected_status=ExperimentStatus.RUNNING,
            )
        finally:
            if not experiment.execution.preserve_workspaces:
                shutil.rmtree(runtime_root, ignore_errors=True)

    async def run_swebench_missing_case_resume(self, experiment_id: str) -> EvalExperiment:
        """Append Attempts only for missing patches, then judge the merged set."""

        experiment = self.repository.get_experiment(experiment_id)
        resume = dict(experiment.summary.get("swebench_case_resume") or {})
        requested_instance_ids = {
            str(item) for item in resume.get("missing_instance_ids") or []
        }
        started_at = utc_now()
        running = experiment.model_copy(
            update={
                "status": ExperimentStatus.RUNNING,
                "started_at": started_at,
                "finished_at": None,
                "error": None,
                "summary": {
                    **experiment.summary,
                    "swebench_case_resume": {
                        **resume,
                        "status": "running",
                        "started_at": started_at.isoformat(),
                    },
                    "progress": {
                        "stage": "preparing",
                        "message": "正在准备补跑缺失 Patch 的 Case",
                        "total": len(requested_instance_ids),
                        "completed": 0,
                        "failed": 0,
                        "updated_at": started_at.isoformat(),
                    },
                },
            }
        )
        self.repository.update_experiment(
            running,
            expected_status=ExperimentStatus.QUEUED,
        )
        runtime_root = self._runtime_root(experiment_id)
        try:
            dataset = self.repository.get_dataset(
                running.dataset_id,
                running.dataset_version,
            )
            if dataset.current_version_id != running.dataset_version_id:
                raise ValueError("Pinned Dataset version identity mismatch")
            drifted = verify_candidate_snapshot(self.backend_dir, running.candidate)
            if drifted:
                raise ValueError(
                    f"Candidate snapshot changed before execution: {', '.join(drifted)}"
                )
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

            current_manifest = swebench_prediction_manifest(
                dataset,
                self.repository.load_run_envelopes(experiment_id),
                model_name_or_path=(running.candidate.llm_model_id or running.candidate.name),
            )
            still_missing = set(current_manifest["missing_instance_ids"])
            target_instances = requested_instance_ids & still_missing
            target_cases = [
                case
                for case in dataset.cases
                if case.enabled
                and case.code is not None
                and case.code.repository.swebench is not None
                and case.code.repository.swebench.instance_id in target_instances
            ]
            result_rows = self.repository.list_results(experiment_id)
            next_repetition_by_case: dict[str, int] = {}
            for row in result_rows:
                case_id = str(row["case_id"])
                next_repetition_by_case[case_id] = max(
                    next_repetition_by_case.get(case_id, 0),
                    int(row["repetition"]) + 1,
                )

            new_attempts: list[dict[str, Any]] = []
            for index, case in enumerate(target_cases, start=1):
                latest = self.repository.get_experiment(experiment_id)
                if latest.status == ExperimentStatus.CANCEL_REQUESTED:
                    self.repository.cancel_running_attempts(
                        experiment_id,
                        "Experiment cancellation requested",
                    )
                    cancelled = latest.model_copy(
                        update={
                            "status": ExperimentStatus.CANCELLED,
                            "finished_at": utc_now(),
                        }
                    )
                    return self.repository.update_experiment(
                        cancelled,
                        expected_status=ExperimentStatus.CANCEL_REQUESTED,
                    )
                self._update_progress(
                    experiment_id,
                    stage="agent_running",
                    message="只补跑缺失 Patch 的 Case",
                    total=len(target_cases),
                    completed=len(new_attempts),
                    failed=sum(
                        item.get("attempt_status") == "failed" for item in new_attempts
                    ),
                    current_index=index,
                    current_case_id=case.case_id,
                    current_case_name=case.name,
                )
                result = await self._run_case(
                    running,
                    case,
                    next_repetition_by_case.get(case.case_id, 0),
                    runtime_root,
                    dataset_data_classification,
                )
                new_attempts.append(result)

            effective_attempts, attempt_history, manifest = (
                self._effective_swebench_attempts_for_scoring(
                    running,
                    dataset,
                )
            )
            official: dict[str, Any]
            if manifest["predictions"]:
                try:
                    official = await self._run_and_apply_official_swebench(
                        running,
                        dataset,
                        runtime_root,
                        effective_attempts,
                        allow_partial_predictions=True,
                    )
                except Exception as exc:
                    official = {
                        "status": "error",
                        "reason": f"{type(exc).__name__}: {str(exc)[:1_000]}",
                        "total": len(manifest["predictions"]),
                        "resolved": 0,
                    }
            else:
                official = {
                    "status": "not_started",
                    "reason": "No persisted SWE-bench patch is available",
                    "total": 0,
                    "resolved": 0,
                }

            latest = self.repository.get_experiment(experiment_id)
            if latest.status == ExperimentStatus.CANCEL_REQUESTED:
                cancelled = latest.model_copy(
                    update={
                        "status": ExperimentStatus.CANCELLED,
                        "finished_at": utc_now(),
                    }
                )
                return self.repository.update_experiment(
                    cancelled,
                    expected_status=ExperimentStatus.CANCEL_REQUESTED,
                )
            # Official scoring may have updated persisted result rows. Reload
            # the effective set so aggregates exactly match durable evidence.
            effective_attempts, attempt_history, manifest = (
                self._effective_swebench_attempts_for_scoring(
                    latest,
                    dataset,
                )
            )
            summary = self._refresh_attempt_scoring_summary(
                latest.summary,
                effective_attempts,
            )
            completed_at = utc_now()
            resume_result = {
                **dict(summary.get("swebench_case_resume") or resume),
                "status": "completed",
                "completed_at": completed_at.isoformat(),
                "new_attempt_ids": [item["attempt_id"] for item in new_attempts],
                "new_attempt_count": len(new_attempts),
                "new_failed_attempt_count": sum(
                    item.get("attempt_status") == "failed" for item in new_attempts
                ),
                "remaining_missing_instance_ids": list(manifest["missing_instance_ids"]),
                "persisted_patch_count": len(manifest["predictions"]),
            }
            resume_history = list(summary.get("swebench_case_resume_history") or [])
            resume_history.append(resume_result)
            effective_attempt_ids = [
                str(item["attempt_id"]) for item in effective_attempts
            ]
            effective_attempt_id_set = set(effective_attempt_ids)
            superseded_attempt_ids = [
                str(item["attempt_id"])
                for item in attempt_history
                if str(item["attempt_id"]) not in effective_attempt_id_set
            ]
            summary.update(
                {
                    "swebench_case_resume": resume_result,
                    "swebench_case_resume_history": resume_history,
                    "swebench_official_harness": official,
                    "swebench_predictions_available": not manifest["missing_instance_ids"],
                    "swebench_predictions_count": len(manifest["predictions"]),
                    "swebench_missing_predictions": len(manifest["missing_instance_ids"]),
                    "attempt_history_count": len(attempt_history),
                    "effective_attempt_ids": effective_attempt_ids,
                    "superseded_attempt_ids": superseded_attempt_ids,
                    "superseded_attempt_count": len(superseded_attempt_ids),
                    "progress": {
                        "stage": "completed",
                        "message": (
                            "缺失 Case 已补跑并完成全部 Patch 判卷"
                            if not manifest["missing_instance_ids"]
                            else (
                                f"补跑结束，仍有 {len(manifest['missing_instance_ids'])} "
                                "个 Case 缺少 Patch；已有 Patch 已完成判卷"
                            )
                        ),
                        "total": len(target_cases),
                        "completed": len(new_attempts),
                        "failed": sum(
                            item.get("attempt_status") == "failed"
                            for item in new_attempts
                        ),
                        "updated_at": completed_at.isoformat(),
                    },
                }
            )
            projection_was_published = bool(
                resume.get("langsmith_projection_was_published")
            )
            if projection_was_published:
                summary["experiment_projection"] = "stale_after_case_resume"
                summary["langsmith_projection"] = "stale_after_case_resume"
            completed = latest.model_copy(
                update={
                    "status": ExperimentStatus.COMPLETED,
                    "verdict": self._verdict_from_summary(summary),
                    "finished_at": completed_at,
                    "summary": summary,
                    "error": None,
                }
            )
            return self.repository.update_experiment(
                completed,
                expected_status=ExperimentStatus.RUNNING,
            )
        except Exception as exc:
            latest = self.repository.get_experiment(experiment_id)
            if latest.status in {
                ExperimentStatus.CANCEL_REQUESTED,
                ExperimentStatus.CANCELLED,
            }:
                if latest.status == ExperimentStatus.CANCEL_REQUESTED:
                    latest = latest.model_copy(
                        update={
                            "status": ExperimentStatus.CANCELLED,
                            "finished_at": utc_now(),
                        }
                    )
                    return self.repository.update_experiment(
                        latest,
                        expected_status=ExperimentStatus.CANCEL_REQUESTED,
                    )
                return latest
            failed_at = utc_now()
            failed = latest.model_copy(
                update={
                    "status": ExperimentStatus.FAILED,
                    "finished_at": failed_at,
                    "summary": {
                        **latest.summary,
                        "swebench_case_resume": {
                            **dict(latest.summary.get("swebench_case_resume") or resume),
                            "status": "failed",
                            "completed_at": failed_at.isoformat(),
                            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                        },
                        "progress": {
                            **dict(latest.summary.get("progress") or {}),
                            "stage": "failed",
                            "message": "缺失 Case 补跑失败；已有 Patch 和 Attempt 均已保留",
                            "updated_at": failed_at.isoformat(),
                        },
                    },
                    "error": EvalError(
                        code="swebench_case_resume_failed",
                        message=f"{type(exc).__name__}: {str(exc)[:1000]}",
                        retryable=isinstance(exc, (TimeoutError, ConnectionError)),
                    ),
                }
            )
            return self.repository.update_experiment(
                failed,
                expected_status=ExperimentStatus.RUNNING,
            )
        finally:
            if not experiment.execution.preserve_workspaces:
                shutil.rmtree(runtime_root, ignore_errors=True)

    async def run(self, experiment_id: str) -> EvalExperiment:
        experiment = self.repository.get_experiment(experiment_id)
        if experiment.summary.get("execution_mode") == "official_verifier_replay":
            return await self.run_official_verifier_replay(experiment_id)
        if experiment.summary.get("execution_mode") == "swebench_missing_case_resume":
            return await self.run_swebench_missing_case_resume(experiment_id)
        experiment = experiment.model_copy(
            update={
                "status": ExperimentStatus.RUNNING,
                "started_at": utc_now(),
                "summary": {
                    **experiment.summary,
                    "progress": {
                        **dict(experiment.summary.get("progress") or {}),
                        "stage": "preparing",
                        "message": "正在初始化隔离 Worker 和 Workspace",
                        "completed": 0,
                        "failed": 0,
                        "updated_at": utc_now().isoformat(),
                    },
                },
            }
        )
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
            dataset_data_classification = str(dataset.metadata.get("data_classification") or "internal").lower()
            if dataset_data_classification not in {
                "public",
                "internal",
                "sensitive",
                "restricted",
            }:
                dataset_data_classification = "restricted"
            outputs: dict[str, list[dict[str, Any]]] = defaultdict(list)
            all_attempts: list[dict[str, Any]] = []
            enabled_cases = [case for case in dataset.cases if case.enabled]
            total_attempts = len(enabled_cases) * experiment.execution.repetitions
            self._update_progress(
                experiment_id,
                stage="preparing",
                message="Dataset 已加载，正在准备第一个 Case",
                total=total_attempts,
                completed=0,
                failed=0,
            )
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
                    self._update_progress(
                        experiment_id,
                        stage="agent_running",
                        message="Agent 正在处理 Case",
                        total=total_attempts,
                        completed=len(all_attempts),
                        failed=sum(item.get("attempt_status") == "failed" for item in all_attempts),
                        current_index=len(all_attempts) + 1,
                        current_case_id=case.case_id,
                        current_case_name=case.name,
                        current_repetition=repetition + 1,
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
                    self._update_progress(
                        experiment_id,
                        stage="case_completed",
                        message="Case 执行完成，正在准备下一项",
                        total=total_attempts,
                        completed=len(all_attempts),
                        failed=sum(item.get("attempt_status") == "failed" for item in all_attempts),
                        current_index=len(all_attempts),
                        current_case_id=case.case_id,
                        current_case_name=case.name,
                        current_repetition=repetition + 1,
                    )

            swebench_official_harness: dict[str, Any] | None = None
            if any(
                case.enabled and case.code is not None and case.code.repository.kind == "swebench"
                for case in dataset.cases
            ):
                try:
                    swebench_official_harness = await self._run_and_apply_official_swebench(
                        experiment,
                        dataset,
                        runtime_root,
                        all_attempts,
                    )
                except Exception as exc:
                    swebench_official_harness = {
                        "status": "error",
                        "reason": f"{type(exc).__name__}: {str(exc)[:1_000]}",
                    }

            self._update_progress(
                experiment_id,
                stage="scoring",
                message="正在汇总七维评分和执行证据",
                total=total_attempts,
                completed=len(all_attempts),
                failed=sum(item.get("attempt_status") == "failed" for item in all_attempts),
            )

            summary = {
                "case_attempts": len(all_attempts),
                "completed_attempts": sum(item.get("attempt_status") == "completed" for item in all_attempts),
                "failed_attempts": sum(item.get("attempt_status") == "failed" for item in all_attempts),
                "cancelled_attempts": sum(item.get("attempt_status") == "cancelled" for item in all_attempts),
                "determinate": sum(item["summary"].get("verdict") != "indeterminate" for item in all_attempts),
                "passed": sum(item["summary"].get("verdict") == "pass" for item in all_attempts),
                "failed": sum(item["summary"].get("verdict") == "fail" for item in all_attempts),
                "critical_failures": sum(bool(item["summary"].get("critical_failure")) for item in all_attempts),
                "indeterminate": sum(item["summary"].get("verdict") == "indeterminate" for item in all_attempts),
                "effective_max_concurrency": 1,
                "requested_max_concurrency": experiment.execution.max_concurrency,
                "progress": {
                    "stage": "scoring",
                    "message": "正在汇总七维评分和执行证据",
                    "total": total_attempts,
                    "completed": len(all_attempts),
                    "failed": sum(item.get("attempt_status") == "failed" for item in all_attempts),
                    "updated_at": utc_now().isoformat(),
                },
            }
            if swebench_official_harness is not None:
                summary["swebench_official_harness"] = swebench_official_harness
            if any(
                case.code is not None and case.code.repository.kind == "swebench" and case.enabled
                for case in dataset.cases
            ):
                swebench_manifest = swebench_prediction_manifest(
                    dataset,
                    self.repository.load_run_envelopes(experiment_id),
                    model_name_or_path=(experiment.candidate.llm_model_id or experiment.candidate.name),
                )
                summary["swebench_predictions_available"] = (
                    bool(swebench_manifest["predictions"]) and not swebench_manifest["missing_instance_ids"]
                )
                summary["swebench_missing_predictions"] = len(swebench_manifest["missing_instance_ids"])
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
                    bucket["evaluator_versions"].add(f"{result.get('evaluator_id')}@{result.get('evaluator_version')}")
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
            summary["applicable_count"] = sum(item["applicable_count"] for item in dimensions.values())
            expected_metrics = sum(item["sample_count"] - item["not_applicable_count"] for item in dimensions.values())
            summary["coverage"] = summary["applicable_count"] / expected_metrics if expected_metrics else None
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
                case.data_classification in {"sensitive", "restricted"} for case in dataset.cases if case.enabled
            ):
                summary["dataset_projection"] = "blocked_by_data_policy"
                summary["experiment_projection"] = "blocked_by_data_policy"
            else:
                self._update_progress(
                    experiment_id,
                    stage="langsmith_projection",
                    message="本地评分已完成，正在投影到 LangSmith",
                    total=total_attempts,
                    completed=len(all_attempts),
                )
                try:
                    bundle = self.repository.export_bundle(experiment.dataset_id, experiment.dataset_version)
                    mapping = LangSmithDatasetAdapter(self.repository, self.settings).sync_dataset(bundle)
                except Exception as exc:
                    projection_error = str(_redact(f"{type(exc).__name__}: {str(exc)[:1000]}"))
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
                        projection_error = str(_redact(f"{type(exc).__name__}: {str(exc)[:1000]}"))
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
                if "pending" in {summary["dataset_projection"], summary["experiment_projection"]}
                else summary["experiment_projection"]
            )
            latest = self.repository.get_experiment(experiment_id)
            if latest.status == ExperimentStatus.CANCEL_REQUESTED:
                self.repository.cancel_running_attempts(experiment_id, "Experiment cancellation requested")
                cancelled = latest.model_copy(update={"status": ExperimentStatus.CANCELLED, "finished_at": utc_now()})
                return self.repository.update_experiment(cancelled, expected_status=ExperimentStatus.CANCEL_REQUESTED)
            completed = latest.model_copy(
                update={
                    "status": ExperimentStatus.COMPLETED,
                    "verdict": (
                        "fail"
                        if summary["failed"] or summary["critical_failures"] or summary["failed_attempts"]
                        else "indeterminate"
                        if summary["indeterminate"]
                        else "pass"
                    ),
                    "finished_at": utc_now(),
                    "remote_experiment_id": remote_id,
                    "remote_url": remote_url,
                    "summary": {
                        **summary,
                        "progress": {
                            **dict(summary.get("progress") or {}),
                            "stage": "completed",
                            "message": "评测完成",
                            "total": total_attempts,
                            "completed": len(all_attempts),
                            "updated_at": utc_now().isoformat(),
                        },
                    },
                }
            )
            return self.repository.update_experiment(completed, expected_status=ExperimentStatus.RUNNING)
        except Exception as exc:
            latest = self.repository.get_experiment(experiment_id)
            if latest.status in {ExperimentStatus.CANCEL_REQUESTED, ExperimentStatus.CANCELLED}:
                if latest.status == ExperimentStatus.CANCEL_REQUESTED:
                    latest = latest.model_copy(update={"status": ExperimentStatus.CANCELLED, "finished_at": utc_now()})
                    return self.repository.update_experiment(latest, expected_status=ExperimentStatus.CANCEL_REQUESTED)
                return latest
            failed = latest.model_copy(
                update={
                    "status": ExperimentStatus.FAILED,
                    "finished_at": utc_now(),
                    "summary": {
                        **latest.summary,
                        "progress": {
                            **dict(latest.summary.get("progress") or {}),
                            "stage": "failed",
                            "message": "评测执行失败",
                            "updated_at": utc_now().isoformat(),
                        },
                    },
                    "error": EvalError(
                        code="experiment_failed",
                        message=f"{type(exc).__name__}: {str(exc)[:1000]}",
                        retryable=isinstance(exc, (TimeoutError, ConnectionError)),
                    ),
                }
            )
            return self.repository.update_experiment(failed, expected_status=ExperimentStatus.RUNNING)
        finally:
            if not experiment.execution.preserve_workspaces:
                shutil.rmtree(runtime_root, ignore_errors=True)
