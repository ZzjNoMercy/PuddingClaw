"""统一 LLM 调用客户端。

业务代码只依赖 ModelClient，不再直接实例化 ChatDeepSeek / ChatOpenAI。
调用链：
    业务代码 -> ModelClient -> Higress（若可用）-> 实际模型
                          └-> 直连 DeepSeek / OpenAI / Qwen
"""

from __future__ import annotations

import logging
import time
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables.config import var_child_runnable_config

import capabilities
from config import get_fallback_llm_config, get_gateway_config, get_gateway_llm_config
from graph.token_usage_store import record_token_usage

logger = logging.getLogger(__name__)


def _child_callback_config(run_manager: Any) -> dict[str, Any] | None:
    """Build nested callback config when the current LangChain version exposes it."""
    if run_manager is not None and hasattr(run_manager, "get_child"):
        return {"callbacks": run_manager.get_child()}
    return None


class ModelClient:
    """统一 LLM 调用入口。

    Args:
        role: 调用角色，用于 token 用量分类，如 "agent" / "title" / "summary" / "compensation"
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
        record_usage: bool = True,
    ) -> None:
        self.role = role
        self.cfg = get_fallback_llm_config()
        self.temperature = temperature if temperature is not None else self.cfg.get("temperature", 0.7)
        self.streaming = streaming
        self.force_direct = force_direct
        self.tools = tools or []
        self.bind_tools_kwargs = bind_tools_kwargs or {}
        self.record_usage = record_usage
        self.gateway_cfg = get_gateway_config()

    def _should_use_gateway(self) -> bool:
        """判断是否应该走 AI Gateway。

        不再要求用户在 config.json 里显式启用 gateway。只要 Higress 被探测到可用，
        就优先走网关；未探测到则自动 fallback 到直连。
        """
        if self.force_direct:
            return False
        try:
            caps = capabilities.detect_capabilities_sync()
            return caps.ai_gateway.available
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ModelClient] capability detection failed: %s", exc)
            return False

    def get_chat_model(self) -> BaseChatModel:
        """获取配置好的 LangChain Chat Model。"""
        if self._should_use_gateway():
            model = self._gateway_model()
        else:
            model = self._direct_model()
        return self._apply_tools(model)

    def _apply_tools(self, model: BaseChatModel) -> BaseChatModel:
        """将工具绑定到模型，保持 LangChain `bind_tools` 参数不丢失。"""
        if self.tools:
            model = model.bind_tools(self.tools, **self.bind_tools_kwargs)
        return model

    def _direct_model_with_tools(self) -> BaseChatModel:
        """获取直连模型，并复用当前已绑定工具。"""
        return self._apply_tools(self._direct_model())

    def _gateway_model(self) -> BaseChatModel:
        """通过 Higress（OpenAI-compatible）调用模型。"""
        from langchain_openai import ChatOpenAI

        gateway_url = capabilities.get_effective_gateway_url()
        gateway_model = get_gateway_llm_config().get("model", self.cfg["model"])
        logger.debug("[ModelClient] using AI Gateway: %s model=%s", gateway_url, gateway_model)
        return ChatOpenAI(
            model=gateway_model,
            # Higress 管理上游 Provider key，PuddingClaw 只传一个占位 key。
            api_key="puddingclaw-gateway",
            base_url=gateway_url,
            temperature=self.temperature,
            streaming=self.streaming,
        )

    def _direct_model(self) -> BaseChatModel:
        """直连模型 provider。"""
        provider = self.cfg.get("provider", "deepseek")
        if provider == "deepseek":
            return self._deepseek_model()
        if provider in {"openai", "qwen", "custom"}:
            return self._openai_model()
        raise ValueError(f"Unsupported LLM provider: {provider}")

    def _deepseek_model(self) -> BaseChatModel:
        from langchain_deepseek import ChatDeepSeek

        logger.debug("[ModelClient] using direct DeepSeek")
        return ChatDeepSeek(
            model=self.cfg["model"],
            api_key=self.cfg.get("api_key", ""),
            base_url=self.cfg.get("base_url", "https://api.deepseek.com"),
            temperature=self.temperature,
            streaming=self.streaming,
            stream_usage=True,
        )

    def _openai_model(self) -> BaseChatModel:
        from langchain_openai import ChatOpenAI

        logger.debug("[ModelClient] using direct OpenAI")
        return ChatOpenAI(
            model=self.cfg["model"],
            api_key=self.cfg.get("api_key", ""),
            base_url=self.cfg.get("base_url", "https://api.openai.com/v1"),
            temperature=self.temperature,
            streaming=self.streaming,
        )

    def _record_usage(
        self,
        usage: dict[str, Any],
        start_time: float,
        *,
        user_id: str,
        session_id: str,
        round_num: int,
    ) -> None:
        """从 usage_metadata 提取并记录 token 用量。"""
        if not self.record_usage:
            return
        try:
            record_token_usage(
                user_id=user_id,
                session_id=session_id,
                round_num=round_num,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                start_time=start_time,
                role=self.role,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ModelClient] record token usage failed: %s", exc)

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
        """异步调用 LLM 并记录 token 用量。"""
        using_gateway = self._should_use_gateway()
        llm = self.get_chat_model()
        start = time.time()
        try:
            response = await llm.ainvoke(messages, config=config, stop=stop, **kwargs)
        except Exception:
            if not using_gateway or not self.gateway_cfg.get("fallback_to_direct", True):
                raise
            logger.warning("[ModelClient] gateway invoke failed; retrying direct provider", exc_info=True)
            response = await self._direct_model_with_tools().ainvoke(messages, config=config, stop=stop, **kwargs)
        usage = getattr(response, "usage_metadata", {}) or {}
        self._record_usage(
            usage,
            start,
            user_id=user_id,
            session_id=session_id,
            round_num=round_num,
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
        """异步流式调用 LLM 并记录 token 用量。

        注意：流式用量的聚合依赖底层模型在最后一个 chunk 返回 usage_metadata，
        不同 provider 行为不一致，这里做 best-effort 记录。
        """
        using_gateway = self._should_use_gateway()
        llm = self.get_chat_model()
        start = time.time()
        aggregated_usage: dict[str, int] = {}
        emitted = False
        try:
            async for chunk in llm.astream(messages, config=config, stop=stop, **kwargs):
                emitted = True
                chunk_usage = getattr(chunk, "usage_metadata", None) or {}
                for key in ("input_tokens", "output_tokens", "total_tokens"):
                    if chunk_usage.get(key):
                        aggregated_usage[key] = aggregated_usage.get(key, 0) + chunk_usage[key]
                yield chunk
        except Exception:
            # 流式输出一旦已向客户端发送 token，再回退会造成重复内容；只允许首 token 前重试。
            if emitted or not using_gateway or not self.gateway_cfg.get("fallback_to_direct", True):
                raise
            logger.warning("[ModelClient] gateway stream failed before first token; retrying direct", exc_info=True)
            async for chunk in self._direct_model_with_tools().astream(messages, config=config, stop=stop, **kwargs):
                chunk_usage = getattr(chunk, "usage_metadata", None) or {}
                for key in ("input_tokens", "output_tokens", "total_tokens"):
                    if chunk_usage.get(key):
                        aggregated_usage[key] = aggregated_usage.get(key, 0) + chunk_usage[key]
                yield chunk
        self._record_usage(
            aggregated_usage,
            start,
            user_id=user_id,
            session_id=session_id,
            round_num=round_num,
        )

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
        """同步调用 LLM 并记录 token 用量。"""
        using_gateway = self._should_use_gateway()
        llm = self.get_chat_model()
        start = time.time()
        try:
            response = llm.invoke(messages, config=config, stop=stop, **kwargs)
        except Exception:
            if not using_gateway or not self.gateway_cfg.get("fallback_to_direct", True):
                raise
            logger.warning("[ModelClient] gateway invoke failed; retrying direct provider", exc_info=True)
            response = self._direct_model_with_tools().invoke(messages, config=config, stop=stop, **kwargs)
        usage = getattr(response, "usage_metadata", {}) or {}
        self._record_usage(
            usage,
            start,
            user_id=user_id,
            session_id=session_id,
            round_num=round_num,
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
        """同步流式调用 LLM 并记录 token 用量。"""
        using_gateway = self._should_use_gateway()
        llm = self.get_chat_model()
        start = time.time()
        aggregated_usage: dict[str, int] = {}
        emitted = False
        try:
            for chunk in llm.stream(messages, config=config, stop=stop, **kwargs):
                emitted = True
                chunk_usage = getattr(chunk, "usage_metadata", None) or {}
                for key in ("input_tokens", "output_tokens", "total_tokens"):
                    if chunk_usage.get(key):
                        aggregated_usage[key] = aggregated_usage.get(key, 0) + chunk_usage[key]
                yield chunk
        except Exception:
            if emitted or not using_gateway or not self.gateway_cfg.get("fallback_to_direct", True):
                raise
            logger.warning("[ModelClient] gateway stream failed before first token; retrying direct", exc_info=True)
            for chunk in self._direct_model_with_tools().stream(messages, config=config, stop=stop, **kwargs):
                chunk_usage = getattr(chunk, "usage_metadata", None) or {}
                for key in ("input_tokens", "output_tokens", "total_tokens"):
                    if chunk_usage.get(key):
                        aggregated_usage[key] = aggregated_usage.get(key, 0) + chunk_usage[key]
                yield chunk
        self._record_usage(
            aggregated_usage,
            start,
            user_id=user_id,
            session_id=session_id,
            round_num=round_num,
        )



class ModelClientChatModel(BaseChatModel):
    """把 ModelClient 包装成 LangChain BaseChatModel。

    这样 LangGraph / create_agent 的主 Agent 调用也会完整经过 ModelClient，
    从而统一走 Higress 网关路由、fallback 重试和 token 用量记录。
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
    ) -> None:
        super().__init__()
        self._client = ModelClient(
            role=role,
            temperature=temperature,
            streaming=streaming,
            force_direct=force_direct,
            tools=tools,
            bind_tools_kwargs=bind_tools_kwargs,
            record_usage=False,
        )

    @property
    def _llm_type(self) -> str:
        return "model_client_chat_model"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "role": self._client.role,
            "model": self._client.cfg.get("model"),
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
        config = _child_callback_config(run_manager)
        response = self._client.invoke(messages, config=config, stop=stop, **kwargs)
        return ChatResult(generations=[ChatGeneration(message=response)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        config = _child_callback_config(run_manager)
        response = await self._client.ainvoke(messages, config=config, stop=stop, **kwargs)
        return ChatResult(generations=[ChatGeneration(message=response)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        # Do not pass a child callback manager to the nested provider stream.
        # BaseChatModel will emit callbacks for the chunks yielded here; passing
        # callbacks inward makes LangGraph see each delta twice. Passing
        # config=None is not enough because LangChain also inherits callbacks
        # through var_child_runnable_config.
        token = var_child_runnable_config.set(None)
        try:
            for chunk in self._client.stream(messages, config=None, stop=stop, **kwargs):
                yield ChatGenerationChunk(message=chunk)
        finally:
            var_child_runnable_config.reset(token)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        # See _stream: nested streaming callbacks duplicate token deltas.
        token = var_child_runnable_config.set(None)
        try:
            async for chunk in self._client.astream(messages, config=None, stop=stop, **kwargs):
                yield ChatGenerationChunk(message=chunk)
        finally:
            var_child_runnable_config.reset(token)

    def bind_tools(
        self,
        tools: list[Any],
        **kwargs: Any,
    ) -> "ModelClientChatModel":
        """绑定工具后返回新的 ModelClientChatModel 实例。"""
        return ModelClientChatModel(
            role=self._client.role,
            temperature=self._client.temperature,
            streaming=self._client.streaming,
            force_direct=self._client.force_direct,
            tools=tools,
            bind_tools_kwargs=kwargs,
        )
