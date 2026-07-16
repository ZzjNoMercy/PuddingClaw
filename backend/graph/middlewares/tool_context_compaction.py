"""DeepAgents-only Tool Context compaction."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse, ToolCallRequest
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import Command

from graph.session_manager import SessionManager, session_manager

logger = logging.getLogger(__name__)
POLICY_VERSION = "tool-context-v1"
LLM_RESULT_INPUT_MAX_CHARS = 24000
LLM_RESULT_OUTPUT_MAX_CHARS = 4000
LLM_BATCH_TIMEOUT_MAX_SECONDS = 30
RAW_OUTPUT_ARTIFACT_KEY = "puddingclaw_raw_tool_output"
CONTEXT_OUTPUT_ARTIFACT_KEY = "puddingclaw_context_output"
CONTEXT_METHOD_ARTIFACT_KEY = "puddingclaw_context_method"
CONTEXT_POLICY_ARTIFACT_KEY = "puddingclaw_context_policy"
# Keep this boundary aligned with DeepAgents FilesystemMiddleware defaults:
# 20,000 tokens at its fixed approximation of four characters per token.
# Results above this character boundary must pass through unchanged so the
# outer FilesystemMiddleware can persist them under /large_tool_results.
DEEPAGENTS_FILESYSTEM_EVICT_CHARS = 20_000 * 4


@dataclass(frozen=True)
class ToolContextConfig:
    enabled: bool = True
    immediate_compaction_enabled: bool = False
    single_tool_trigger_tokens: int = 8000
    background_min_result_tokens: int = 1000
    keep_recent_tool_results: int = 12
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
            keep_recent_tool_results=positive("keep_recent_tool_results", 12),
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
        "tool": str(candidate.get("tool") or "unknown_tool"),
        "input": input_value,
        "raw_output_ref": candidate.get("raw_output_ref"),
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
            keep_recent=cfg.keep_recent_tool_results,
            policy_version=POLICY_VERSION,
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

    async def wait(self, session_id: str, timeout: float = 5) -> None:
        task = self._tasks.get(session_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

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
        except Exception as exc:
            logger.exception("Tool Context job failed for session=%s", session_id)
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
        if not self.cfg.immediate_compaction_enabled:
            return result
        raw = str(result.content or "")
        if len(raw) > DEEPAGENTS_FILESYSTEM_EVICT_CHARS:
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
        ready = await asyncio.to_thread(self.manager.get_ready_tool_context_outputs, session_id)
        if not ready:
            return await handler(request)
        messages: list[Any] = []
        for message in request.messages:
            if isinstance(message, ToolMessage) and message.tool_call_id in ready:
                messages.append(message.model_copy(update={"content": ready[message.tool_call_id]}))
            else:
                messages.append(message)
        return await handler(request.override(messages=messages))

    async def aafter_agent(
        self,
        state: Any,
        runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        return {"tool_context_enqueue": True}
