"""统一 LLM 调用客户端。

业务代码只依赖 ModelClient，不再直接实例化 ChatDeepSeek / ChatOpenAI。
调用链：业务代码 -> ModelClient -> Provider Registry -> 直连 Provider。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables.config import var_child_runnable_config
from langgraph.config import get_stream_writer

from config import get_fallback_llm_config
from llm.thinking_mapping import normalize_model_temperature

logger = logging.getLogger(__name__)

INTERNAL_CALL_MARKER = "_puddingclaw_internal_call"
CONTEXT_SUMMARY_CALL = "context_summary"


class ModelTransportInterruptedError(RuntimeError):
    """A retryable model stream ended before a complete AIMessage existed."""

    def __init__(
        self,
        *,
        cause: Exception,
        attempt: int,
        route: str,
        chunks_received: int,
    ) -> None:
        super().__init__(
            "model_transport_interrupted: "
            f"{cause.__class__.__name__}: {cause}"
        )
        self.cause = cause
        self.attempt = attempt
        self.route = route
        self.chunks_received = chunks_received


def _retryable_stream_error(exc: Exception) -> bool:
    """Classify transport failures without coupling to one HTTP client."""

    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, TimeoutError)):
        return True
    class_names = {cls.__name__ for cls in type(exc).__mro__}
    if class_names.intersection(
        {
            "ConnectError",
            "ConnectionError",
            "NetworkError",
            "ReadError",
            "ReadTimeout",
            "RemoteProtocolError",
        }
    ):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "connection reset",
            "incomplete chunked read",
            "peer closed connection",
            "server disconnected",
        )
    )


def _emit_model_stream_event(payload: dict[str, Any]) -> None:
    try:
        get_stream_writer()(payload)
    except (KeyError, RuntimeError):
        return


def _usage_value(usage: dict[str, Any], *path: str) -> int:
    value: Any = usage
    for key in path:
        if not isinstance(value, dict):
            return 0
        value = value.get(key)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _merge_usage(total: dict[str, Any], usage: dict[str, Any]) -> None:
    """Merge LangChain usage chunks without discarding provider details."""

    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = _usage_value(usage, key)
        if value:
            total[key] = _usage_value(total, key) + value
    for detail_key, target_key in (
        ("cache_read", "cache_read"),
        ("cache_creation", "cache_creation"),
    ):
        value = _usage_value(usage, "input_token_details", detail_key)
        if value:
            details = total.setdefault("input_token_details", {})
            details[target_key] = _usage_value(details, target_key) + value
    reasoning = _usage_value(usage, "output_token_details", "reasoning")
    if reasoning:
        details = total.setdefault("output_token_details", {})
        details["reasoning"] = _usage_value(details, "reasoning") + reasoning


def _message_text(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text") or block.get("content") or "")
            for block in content
            if isinstance(block, dict)
        )
    return ""


def _internal_call_kind(messages: list[BaseMessage]) -> str | None:
    """Identify middleware-owned model calls that must not enter user SSE text."""

    for message in messages:
        text = _message_text(message)
        if (
            "<role>\nContext Extraction Assistant\n</role>" in text
            and "## SESSION INTENT" in text
            and "<messages>\nMessages to summarize:" in text
        ):
            return CONTEXT_SUMMARY_CALL
    return None


def _mark_internal_message(message: BaseMessage, kind: str | None) -> BaseMessage:
    if not kind:
        return message
    additional = dict(getattr(message, "additional_kwargs", None) or {})
    additional[INTERNAL_CALL_MARKER] = kind
    message.additional_kwargs = additional
    return message


def _emit_internal_call_status(kind: str | None, status: str) -> None:
    """Surface context compression without exposing its generated summary."""

    if kind != CONTEXT_SUMMARY_CALL:
        return
    try:
        writer = get_stream_writer()
        writer(
            {
                "type": "context_maintenance",
                "status": status,
                "phase": "deepagents_summarization",
                **(
                    {"message": "上下文达到压缩阈值，正在压缩，完成后将继续生成..."}
                    if status == "start"
                    else {}
                ),
            }
        )
    except (KeyError, RuntimeError):
        # Title generation, tests and direct ModelClient calls may run outside
        # a LangGraph streaming context.
        return


def _patch_openai_reasoning_extraction() -> None:
    """Preserve provider-specific reasoning fields that ChatOpenAI drops.

    ChatOpenAI only targets official OpenAI delta fields. When Higress routes
    to DeepSeek-style providers, the SSE delta contains ``reasoning_content``
    which is silently discarded. This patch attaches it to
    ``additional_kwargs`` so downstream extractors can surface it as the
    thinking process.
    """
    try:
        from langchain_openai.chat_models import base as openai_base
    except Exception:
        return

    _original = openai_base._convert_delta_to_message_chunk

    def _wrapped(_dict, default_class):
        chunk = _original(_dict, default_class)
        if not isinstance(chunk, AIMessageChunk):
            return chunk

        additional = getattr(chunk, "additional_kwargs", None) or {}
        updated = False

        # DeepSeek / third-party provider reasoning_content
        if "reasoning_content" in _dict:
            existing = additional.get("reasoning_content", "")
            additional["reasoning_content"] = str(existing) + str(
                _dict["reasoning_content"] or ""
            )
            updated = True

        # OpenAI Responses API style reasoning object
        if "reasoning" in _dict:
            additional["reasoning"] = _dict["reasoning"]
            updated = True

        if updated:
            chunk.additional_kwargs = additional

        return chunk

    openai_base._convert_delta_to_message_chunk = _wrapped


_patch_openai_reasoning_extraction()


def _child_callback_config(run_manager: Any) -> dict[str, Any] | None:
    """Build nested callback config when the current LangChain version exposes it."""
    if run_manager is not None and hasattr(run_manager, "get_child"):
        return {"callbacks": run_manager.get_child()}
    return None


def _record_model_input_trace(
    messages: list[BaseMessage],
    *,
    tool_schema_count: int,
    tool_schemas: list[Any] | None = None,
    model_params: dict[str, Any] | None = None,
    capture_boundary: str,
) -> None:
    """Best-effort local trace hook for the final payload entering the model."""

    try:
        from graph.trace_collector import get_current_trace_collector

        collector = get_current_trace_collector()
        if collector is None:
            return
        collector.add_model_input_span(
            messages=messages,
            tool_schema_count=tool_schema_count,
            tool_schemas=tool_schemas or [],
            model_params=model_params or {},
            capture_boundary=capture_boundary,
        )
    except Exception:
        logger.debug("[ModelClient] failed to record model input trace", exc_info=True)


class ModelClient:
    """统一 LLM 调用入口。

    Args:
        role: 调用角色，用于区分 Agent、标题、摘要等模型工作负载
        temperature: 采样温度；None 时使用 config.json 中的默认值
        streaming: 是否启用流式输出
        force_direct: 为 True 时跳过 Higress，直接走直连 provider（用于测试或兜底）
    """

    def __init__(
        self,
        *,
        role: str = "agent",
        temperature: float | None = None,
        streaming: bool = False,
        force_direct: bool = False,
        tools: list[Any] | None = None,
        bind_tools_kwargs: dict[str, Any] | None = None,
        thinking_enabled: bool | None = None,
        model_override: str | None = None,
        model_id_override: str | None = None,
        thinking_level: str | None = None,
        credential_name: str | None = None,
        binding: str = "agent",
    ) -> None:
        self.role = role
        self.binding = binding
        self.thinking_enabled = thinking_enabled
        self.model_override = str(model_override or "").strip() or None
        self.model_id_override = str(model_id_override or "").strip() or None
        self.thinking_level = str(thinking_level or "").strip() or None
        self.credential_name = str(credential_name or "").strip() or None
        resolution_kwargs: dict[str, Any] = {
            "thinking_enabled_override": thinking_enabled,
            "binding": binding,
        }
        if self.model_id_override is not None:
            resolution_kwargs["model_id_override"] = self.model_id_override
        if self.thinking_level is not None:
            resolution_kwargs["thinking_level"] = self.thinking_level
        if self.credential_name is not None:
            resolution_kwargs["credential_name"] = self.credential_name
        self.cfg = get_fallback_llm_config(**resolution_kwargs)
        if self.model_override is not None:
            self.cfg["model"] = self.model_override
        requested_temperature = float(
            temperature if temperature is not None else self.cfg.get("temperature", 0.7)
        )
        self.temperature = normalize_model_temperature(
            provider_id=str(self.cfg.get("provider") or ""),
            model_name=str(self.cfg.get("model") or ""),
            temperature=requested_temperature,
        )
        self.cfg["temperature"] = self.temperature
        if self.temperature != requested_temperature:
            logger.info(
                "[ModelClient] normalized temperature: role=%s provider=%s model=%s requested=%s effective=%s",
                self.role,
                self.cfg.get("provider") or "<unknown>",
                self.cfg.get("model") or "<unknown>",
                requested_temperature,
                self.temperature,
            )
        self.streaming = streaming
        self.force_direct = force_direct
        self.tools = tools or []
        self.bind_tools_kwargs = bind_tools_kwargs or {}

    def get_chat_model(self) -> BaseChatModel:
        """获取已绑定的直连 Provider 模型。"""
        return self._apply_tools(self._direct_model())

    def _apply_tools(self, model: BaseChatModel) -> BaseChatModel:
        """将工具绑定到模型，保持 LangChain `bind_tools` 参数不丢失。"""
        if self.tools:
            model = model.bind_tools(self.tools, **self.bind_tools_kwargs)
        return model

    def _direct_model_with_tools(self) -> BaseChatModel:
        """获取直连模型，并复用当前已绑定工具。"""
        return self._apply_tools(self._direct_model())

    def _thinking_kwargs(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """构造思考模式参数（reasoning_effort / extra_body.thinking）。

        当会话/调用方显式选择推理强度时，统一传递给底层模型。
        DeepSeek 官方 API 以及 Higress 透传场景均支持这些参数。
        """
        kwargs: dict[str, Any] = {}
        reasoning_effort = cfg.get("reasoning_effort")
        extra_body = cfg.get("extra_body")
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if extra_body:
            kwargs["extra_body"] = extra_body
        return kwargs

    def _direct_model(self) -> BaseChatModel:
        """直连模型 provider。"""
        provider = self.cfg.get("provider", "deepseek")
        protocol = self.cfg.get("protocol") or ("deepseek" if provider == "deepseek" else "openai_compatible" if provider in {"openai", "qwen", "custom", "kimi", "siliconflow"} else "")
        if provider == "deepseek" or protocol == "deepseek":
            return self._deepseek_model()
        if protocol == "openai_compatible":
            return self._openai_model()
        raise ValueError(f"Unsupported LLM provider: {provider}")

    def _deepseek_model(self) -> BaseChatModel:
        from langchain_deepseek import ChatDeepSeek

        thinking_kwargs = self._thinking_kwargs(self.cfg)
        logger.info(
            "[ModelClient] using direct DeepSeek: role=%s model=%s thinking=%s",
            self.role,
            self.cfg["model"],
            bool(thinking_kwargs),
        )
        if thinking_kwargs:
            logger.info("[ModelClient] thinking kwargs: %s", thinking_kwargs)
        return ChatDeepSeek(
            model=self.cfg["model"],
            api_key=self.cfg.get("api_key", ""),
            base_url=self.cfg.get("base_url", "https://api.deepseek.com"),
            temperature=self.temperature,
            streaming=self.streaming,
            stream_usage=True,
            **thinking_kwargs,
        )

    def _openai_model(self) -> BaseChatModel:
        from langchain_openai import ChatOpenAI

        thinking_kwargs = self._thinking_kwargs(self.cfg)
        logger.info(
            "[ModelClient] using direct OpenAI-compatible: role=%s provider=%s model=%s thinking=%s",
            self.role,
            self.cfg.get("provider", "openai"),
            self.cfg["model"],
            bool(thinking_kwargs),
        )
        if thinking_kwargs:
            logger.info("[ModelClient] thinking kwargs: %s", thinking_kwargs)
        return ChatOpenAI(
            model=self.cfg["model"],
            api_key=self.cfg.get("api_key", ""),
            base_url=self.cfg.get("base_url", "https://api.openai.com/v1"),
            temperature=self.temperature,
            streaming=self.streaming,
            stream_usage=True,
            **thinking_kwargs,
        )

    def _emit_usage(
        self,
        usage: dict[str, Any],
        *,
        call_id: str,
        started_at: float,
        generation_started_at: float | None = None,
    ) -> None:
        """Publish one provider call's facts to the enclosing Agent stream."""

        completed_at = time.perf_counter()
        input_tokens = _usage_value(usage, "input_tokens")
        output_tokens = _usage_value(usage, "output_tokens")
        total_tokens = _usage_value(usage, "total_tokens") or input_tokens + output_tokens
        duration_seconds = max(0.0, completed_at - started_at)
        generation_seconds = max(
            0.0,
            completed_at - (generation_started_at if generation_started_at is not None else started_at),
        )
        measured = bool(usage) and any(
            (input_tokens, output_tokens, total_tokens)
        )
        _emit_model_stream_event(
            {
                "type": "model_usage",
                "call_id": call_id,
                "provider": str(self.cfg.get("provider") or ""),
                "model": str(self.cfg.get("model") or ""),
                "role": self.role,
                "binding": self.binding,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cache_read_tokens": _usage_value(
                    usage, "input_token_details", "cache_read"
                ),
                "cache_creation_tokens": _usage_value(
                    usage, "input_token_details", "cache_creation"
                ),
                "reasoning_tokens": _usage_value(
                    usage, "output_token_details", "reasoning"
                ),
                "duration_ms": round(duration_seconds * 1000),
                "tokens_per_second": (
                    round(output_tokens / generation_seconds, 1)
                    if output_tokens > 0 and generation_seconds > 0
                    else None
                ),
                "measured": measured,
            }
        )

    async def ainvoke(
        self,
        messages: list[BaseMessage],
        config: Any = None,
        *,
        user_id: str = "model_client",
        session_id: str = "model_client",
        round_num: int = 0,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        """异步调用 LLM，并向当前 Agent Run 发布 provider 用量。"""
        llm = self.get_chat_model()
        call_id = uuid.uuid4().hex
        start = time.perf_counter()
        response = await llm.ainvoke(messages, config=config, stop=stop, **kwargs)
        usage = getattr(response, "usage_metadata", {}) or {}
        self._emit_usage(
            usage,
            call_id=call_id,
            started_at=start,
        )
        return response

    async def astream(
        self,
        messages: list[BaseMessage],
        config: Any = None,
        *,
        user_id: str = "model_client",
        session_id: str = "model_client",
        round_num: int = 0,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        """异步流式调用 LLM 并发布 provider 用量。

        注意：流式用量的聚合依赖底层模型在最后一个 chunk 返回 usage_metadata，
        不同 provider 行为不一致，这里做 best-effort 记录。
        """
        llm = self.get_chat_model()
        call_id = uuid.uuid4().hex
        start = time.perf_counter()
        generation_started_at: float | None = None
        route = "direct"
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            emitted_chunks = 0
            attempt_usage: dict[str, Any] = {}
            _emit_model_stream_event(
                {
                    "type": "model_stream_attempt",
                    "status": "started",
                    "attempt": attempt,
                    "route": route,
                    "role": self.role,
                    "model": self.cfg.get("model"),
                }
            )
            try:
                async for chunk in llm.astream(messages, config=config, stop=stop, **kwargs):
                    emitted_chunks += 1
                    if generation_started_at is None:
                        generation_started_at = time.perf_counter()
                    preview = _message_text(chunk)
                    if preview:
                        _emit_model_stream_event(
                            {
                                "type": "model_stream_preview",
                                "attempt": attempt,
                                "route": route,
                                "content": preview,
                            }
                        )
                    chunk_usage = getattr(chunk, "usage_metadata", None) or {}
                    _merge_usage(attempt_usage, chunk_usage)
                    # Preserve genuine provider streaming. Once a chunk has
                    # crossed this boundary the model node must never retry,
                    # because a second attempt would replay or contradict
                    # content already visible to the graph and UI.
                    yield chunk
            except Exception as exc:
                retryable = _retryable_stream_error(exc)
                can_retry = (
                    emitted_chunks == 0
                    and attempt < max_attempts
                    and retryable
                )
                _emit_model_stream_event(
                    {
                        "type": "model_transport_interrupted",
                        "status": "interrupted",
                        "attempt": attempt,
                        "route": route,
                        "role": self.role,
                        "model": self.cfg.get("model"),
                        "chunks_received": emitted_chunks,
                        "chunks_emitted": emitted_chunks,
                        "error_class": exc.__class__.__name__,
                        "retryable": retryable,
                        "next_action": (
                            "retry_same_model_node"
                            if can_retry
                            else "stop_without_replay"
                            if emitted_chunks
                            else "stop"
                        ),
                    }
                )
                if can_retry:
                    logger.warning(
                        "[ModelClient] model stream interrupted; retrying attempt=%d route=%s chunks=%d",
                        attempt,
                        route,
                        emitted_chunks,
                        exc_info=True,
                    )
                    await asyncio.sleep(0.25 * (2 ** (attempt - 1)))
                    continue
                if retryable:
                    raise ModelTransportInterruptedError(
                        cause=exc,
                        attempt=attempt,
                        route=route,
                        chunks_received=emitted_chunks,
                    ) from exc
                raise
            _emit_model_stream_event(
                {
                    "type": "model_stream_attempt",
                    "status": "completed",
                    "attempt": attempt,
                    "route": route,
                    "role": self.role,
                    "model": self.cfg.get("model"),
                    "chunks_received": emitted_chunks,
                }
            )
            self._emit_usage(
                attempt_usage,
                call_id=call_id,
                started_at=start,
                generation_started_at=generation_started_at,
            )
            return

    def invoke(
        self,
        messages: list[BaseMessage],
        config: Any = None,
        *,
        user_id: str = "model_client",
        session_id: str = "model_client",
        round_num: int = 0,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        """同步调用 LLM，并向当前 Agent Run 发布 provider 用量。"""
        llm = self.get_chat_model()
        call_id = uuid.uuid4().hex
        start = time.perf_counter()
        response = llm.invoke(messages, config=config, stop=stop, **kwargs)
        usage = getattr(response, "usage_metadata", {}) or {}
        self._emit_usage(
            usage,
            call_id=call_id,
            started_at=start,
        )
        return response

    def stream(
        self,
        messages: list[BaseMessage],
        config: Any = None,
        *,
        user_id: str = "model_client",
        session_id: str = "model_client",
        round_num: int = 0,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        """同步流式调用 LLM 并发布 provider 用量。"""
        llm = self.get_chat_model()
        call_id = uuid.uuid4().hex
        start = time.perf_counter()
        generation_started_at: float | None = None
        route = "direct"
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            emitted_chunks = 0
            attempt_usage: dict[str, Any] = {}
            _emit_model_stream_event(
                {
                    "type": "model_stream_attempt",
                    "status": "started",
                    "attempt": attempt,
                    "route": route,
                    "role": self.role,
                    "model": self.cfg.get("model"),
                }
            )
            try:
                for chunk in llm.stream(messages, config=config, stop=stop, **kwargs):
                    emitted_chunks += 1
                    if generation_started_at is None:
                        generation_started_at = time.perf_counter()
                    preview = _message_text(chunk)
                    if preview:
                        _emit_model_stream_event(
                            {
                                "type": "model_stream_preview",
                                "attempt": attempt,
                                "route": route,
                                "content": preview,
                            }
                        )
                    chunk_usage = getattr(chunk, "usage_metadata", None) or {}
                    _merge_usage(attempt_usage, chunk_usage)
                    yield chunk
            except Exception as exc:
                retryable = _retryable_stream_error(exc)
                can_retry = (
                    emitted_chunks == 0
                    and attempt < max_attempts
                    and retryable
                )
                _emit_model_stream_event(
                    {
                        "type": "model_transport_interrupted",
                        "status": "interrupted",
                        "attempt": attempt,
                        "route": route,
                        "role": self.role,
                        "model": self.cfg.get("model"),
                        "chunks_received": emitted_chunks,
                        "chunks_emitted": emitted_chunks,
                        "error_class": exc.__class__.__name__,
                        "retryable": retryable,
                        "next_action": (
                            "retry_same_model_node"
                            if can_retry
                            else "stop_without_replay"
                            if emitted_chunks
                            else "stop"
                        ),
                    }
                )
                if can_retry:
                    logger.warning(
                        "[ModelClient] model stream interrupted; retrying attempt=%d route=%s chunks=%d",
                        attempt,
                        route,
                        emitted_chunks,
                        exc_info=True,
                    )
                    time.sleep(0.25 * (2 ** (attempt - 1)))
                    continue
                if retryable:
                    raise ModelTransportInterruptedError(
                        cause=exc,
                        attempt=attempt,
                        route=route,
                        chunks_received=emitted_chunks,
                    ) from exc
                raise
            _emit_model_stream_event(
                {
                    "type": "model_stream_attempt",
                    "status": "completed",
                    "attempt": attempt,
                    "route": route,
                    "role": self.role,
                    "model": self.cfg.get("model"),
                    "chunks_received": emitted_chunks,
                }
            )
            self._emit_usage(
                attempt_usage,
                call_id=call_id,
                started_at=start,
                generation_started_at=generation_started_at,
            )
            return



class ModelClientChatModel(BaseChatModel):
    """把 ModelClient 包装成 LangChain BaseChatModel。

    这样 LangGraph / create_agent 的主 Agent 调用也会完整经过 ModelClient，
    从而统一走 Provider Registry 解析、同一绑定内的传输重试和 token 用量记录。
    """

    def __init__(
        self,
        *,
        role: str = "agent",
        temperature: float | None = None,
        streaming: bool = True,
        force_direct: bool = False,
        tools: list[Any] | None = None,
        bind_tools_kwargs: dict[str, Any] | None = None,
        thinking_enabled: bool | None = None,
        model_override: str | None = None,
        model_id_override: str | None = None,
        thinking_level: str | None = None,
        credential_name: str | None = None,
        binding: str = "agent",
    ) -> None:
        # BaseChatModel otherwise may route ``ainvoke`` through ``_astream``
        # when callbacks request streaming, even though the wrapped provider
        # was constructed with streaming=False. A non-streaming provider can
        # return a full AIMessage there, which is not a BaseMessageChunk.
        super().__init__(disable_streaming=not streaming)
        self._client = ModelClient(
            role=role,
            temperature=temperature,
            streaming=streaming,
            force_direct=force_direct,
            tools=tools,
            bind_tools_kwargs=bind_tools_kwargs,
            thinking_enabled=thinking_enabled,
            model_override=model_override,
            model_id_override=model_id_override,
            thinking_level=thinking_level,
            credential_name=credential_name,
            binding=binding,
        )

    @property
    def _llm_type(self) -> str:
        return "model_client_chat_model"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "role": self._client.role,
            "binding": self._client.binding,
            "model": self._client.cfg.get("model"),
            "credential_name": self._client.credential_name or "default",
            "temperature": self._client.temperature,
            "streaming": self._client.streaming,
        }

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        _record_model_input_trace(
            messages,
            tool_schema_count=len(self._client.tools),
            tool_schemas=self._client.tools,
            model_params=self._model_trace_params(),
            capture_boundary="ModelClientChatModel._generate",
        )
        config = _child_callback_config(run_manager)
        internal_call = _internal_call_kind(messages)
        _emit_internal_call_status(internal_call, "start")
        try:
            response = self._client.invoke(messages, config=config, stop=stop, **kwargs)
            _mark_internal_message(response, internal_call)
            return ChatResult(generations=[ChatGeneration(message=response)])
        finally:
            _emit_internal_call_status(internal_call, "done")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        _record_model_input_trace(
            messages,
            tool_schema_count=len(self._client.tools),
            tool_schemas=self._client.tools,
            model_params=self._model_trace_params(),
            capture_boundary="ModelClientChatModel._agenerate",
        )
        config = _child_callback_config(run_manager)
        internal_call = _internal_call_kind(messages)
        _emit_internal_call_status(internal_call, "start")
        try:
            response = await self._client.ainvoke(messages, config=config, stop=stop, **kwargs)
            _mark_internal_message(response, internal_call)
            return ChatResult(generations=[ChatGeneration(message=response)])
        finally:
            _emit_internal_call_status(internal_call, "done")

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        _record_model_input_trace(
            messages,
            tool_schema_count=len(self._client.tools),
            tool_schemas=self._client.tools,
            model_params=self._model_trace_params(),
            capture_boundary="ModelClientChatModel._stream",
        )
        # Do not pass a child callback manager to the nested provider stream.
        # BaseChatModel will emit callbacks for the chunks yielded here; passing
        # callbacks inward makes LangGraph see each delta twice. Passing
        # config=None is not enough because LangChain also inherits callbacks
        # through var_child_runnable_config.
        token = var_child_runnable_config.set(None)
        internal_call = _internal_call_kind(messages)
        _emit_internal_call_status(internal_call, "start")
        try:
            for chunk in self._client.stream(messages, config=None, stop=stop, **kwargs):
                _mark_internal_message(chunk, internal_call)
                yield ChatGenerationChunk(message=chunk)
        finally:
            var_child_runnable_config.reset(token)
            _emit_internal_call_status(internal_call, "done")

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        _record_model_input_trace(
            messages,
            tool_schema_count=len(self._client.tools),
            tool_schemas=self._client.tools,
            model_params=self._model_trace_params(),
            capture_boundary="ModelClientChatModel._astream",
        )
        # See _stream: nested streaming callbacks duplicate token deltas.
        token = var_child_runnable_config.set(None)
        internal_call = _internal_call_kind(messages)
        _emit_internal_call_status(internal_call, "start")
        try:
            async for chunk in self._client.astream(messages, config=None, stop=stop, **kwargs):
                _mark_internal_message(chunk, internal_call)
                yield ChatGenerationChunk(message=chunk)
        finally:
            var_child_runnable_config.reset(token)
            _emit_internal_call_status(internal_call, "done")

    def bind_tools(
        self,
        tools: list[Any],
        **kwargs: Any,
    ) -> ModelClientChatModel:
        """绑定工具后返回新的 ModelClientChatModel 实例。"""
        return ModelClientChatModel(
            role=self._client.role,
            temperature=self._client.temperature,
            streaming=self._client.streaming,
            force_direct=self._client.force_direct,
            tools=tools,
            bind_tools_kwargs=kwargs,
            thinking_enabled=self._client.thinking_enabled,
            model_override=self._client.model_override,
            model_id_override=self._client.model_id_override,
            thinking_level=self._client.thinking_level,
            credential_name=self._client.credential_name,
            binding=self._client.binding,
        )

    def _model_trace_params(self) -> dict[str, Any]:
        return {
            "role": self._client.role,
            "binding": self._client.binding,
            "model": self._client.cfg.get("model"),
            "temperature": self._client.temperature,
            "streaming": self._client.streaming,
            "force_direct": self._client.force_direct,
            "thinking_enabled": self._client.thinking_enabled,
            "model_override": self._client.model_override,
            "model_id_override": self._client.model_id_override,
            "thinking_level": self._client.thinking_level,
            "credential_name": self._client.credential_name or "default",
            "tool_choice": self._client.bind_tools_kwargs.get("tool_choice"),
            "strict": self._client.bind_tools_kwargs.get("strict"),
        }
