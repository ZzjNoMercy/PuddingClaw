"""Runtime middleware proxy for trace attribution.

The proxy wraps PuddingClaw-owned middleware instances before passing them into
DeepAgents/LangChain. It records hook before/after summaries without modifying
third-party source code.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware

from graph.trace_collector import TraceCollector, get_current_trace_collector

STATE_HOOKS = (
    "before_agent",
    "before_model",
    "after_model",
    "after_agent",
)
ASYNC_STATE_HOOKS = {
    "abefore_agent": "before_agent",
    "abefore_model": "before_model",
    "aafter_model": "after_model",
    "aafter_agent": "after_agent",
}
WRAP_HOOKS = ("wrap_model_call", "wrap_tool_call")
ASYNC_WRAP_HOOKS = {
    "awrap_model_call": "wrap_model_call",
    "awrap_tool_call": "wrap_tool_call",
}


class _TracingMiddlewareProxyBase(AgentMiddleware):
    """Base class used by dynamically generated proxy subclasses."""

    def __init__(self, wrapped: AgentMiddleware) -> None:
        self._wrapped = wrapped

    @property
    def name(self) -> str:
        return getattr(self._wrapped, "name", self._wrapped.__class__.__name__)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._wrapped, item)

    def __repr__(self) -> str:
        return f"<TracingMiddlewareProxy wrapped={self.name}>"

    def _run_state_hook(
        self,
        hook: str,
        state: Any,
        runtime: Any = None,
        config: Any = None,
        **kwargs: Any,
    ) -> Any:
        method = getattr(self._wrapped, hook)
        collector = get_current_trace_collector()
        before = TraceCollector.summarize_hook_payload(state) if collector else None
        try:
            result = self._call_state_method(method, state=state, runtime=runtime, config=config, extra=kwargs)
        except Exception as exc:
            self._record_error(collector, hook=hook, before=before, exc=exc)
            raise
        after = TraceCollector.merged_state_summary(state, result) if collector else None
        self._record(collector, hook=hook, before=before, after=after)
        return result

    async def _arun_state_hook(
        self,
        async_hook: str,
        hook: str,
        state: Any,
        runtime: Any = None,
        config: Any = None,
        **kwargs: Any,
    ) -> Any:
        method = getattr(self._wrapped, async_hook)
        collector = get_current_trace_collector()
        before = TraceCollector.summarize_hook_payload(state) if collector else None
        try:
            result = await self._call_state_method(method, state=state, runtime=runtime, config=config, extra=kwargs)
        except Exception as exc:
            self._record_error(collector, hook=hook, before=before, exc=exc)
            raise
        after = TraceCollector.merged_state_summary(state, result) if collector else None
        self._record(collector, hook=hook, before=before, after=after)
        return result

    @staticmethod
    def _call_state_method(
        method: Callable[..., Any],
        *,
        state: Any,
        runtime: Any,
        config: Any,
        extra: dict[str, Any],
    ) -> Any:
        signature = inspect.signature(method)
        parameters = signature.parameters
        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
            return method(state=state, runtime=runtime, config=config, **extra)
        call_kwargs: dict[str, Any] = {}
        values = {"state": state, "runtime": runtime, "config": config, **extra}
        for name, parameter in parameters.items():
            if parameter.kind in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            } and name in values:
                call_kwargs[name] = values[name]
        try:
            return method(**call_kwargs)
        except TypeError:
            positional = [
                value
                for value in (state, runtime, config)
                if value is not None
            ]
            return method(*positional[: len(parameters)])

    def _run_wrap_hook(self, hook: str, *args: Any, **kwargs: Any) -> Any:
        method = getattr(self._wrapped, hook)
        collector = get_current_trace_collector()
        request = args[0] if args else kwargs.get("request")
        handler = args[1] if len(args) > 1 else kwargs.get("handler")
        request_before = TraceCollector.summarize_hook_payload(request) if collector else None
        request_sent: dict[str, Any] | None = None
        response_observed: dict[str, Any] | None = None

        def traced_handler(next_request: Any) -> Any:
            nonlocal request_sent, response_observed
            request_sent = TraceCollector.summarize_hook_payload(next_request) if collector else None
            response = handler(next_request)
            response_observed = TraceCollector.summarize_hook_payload(response) if collector else None
            return response

        call_args, call_kwargs = self._replace_handler_arg(args, kwargs, traced_handler)
        try:
            result = method(*call_args, **call_kwargs)
        except Exception as exc:
            self._record_error(collector, hook=hook, before=request_before, exc=exc)
            raise
        if response_observed is None and result is not None:
            response_observed = TraceCollector.summarize_hook_payload(result) if collector else None
        self._record_wrap(
            collector,
            hook=hook,
            request_before=request_before,
            request_sent=request_sent,
            response_observed=response_observed,
        )
        return result

    async def _arun_wrap_hook(
        self,
        async_hook: str,
        hook: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        method = getattr(self._wrapped, async_hook)
        collector = get_current_trace_collector()
        request = args[0] if args else kwargs.get("request")
        handler = args[1] if len(args) > 1 else kwargs.get("handler")
        request_before = TraceCollector.summarize_hook_payload(request) if collector else None
        request_sent: dict[str, Any] | None = None
        response_observed: dict[str, Any] | None = None

        async def traced_handler(next_request: Any) -> Any:
            nonlocal request_sent, response_observed
            request_sent = TraceCollector.summarize_hook_payload(next_request) if collector else None
            response = await handler(next_request)
            response_observed = TraceCollector.summarize_hook_payload(response) if collector else None
            return response

        call_args, call_kwargs = self._replace_handler_arg(args, kwargs, traced_handler)
        try:
            result = await method(*call_args, **call_kwargs)
        except Exception as exc:
            self._record_error(collector, hook=hook, before=request_before, exc=exc)
            raise
        if response_observed is None and result is not None:
            response_observed = TraceCollector.summarize_hook_payload(result) if collector else None
        self._record_wrap(
            collector,
            hook=hook,
            request_before=request_before,
            request_sent=request_sent,
            response_observed=response_observed,
        )
        return result

    @staticmethod
    def _replace_handler_arg(
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        traced_handler: Callable[..., Any],
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        if len(args) > 1:
            return (args[0], traced_handler, *args[2:]), kwargs
        next_kwargs = dict(kwargs)
        next_kwargs["handler"] = traced_handler
        return args, next_kwargs

    def _record(
        self,
        collector: TraceCollector | None,
        *,
        hook: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        status: str = "read",
    ) -> None:
        if collector is None:
            return
        model_call_index = collector.model_call_index_for_hook(hook)
        metadata = {
            "proxied_middleware": self._wrapped.__class__.__name__,
            "proxied_module": self._wrapped.__class__.__module__,
            "observability_layer": "middleware_proxy",
        }
        if model_call_index is not None:
            metadata["model_call_index"] = model_call_index
        collector.record_middleware_hook_attribution(
            hook=hook,
            middleware=self.name,
            before=before,
            after=after,
            status=status,
            evidence=[f"{self.name}.{hook} captured by middleware proxy."],
            metadata=metadata,
        )

    def _record_wrap(
        self,
        collector: TraceCollector | None,
        *,
        hook: str,
        request_before: dict[str, Any] | None,
        request_sent: dict[str, Any] | None,
        response_observed: dict[str, Any] | None,
    ) -> None:
        if collector is None:
            return
        model_call_index = collector.model_call_index_for_hook(hook)
        metadata = {
            "proxied_middleware": self._wrapped.__class__.__name__,
            "proxied_module": self._wrapped.__class__.__module__,
            "observability_layer": "middleware_proxy",
        }
        if model_call_index is not None:
            metadata["model_call_index"] = model_call_index
        collector.record_wrap_hook_attribution(
            hook=hook,
            middleware=self.name,
            request_before=request_before,
            request_sent=request_sent,
            response_observed=response_observed,
            evidence=[f"{self.name}.{hook} request captured by middleware proxy."],
            metadata=metadata,
        )

    def _record_error(
        self,
        collector: TraceCollector | None,
        *,
        hook: str,
        before: dict[str, Any] | None,
        exc: Exception,
    ) -> None:
        self._record(
            collector,
            hook=hook,
            before=before,
            after={
                "payload_kind": "error",
                "error_type": exc.__class__.__name__,
                "message": str(exc),
            },
            status="error",
        )


def wrap_middlewares_for_trace(middlewares: list[Any]) -> list[Any]:
    """Wrap AgentMiddleware objects with trace proxies.

    Only hooks actually implemented by the wrapped middleware class are exposed
    on the generated proxy class. This preserves LangChain's hook detection and
    avoids creating synthetic middleware nodes.
    """

    return [wrap_middleware_for_trace(middleware) for middleware in middlewares]


def wrap_middleware_for_trace(middleware: Any) -> Any:
    if isinstance(middleware, _TracingMiddlewareProxyBase):
        return middleware
    if not isinstance(middleware, AgentMiddleware):
        return middleware
    methods: dict[str, Any] = {}
    for hook in STATE_HOOKS:
        if _implements_hook(middleware, hook):
            proxy_hook = _make_state_hook(hook)
            _copy_hook_config(getattr(middleware.__class__, hook), proxy_hook)
            methods[hook] = proxy_hook
    for async_hook, hook in ASYNC_STATE_HOOKS.items():
        if _implements_hook(middleware, async_hook):
            proxy_hook = _make_async_state_hook(async_hook, hook)
            _copy_hook_config(getattr(middleware.__class__, async_hook), proxy_hook)
            methods[async_hook] = proxy_hook
    for hook in WRAP_HOOKS:
        if _implements_hook(middleware, hook):
            methods[hook] = _make_wrap_hook(hook)
    for async_hook, hook in ASYNC_WRAP_HOOKS.items():
        if _implements_hook(middleware, async_hook):
            methods[async_hook] = _make_async_wrap_hook(async_hook, hook)

    if not methods:
        return middleware

    proxy_class = type(
        f"Tracing{middleware.__class__.__name__}",
        (_TracingMiddlewareProxyBase, middleware.__class__),
        methods,
    )
    return proxy_class(middleware)


def _copy_hook_config(source: Any, target: Any) -> None:
    """Preserve LangChain graph-edge metadata on generated proxy hooks.

    Losing ``__can_jump_to__`` makes a traced middleware return a state field
    named ``jump_to`` without the graph having the corresponding conditional
    edge. The trace then looks correct while the completion loop silently ends.
    """

    can_jump_to = getattr(source, "__can_jump_to__", None)
    if can_jump_to is not None:
        target.__can_jump_to__ = list(can_jump_to)


def _implements_hook(middleware: AgentMiddleware, hook: str) -> bool:
    return getattr(middleware.__class__, hook, None) is not getattr(AgentMiddleware, hook, None)


def _make_state_hook(hook: str) -> Callable[..., Any]:
    def _hook(
        self: _TracingMiddlewareProxyBase,
        state: Any,
        runtime: Any = None,
        config: Any = None,
        **kwargs: Any,
    ) -> Any:
        return self._run_state_hook(hook, state, runtime=runtime, config=config, **kwargs)

    return _hook


def _make_async_state_hook(async_hook: str, hook: str) -> Callable[..., Awaitable[Any]]:
    async def _hook(
        self: _TracingMiddlewareProxyBase,
        state: Any,
        runtime: Any = None,
        config: Any = None,
        **kwargs: Any,
    ) -> Any:
        return await self._arun_state_hook(async_hook, hook, state, runtime=runtime, config=config, **kwargs)

    return _hook


def _make_wrap_hook(hook: str) -> Callable[..., Any]:
    def _hook(self: _TracingMiddlewareProxyBase, *args: Any, **kwargs: Any) -> Any:
        return self._run_wrap_hook(hook, *args, **kwargs)

    return _hook


def _make_async_wrap_hook(async_hook: str, hook: str) -> Callable[..., Awaitable[Any]]:
    async def _hook(self: _TracingMiddlewareProxyBase, *args: Any, **kwargs: Any) -> Any:
        return await self._arun_wrap_hook(async_hook, hook, *args, **kwargs)

    return _hook
