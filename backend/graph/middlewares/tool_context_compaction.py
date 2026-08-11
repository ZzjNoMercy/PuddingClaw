"""DeepAgents-only Tool Context compaction."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse, ToolCallRequest
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import Command

from graph.session_manager import SessionManager, session_manager

logger = logging.getLogger(__name__)
POLICY_VERSION = "tool-context-v2-harness-control"
PROJECTION_VERSION = "evidence-projection-v1"
DEFAULT_CONTEXT_PROFILE = "detailed"
LLM_RESULT_INPUT_MAX_CHARS = 24000
LLM_RESULT_OUTPUT_MAX_CHARS = 4000
LLM_BATCH_TIMEOUT_MAX_SECONDS = 30
RAW_OUTPUT_ARTIFACT_KEY = "puddingclaw_raw_tool_output"
CONTEXT_OUTPUT_ARTIFACT_KEY = "puddingclaw_context_output"
CONTEXT_METHOD_ARTIFACT_KEY = "puddingclaw_context_method"
CONTEXT_POLICY_ARTIFACT_KEY = "puddingclaw_context_policy"
HARNESS_CONTROL_TOOLS = frozenset(
    {
        "create_goal",
        "get_goal",
        "update_goal",
        "update_todos",
        "stage_external_artifact",
        "commit_external_artifact",
        "stage_external_directory",
        "prepare_external_directory_commit",
        "commit_external_directory",
        "inspect_file_version",
        "copy_file",
        "materialize_source_ref",
        "replace_file",
        "patch_file",
    }
)
LARGE_TOOL_RESULT_OFFLOAD_TOKENS = 20_000
LARGE_TOOL_RESULT_ARTIFACT_KEY = "puddingclaw_large_tool_result"


@dataclass(frozen=True)
class ToolContextConfig:
    enabled: bool = True
    immediate_compaction_enabled: bool = False
    single_tool_trigger_tokens: int = 8000
    background_min_result_tokens: int = 1000
    retain_tool_context_tokens: int = 32000
    batch_size: int = 6
    max_concurrency: int = 4
    job_timeout_seconds: int = 120
    max_candidates_per_job: int = 48

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> ToolContextConfig:
        raw = value if isinstance(value, dict) else {}

        def positive(name: str, default: int) -> int:
            try:
                parsed = int(raw.get(name, default))
            except (TypeError, ValueError):
                return default
            return parsed if parsed > 0 else default

        return cls(
            enabled=bool(raw.get("enabled", True)),
            immediate_compaction_enabled=bool(
                raw.get("immediate_compaction_enabled", False)
            ),
            single_tool_trigger_tokens=positive("single_tool_trigger_tokens", 8000),
            background_min_result_tokens=positive("background_min_result_tokens", 1000),
            retain_tool_context_tokens=positive("retain_tool_context_tokens", 32000),
            batch_size=min(8, positive("batch_size", 6)),
            max_concurrency=min(4, positive("max_concurrency", 4)),
            job_timeout_seconds=positive("job_timeout_seconds", 120),
            max_candidates_per_job=positive("max_candidates_per_job", 48),
        )


def estimate_text_tokens(text: str) -> int:
    return SessionManager._tool_context_tokens(str(text or ""))


def _head_tail(text: str, *, max_chars: int, label: str) -> str:
    if len(text) <= max_chars:
        return text
    head_size = max_chars * 2 // 3
    tail_size = max_chars - head_size
    omitted = len(text) - head_size - tail_size
    return (
        f"[Tool Context：{label}，原文保留在 raw_output_ref]\n"
        f"{text[:head_size]}\n"
        f"\n... [省略 {omitted} 字符] ...\n"
        f"{text[-tail_size:]}"
    )


def _looks_error(text: str) -> bool:
    lowered = text.lstrip().lower()
    return lowered.startswith(("error:", "exception:", "traceback")) or any(
        marker in lowered
        for marker in ("undefinedcolumnerror", "permission denied", "timed out")
    )


def compact_immediate_tool_output(text: str, *, tool_name: str = "") -> tuple[str, str]:
    name = (tool_name or "unknown_tool").lower()
    if _looks_error(text):
        return _head_tail(text, max_chars=7000, label="错误结果高保真裁剪"), "immediate_error"
    return _head_tail(text, max_chars=7000, label=f"{name} 单条超限裁剪"), "immediate_head_tail"


def _with_candidate_metadata(candidate: dict[str, Any], summary: str) -> str:
    tool_input = candidate.get("input") or ""
    if isinstance(tool_input, (dict, list)):
        serialized_input = json.dumps(
            tool_input,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
        input_value: Any = tool_input if len(serialized_input) <= 1200 else serialized_input[:1200]
    else:
        input_value = str(tool_input)[:1200]
    metadata = {
        "tool_call_id": str(candidate.get("tool_call_id") or ""),
        "tool": str(candidate.get("tool") or "unknown_tool"),
        "input": input_value,
        "source_hash": candidate.get("source_hash"),
        "raw_output_ref": candidate.get("raw_output_ref"),
        "harness_evidence": {
            "identity_preserved": True,
            "raw_output_recoverable": bool(candidate.get("raw_output_ref")),
        },
    }
    return (
        "[Tool Context 元数据]\n"
        f"{json.dumps(metadata, ensure_ascii=False, separators=(',', ':'))}\n"
        f"{summary}"
    )


def _deterministic_background_compaction(candidate: dict[str, Any]) -> tuple[str, str] | None:
    tool_name = str(candidate.get("tool") or "unknown_tool").lower().replace("-", "_")
    output = str(candidate.get("output") or "")
    if bool(candidate.get("is_error")) or _looks_error(output):
        return _with_candidate_metadata(
            candidate, _head_tail(output, max_chars=3200, label="错误结果高保真摘要")
        ), "error_adapter"
    # Harness control-plane results are evidence, not prose.  In particular,
    # an LLM must never turn "Todo status=completed" into the stronger claim
    # "verification passed", or rewrite lease IDs / CAS hashes.  Keep these
    # outputs byte-for-byte inside the model context; raw_output_ref remains the
    # recovery source and the authoritative Session state is injected separately.
    if tool_name in HARNESS_CONTROL_TOOLS:
        if tool_name == "inspect_file_version":
            header, separator, content = output.partition("\ncontent:\n")
            if separator:
                compact_content = _head_tail(
                    content,
                    max_chars=2200,
                    label="文件正文摘要；版本头保持原值",
                )
                return _with_candidate_metadata(
                    candidate,
                    f"{header}{separator}{compact_content}",
                ), "versioned_file_adapter"
        return _with_candidate_metadata(candidate, output), "harness_control_passthrough"
    if tool_name in {"read_file", "read_resource", "read_external_file"}:
        return _with_candidate_metadata(
            candidate, _head_tail(output, max_chars=2200, label="文件读取摘要")
        ), "file_adapter"
    if tool_name in {"terminal", "execute", "python_repl"}:
        return _with_candidate_metadata(
            candidate, _head_tail(output, max_chars=2200, label="终端输出摘要")
        ), "terminal_adapter"
    if tool_name in {"grep", "glob", "ls", "search", "web_search"} or "search" in tool_name:
        return _with_candidate_metadata(
            candidate, _head_tail(output, max_chars=1800, label="搜索结果摘要")
        ), "search_adapter"
    if tool_name.startswith("database_") or tool_name.startswith("sql_") or "sql" in tool_name:
        return _with_candidate_metadata(
            candidate, _head_tail(output, max_chars=2500, label="数据库/SQL 结果摘要")
        ), "database_adapter"
    stripped = output.strip()
    if stripped.startswith(("{", "[")):
        try:
            payload = json.loads(stripped)
            compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            return _with_candidate_metadata(
                candidate, _head_tail(compact, max_chars=2500, label="结构化 JSON 摘要")
            ), "json_adapter"
        except json.JSONDecodeError:
            pass
    return None


def _summary_prompt(candidates: list[dict[str, Any]]) -> str:
    items = [
        {
            "tool_call_id": item["tool_call_id"],
            "tool": item.get("tool"),
            "input": str(item.get("input") or "")[:1000],
            "raw_output_ref": item.get("raw_output_ref"),
            "output": _head_tail(
                str(item.get("output") or ""),
                max_chars=LLM_RESULT_INPUT_MAX_CHARS,
                label="LLM 摘要输入预算裁剪",
            ),
        }
        for item in candidates
    ]
    return (
        "<role>Tool Context Compression Assistant</role>\n"
        "将以下历史工具结果压缩为供后续模型使用的中文上下文。"
        "必须保留文件路径、ID、SQL、数字、错误、已完成动作和后续读取方式；不得编造。"
        "只返回 JSON 对象，key 必须逐字使用输入 tool_call_id，value 为摘要字符串。\n"
        f"<tool_results>{json.dumps(items, ensure_ascii=False)}</tool_results>"
    )


def _parse_summary_mapping(content: Any, expected_ids: set[str]) -> dict[str, str]:
    text = str(content or "").strip()
    fence = chr(96) * 3
    if text.startswith(fence):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if len(lines) > 2 else lines)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        key: str(value).strip()
        for key, value in payload.items()
        if key in expected_ids and str(value).strip()
    }


class ToolContextCompactionService:
    """Process-local scheduler with persisted, idempotent Session state."""

    def __init__(
        self,
        *,
        manager: SessionManager = session_manager,
        model_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.manager = manager
        self.model_factory = model_factory
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def enqueue(self, session_id: str, cfg: ToolContextConfig) -> str | None:
        if not cfg.enabled:
            return None
        candidates = await asyncio.to_thread(
            self.manager.select_tool_context_candidates,
            session_id,
            min_result_tokens=cfg.background_min_result_tokens,
            retain_tokens=cfg.retain_tool_context_tokens,
            policy_version=POLICY_VERSION,
            context_profile=DEFAULT_CONTEXT_PROFILE,
        )
        if not candidates:
            return None
        candidates = candidates[: cfg.max_candidates_per_job]
        active = self._tasks.get(session_id)
        if active is not None and not active.done():
            return None
        job_id = f"toolctx-{uuid.uuid4().hex[:16]}"
        began = await asyncio.to_thread(
            self.manager.begin_tool_context_job,
            session_id,
            job_id=job_id,
            candidates=candidates,
            policy_version=POLICY_VERSION,
            lease_timeout_seconds=max(300, cfg.job_timeout_seconds * 2),
        )
        if not began:
            return None
        queued_at = time.monotonic()
        task = asyncio.create_task(
            self._run_job(session_id, job_id, candidates, cfg, queued_at=queued_at)
        )
        self._tasks[session_id] = task

        def remove_finished(finished: asyncio.Task[None], sid: str = session_id) -> None:
            if self._tasks.get(sid) is finished:
                self._tasks.pop(sid, None)

        task.add_done_callback(remove_finished)
        return job_id

    async def wait(self, session_id: str, timeout: float = 5) -> dict[str, Any]:
        task = self._tasks.get(session_id)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                status = await asyncio.to_thread(
                    self.manager.get_tool_context_status,
                    session_id,
                )
                job_id = str(status.get("id") or "")
                if job_id:
                    await asyncio.to_thread(
                        self.manager.fail_unresolved_tool_context_candidates,
                        session_id,
                        job_id,
                        reason="Background Tool Context task was cancelled; raw result retained.",
                    )
                    await asyncio.to_thread(
                        self.manager.update_tool_context_job,
                        session_id,
                        job_id,
                        status="failed",
                        error="Background Tool Context task was cancelled.",
                    )
        return await asyncio.to_thread(self.manager.get_tool_context_status, session_id)

    def _model(self) -> Any:
        if self.model_factory is not None:
            return self.model_factory()
        from llm.model_client import ModelClient

        return ModelClient(role="summary", streaming=False).get_chat_model()

    async def _llm_batch(self, candidates: list[dict[str, Any]]) -> dict[str, str]:
        response = await self._model().ainvoke([HumanMessage(content=_summary_prompt(candidates))])
        return _parse_summary_mapping(
            getattr(response, "content", response),
            {str(item["tool_call_id"]) for item in candidates},
        )

    async def _run_job(
        self,
        session_id: str,
        job_id: str,
        candidates: list[dict[str, Any]],
        cfg: ToolContextConfig,
        *,
        queued_at: float | None = None,
    ) -> None:
        started = time.monotonic()
        await asyncio.to_thread(
            self.manager.update_tool_context_job, session_id, job_id, status="running"
        )
        completed = failed = deterministic_count = llm_count = after_tokens = 0
        before_tokens = sum(int(item.get("estimated_tokens") or 0) for item in candidates)
        try:
            deterministic: list[tuple[dict[str, Any], str, str]] = []
            unstructured: list[dict[str, Any]] = []
            for candidate in candidates:
                result = _deterministic_background_compaction(candidate)
                if result is None:
                    unstructured.append(candidate)
                else:
                    deterministic.append((candidate, result[0], result[1]))

            for candidate, summary, method in deterministic:
                ok = await asyncio.to_thread(
                    self.manager.complete_tool_context_compaction,
                    session_id,
                    job_id=job_id,
                    tool_call_id=str(candidate["tool_call_id"]),
                    source_hash=str(candidate["source_hash"]),
                    policy_version=POLICY_VERSION,
                    context_output=summary,
                    method=method,
                    context_profile=str(candidate.get("context_profile") or DEFAULT_CONTEXT_PROFILE),
                )
                if ok:
                    completed += 1
                    deterministic_count += 1
                    after_tokens += estimate_text_tokens(summary)
                else:
                    failed += 1

            semaphore = asyncio.Semaphore(cfg.max_concurrency)
            processed_unstructured = 0

            async def summarize_batch(batch: list[dict[str, Any]]) -> None:
                nonlocal completed, failed, llm_count, after_tokens, processed_unstructured
                async with semaphore:
                    try:
                        mapping = await asyncio.wait_for(
                            self._llm_batch(batch),
                            timeout=max(
                                1.0,
                                min(
                                    float(LLM_BATCH_TIMEOUT_MAX_SECONDS),
                                    float(cfg.job_timeout_seconds) / 2,
                                ),
                            ),
                        )
                    except Exception:
                        logger.warning("Tool Context LLM batch failed", exc_info=True)
                        mapping = {}
                    for candidate in batch:
                        tool_call_id = str(candidate["tool_call_id"])
                        summary = mapping.get(tool_call_id)
                        method = "llm_summary"
                        if summary:
                            summary = _head_tail(
                                summary,
                                max_chars=LLM_RESULT_OUTPUT_MAX_CHARS,
                                label="LLM 摘要输出预算裁剪",
                            )
                            summary = _with_candidate_metadata(candidate, summary)
                        if not summary or estimate_text_tokens(summary) >= int(
                            candidate.get("estimated_tokens") or 0
                        ):
                            summary = _head_tail(
                                str(candidate.get("output") or ""),
                                max_chars=2200,
                                label="非结构化结果降级裁剪",
                            )
                            summary = _with_candidate_metadata(candidate, summary)
                            method = "fallback_head_tail"
                        ok = await asyncio.to_thread(
                            self.manager.complete_tool_context_compaction,
                            session_id,
                            job_id=job_id,
                            tool_call_id=tool_call_id,
                            source_hash=str(candidate["source_hash"]),
                            policy_version=POLICY_VERSION,
                            context_output=summary,
                            method=method,
                            context_profile=str(
                                candidate.get("context_profile") or DEFAULT_CONTEXT_PROFILE
                            ),
                        )
                        if ok:
                            completed += 1
                            llm_count += int(method == "llm_summary")
                            after_tokens += estimate_text_tokens(summary)
                        else:
                            failed += 1
                        processed_unstructured += 1

            batches = [
                unstructured[index : index + cfg.batch_size]
                for index in range(0, len(unstructured), cfg.batch_size)
            ]
            if batches:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*(summarize_batch(batch) for batch in batches)),
                        timeout=cfg.job_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    remaining = max(0, len(unstructured) - processed_unstructured)
                    failed += remaining
                    logger.warning(
                        "Tool Context job=%s reached %ss budget with %d result(s) pending",
                        job_id,
                        cfg.job_timeout_seconds,
                        remaining,
                    )

            metrics = {
                "tool_context_tokens_before": before_tokens,
                "tool_context_tokens_after": after_tokens,
                "selected_tool_count": len(candidates),
                "deterministic_compaction_count": deterministic_count,
                "llm_summary_count": llm_count,
                "compaction_failure_count": failed,
                "compaction_job_queue_delay_ms": round(
                    max(0.0, started - (queued_at if queued_at is not None else started)) * 1000
                ),
                "compaction_cache_hit_count": int(
                    candidates[0].get("scan_cache_hit_count") or 0
                ) if candidates else 0,
                "raw_output_ref_missing_count": sum(
                    1 for item in candidates if not item.get("raw_output_ref")
                ),
                "tool_call_id_integrity_failure_count": failed,
                "compaction_job_duration_ms": round((time.monotonic() - started) * 1000),
            }
            if failed:
                await asyncio.to_thread(
                    self.manager.fail_unresolved_tool_context_candidates,
                    session_id,
                    job_id,
                    reason="Tool Context compaction failed; raw result retained.",
                )
            await asyncio.to_thread(
                self.manager.update_tool_context_job,
                session_id,
                job_id,
                status="completed" if failed == 0 else "completed_with_errors",
                completed_count=completed,
                failed_count=failed,
                metrics=metrics,
            )
            final_status = await asyncio.to_thread(
                self.manager.get_tool_context_status, session_id
            )
            logger.info(
                "[ToolContext] session=%s job=%s policy=%s revision=%s selected=%d completed=%d failed=%d before=%d after=%d",
                session_id,
                job_id,
                POLICY_VERSION,
                final_status.get("revision"),
                len(candidates),
                completed,
                failed,
                before_tokens,
                after_tokens,
            )
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.manager.fail_unresolved_tool_context_candidates,
                session_id,
                job_id,
                reason="Tool Context compaction was cancelled; raw result retained.",
            )
            await asyncio.to_thread(
                self.manager.update_tool_context_job,
                session_id,
                job_id,
                status="failed",
                completed_count=completed,
                failed_count=max(1, failed),
                error="CancelledError: background Tool Context task was cancelled.",
            )
            raise
        except Exception as exc:
            logger.exception("Tool Context job failed for session=%s", session_id)
            await asyncio.to_thread(
                self.manager.fail_unresolved_tool_context_candidates,
                session_id,
                job_id,
                reason=f"{type(exc).__name__}: {exc}; raw result retained.",
            )
            await asyncio.to_thread(
                self.manager.update_tool_context_job,
                session_id,
                job_id,
                status="failed",
                completed_count=completed,
                failed_count=failed + 1,
                error=f"{type(exc).__name__}: {exc}",
            )


tool_context_compaction_service = ToolContextCompactionService()


class LargeToolResultOffloadMiddleware(AgentMiddleware[Any, Any, Any]):
    """Persist every oversized textual Tool Result before it reaches model context."""

    def __init__(
        self,
        *,
        manager: SessionManager = session_manager,
        workspace_path: str | Path | None = None,
        session_id: str = "",
        query_id: str = "",
    ) -> None:
        super().__init__()
        self.manager = manager
        self.workspace_path = str(workspace_path or "")
        self.session_id = session_id
        self.query_id = query_id

    @staticmethod
    def _pointer(raw: str, ref: dict[str, Any]) -> str:
        preview = _head_tail(raw, max_chars=4000, label="超大 Tool Result 预览")
        return (
            "[PuddingClaw：超大 Tool Result 已无损落盘]\n"
            f"完整结果： {ref['virtual_path']}\n"
            f"原始大小：约 {ref['estimated_tokens']:,} tokens（{ref['original_chars']:,} 字符）。\n"
            "需要精确内容时请分段调用 read_file，或使用 grep 搜索该路径。\n\n"
            f"{preview}"
        )

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        result = await handler(request)
        if not isinstance(result, ToolMessage) or not isinstance(result.content, str):
            return result
        raw = result.content
        if estimate_text_tokens(raw) <= LARGE_TOOL_RESULT_OFFLOAD_TOKENS:
            return result

        runtime_context = getattr(getattr(request, "runtime", None), "context", None)
        context = runtime_context if isinstance(runtime_context, dict) else {}
        workspace_path = str(context.get("workspace_path") or self.workspace_path)
        session_id = str(context.get("session_id") or self.session_id)
        query_id = str(context.get("query_id") or self.query_id)
        tool_call_id = str(request.tool_call.get("id") or result.tool_call_id or "")
        if not workspace_path or not session_id or not query_id or not tool_call_id:
            logger.warning(
                "Large Tool Result could not be offloaded because its owner binding is incomplete: "
                "tool=%s session=%s query=%s",
                request.tool_call.get("name"),
                session_id,
                query_id,
            )
            return result

        try:
            ref = await asyncio.to_thread(
                self.manager.materialize_large_tool_result,
                workspace_path=workspace_path,
                session_id=session_id,
                query_id=query_id,
                tool_call_id=tool_call_id,
                output=raw,
            )
        except (OSError, ValueError):
            logger.warning(
                "Failed to offload oversized Tool Result: tool=%s session=%s query=%s",
                request.tool_call.get("name"),
                session_id,
                query_id,
                exc_info=True,
            )
            # Preserve the raw ToolMessage so the outer DeepAgents middleware
            # can still attempt its own eviction for non-excluded tools.
            return result

        extra = dict(result.additional_kwargs or {})
        extra.update(
            {
                "puddingclaw_query_id": query_id,
                "puddingclaw_tool_source_hash": str(ref["source_hash"]),
            }
        )
        artifact = dict(result.artifact) if isinstance(result.artifact, dict) else {}
        artifact[LARGE_TOOL_RESULT_ARTIFACT_KEY] = ref
        return result.model_copy(
            update={
                "content": self._pointer(raw, ref),
                "additional_kwargs": extra,
                "artifact": artifact,
            }
        )


class ToolContextCompactionMiddleware(AgentMiddleware[Any, Any, Any]):
    """Registered only while DeepAgents Tool Context is enabled."""

    def __init__(
        self,
        cfg: ToolContextConfig,
        *,
        manager: SessionManager = session_manager,
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.manager = manager

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        result = await handler(request)
        if not isinstance(result, ToolMessage):
            return result
        raw = str(result.content or "")
        runtime_context = getattr(getattr(request, "runtime", None), "context", None)
        context = runtime_context if isinstance(runtime_context, dict) else {}
        tagged_kwargs = dict(result.additional_kwargs or {})
        tagged_kwargs.update(
            {
                "puddingclaw_query_id": str(context.get("query_id") or ""),
                "puddingclaw_tool_source_hash": self.manager._tool_context_source_hash(raw),
            }
        )
        result = result.model_copy(update={"additional_kwargs": tagged_kwargs})
        if not self.cfg.immediate_compaction_enabled:
            return result
        if estimate_text_tokens(raw) > LARGE_TOOL_RESULT_OFFLOAD_TOKENS:
            return result
        if estimate_text_tokens(raw) <= self.cfg.single_tool_trigger_tokens:
            return result
        compacted, method = compact_immediate_tool_output(
            raw,
            tool_name=str(request.tool_call.get("name") or result.name or "unknown_tool"),
        )
        artifact = result.artifact
        if isinstance(artifact, dict):
            artifact_payload = dict(artifact)
        elif artifact is None:
            artifact_payload = {}
        else:
            artifact_payload = {"original_artifact": artifact}
        artifact_payload.update(
            {
                RAW_OUTPUT_ARTIFACT_KEY: raw,
                CONTEXT_OUTPUT_ARTIFACT_KEY: compacted,
                CONTEXT_METHOD_ARTIFACT_KEY: method,
                CONTEXT_POLICY_ARTIFACT_KEY: POLICY_VERSION,
            }
        )
        return result.model_copy(update={"content": compacted, "artifact": artifact_payload})

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        context = request.runtime.context if request.runtime is not None else {}
        session_id = str(context.get("session_id") or "") if isinstance(context, dict) else ""
        if not session_id:
            return await handler(request)
        ready = await asyncio.to_thread(self.manager.get_ready_tool_context_entries, session_id)
        if not ready:
            return await handler(request)
        messages: list[Any] = []
        for message in request.messages:
            if isinstance(message, ToolMessage) and message.tool_call_id in ready:
                extra = dict(message.additional_kwargs or {})
                source_hash = str(extra.get("puddingclaw_tool_source_hash") or "")
                if not source_hash:
                    artifact = message.artifact if isinstance(message.artifact, dict) else {}
                    raw = str(artifact.get(RAW_OUTPUT_ARTIFACT_KEY) or message.content or "")
                    source_hash = self.manager._tool_context_source_hash(raw)
                query_id = str(extra.get("puddingclaw_query_id") or "")
                matches = [
                    entry
                    for entry in ready[message.tool_call_id]
                    if entry.get("source_hash") == source_hash
                    and (not query_id or entry.get("query_id") == query_id)
                ]
                if len(matches) == 1:
                    messages.append(message.model_copy(update={"content": matches[0]["context_output"]}))
                    continue
                messages.append(message)
            else:
                messages.append(message)
        return await handler(request.override(messages=messages))

    async def aafter_agent(
        self,
        state: Any,
        runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        return {"tool_context_enqueue": True}
