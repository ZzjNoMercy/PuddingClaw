"""Local trace collector for Agent-mode runs.

Builds a white-box execution trace from DeepAgents graph events. The resulting
tree is persisted to `session.json` and can be rendered by the frontend trace
viewer. LangSmith remains optional and independent.
"""

from __future__ import annotations

import time
import uuid
import ast
import hashlib
import json
from contextvars import ContextVar
from typing import Any


_current_trace_collector: ContextVar["TraceCollector | None"] = ContextVar(
    "current_trace_collector",
    default=None,
)


def get_current_trace_collector() -> "TraceCollector | None":
    return _current_trace_collector.get()


class TraceSpan:
    """A single node in the execution trace tree."""

    def __init__(
        self,
        *,
        span_id: str,
        parent_id: str | None,
        span_type: str,
        name: str,
        started_at: float,
        input_data: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.id = span_id
        self.parent_id = parent_id
        self.type = span_type
        self.name = name
        self.started_at = started_at
        self.completed_at: float | None = None
        self.input = input_data
        self.output: Any = None
        self.status = "running"
        self.metadata = dict(metadata) if metadata else {}
        self.children: list[TraceSpan] = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "type": self.type,
            "name": self.name,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "input": self._serialize(self.input),
            "output": self._serialize(self.output),
            "metadata": self.metadata,
        }

    @staticmethod
    def _serialize(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, (list, dict, tuple)):
            return value
        try:
            return str(value)
        except Exception:
            return None


TraceEventCallback = Any  # Callable[[str, dict[str, Any]], None]


class TraceCollector:
    """Collect spans during a single Agent run.

    Usage:
        collector = TraceCollector(session_id="...")
        collector.start_llm_span("model", input_data=...)
        collector.start_tool_span("read_file", tool_call_id="...", input_data=...)
        collector.finish_tool_span(tool_call_id="...", output=..., is_error=False)
        collector.finish_llm_span(output=...)
        trace = collector.finish(status="completed")

    If ``emit_callback`` is provided, it is invoked with
    ``(event_name, payload)`` whenever a span starts or ends, enabling
    real-time SSE updates.
    """

    def __init__(
        self,
        session_id: str,
        query_id: str | None = None,
        emit_callback: TraceEventCallback | None = None,
        runtime_inventory: dict[str, Any] | None = None,
    ) -> None:
        self.session_id = session_id
        self.query_id = query_id or f"query-{uuid.uuid4().hex[:12]}"
        self.trace_id = f"trace-{uuid.uuid4().hex[:12]}"
        self.started_at = time.time()
        self.completed_at: float | None = None
        self.status = "running"
        self._emit_callback = emit_callback
        self.runtime_inventory = runtime_inventory or {}
        self.root = TraceSpan(
            span_id=f"{self.trace_id}-root",
            parent_id=None,
            span_type="root",
            name="agent.run",
            started_at=self.started_at,
            input_data=None,
            metadata={"session_id": session_id, "query_id": self.query_id},
        )
        self._active_spans: dict[str, TraceSpan] = {self.root.id: self.root}
        self._span_stack: list[TraceSpan] = [self.root]
        self._tool_span_by_call_id: dict[str, TraceSpan] = {}
        self._model_input_count = 0
        self._last_model_input_summary: dict[str, Any] | None = None
        self._skill_effect_recorded = False
        self._memory_effect_recorded = False
        self._subagent_effect_recorded = False
        self._middleware_effects: list[dict[str, Any]] = []
        self._middleware_invocations: list[dict[str, Any]] = []
        self._middleware_invocation_counts: dict[str, int] = {}
        self._hook_boundary_snapshots: list[dict[str, Any]] = []
        self._event_order = 0
        self.root.metadata.setdefault("event_order", self._next_event_order())
        self._emit("trace_span_start", self._event_payload(self.root))

    def model_call_index_for_hook(self, hook: str) -> int | None:
        """Return the model call index currently associated with a model hook.

        `before_model` and `wrap_model_call` run before `add_model_input_span`
        increments the counter, so the current model call is `_model_input_count`.
        `after_model` runs after the model input boundary has been captured.
        """

        if hook in {"before_model", "wrap_model_call"}:
            return self._model_input_count
        if hook == "after_model" and self._model_input_count > 0:
            return self._model_input_count - 1
        return None

    def __enter__(self) -> "TraceCollector":
        self._context_token = _current_trace_collector.set(self)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        token = getattr(self, "_context_token", None)
        if token is not None:
            _current_trace_collector.reset(token)
            self._context_token = None

    def _new_id(self, prefix: str) -> str:
        return f"{self.trace_id}-{prefix}-{uuid.uuid4().hex[:8]}"

    def _next_event_order(self) -> int:
        order = self._event_order
        self._event_order += 1
        return order

    def _current_parent(self) -> TraceSpan:
        return self._span_stack[-1] if self._span_stack else self.root

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._emit_callback:
            try:
                self._emit_callback(event, payload)
            except Exception:
                pass

    def _event_payload(self, span: TraceSpan) -> dict[str, Any]:
        return {
            "span": span.to_dict(),
            "trace_id": self.trace_id,
            "query_id": self.query_id,
            "session_id": self.session_id,
        }

    def start_llm_span(
        self,
        name: str = "model",
        input_data: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        span_id = self._new_id("llm")
        parent = self._current_parent()
        span_metadata = dict(metadata) if metadata else {}
        span_metadata.setdefault("event_order", self._next_event_order())
        span = TraceSpan(
            span_id=span_id,
            parent_id=parent.id,
            span_type="llm",
            name=name,
            started_at=time.time(),
            input_data=input_data,
            metadata=span_metadata,
        )
        parent.children.append(span)
        self._active_spans[span_id] = span
        self._span_stack.append(span)
        self._emit("trace_span_start", self._event_payload(span))
        return span_id

    def finish_llm_span(self, output: Any = None, error: str | None = None) -> None:
        if not self._span_stack:
            return
        # Find the nearest active LLM span on the stack.
        for idx in range(len(self._span_stack) - 1, -1, -1):
            if self._span_stack[idx].type == "llm":
                span = self._span_stack.pop(idx)
                span.completed_at = time.time()
                span.output = output
                if error:
                    span.status = "error"
                    span.metadata["error"] = error
                else:
                    span.status = "completed"
                self._emit("trace_span_end", self._event_payload(span))
                return

    def add_model_input_span(
        self,
        *,
        messages: list[Any],
        tool_schema_count: int = 0,
        tool_schemas: list[Any] | None = None,
        model_params: dict[str, Any] | None = None,
        capture_boundary: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Record the final message payload entering the LLM boundary."""

        preview = [self._message_preview(message) for message in messages]
        tool_schema_items = [self._tool_schema_contract(tool) for tool in (tool_schemas or [])]
        estimated_tokens = sum(item.get("estimated_tokens", 0) for item in preview)
        system_prompt_chars = sum(
            item.get("chars", 0)
            for item in preview
            if item.get("role") in {"system", "SystemMessage"}
        )
        system_prompt_text = "\n\n".join(
            str(item.get("content") or "")
            for item in preview
            if str(item.get("role", "")).lower() in {"system", "systemmessage"}
        )
        fingerprints = {
            "messages_hash": self._stable_hash(preview),
            "system_prompt_hash": self._stable_hash(system_prompt_text),
            "tool_schema_hash": self._stable_hash(tool_schema_items),
        }
        assembly = self._model_input_assembly(
            preview=preview,
            tool_schema_items=tool_schema_items,
            model_params=model_params or {},
            capture_boundary=capture_boundary,
            fingerprints=fingerprints,
        )
        contract = {
            "message_count": len(messages),
            "system_prompt_chars": system_prompt_chars,
            "estimated_tokens": estimated_tokens,
            "tool_schema_count": tool_schema_count,
            "tool_schemas": tool_schema_items,
            "params": model_params or {},
            "fingerprints": fingerprints,
            "assembly": assembly,
        }
        model_call_index = self._model_input_count
        self._model_input_count += 1
        span_metadata = {
            "model_call_index": model_call_index,
            "message_count": len(messages),
            "estimated_tokens": estimated_tokens,
            "system_prompt_chars": system_prompt_chars,
            "tool_schema_count": tool_schema_count,
            "capture_boundary": capture_boundary,
            "fingerprints": fingerprints,
            "model_params": model_params or {},
            "harness": {
                "mechanism": "context_management",
                "pillars": [
                    {"name": "context_engineering", "role": "primary"},
                    {"name": "garbage_collection", "role": "supporting"},
                ],
            },
        }
        if metadata:
            span_metadata.update(metadata)

        # `add_model_input_span` observes the final prompt after before_agent
        # middleware has already run. Record prompt-level before_agent evidence
        # in middleware stack order before the before_model boundary so the UI
        # does not confuse detection order with execution order.
        self._record_skill_prompt_effect(preview)
        self._record_memory_prompt_effect(preview)
        self._record_subagent_prompt_effect(preview)

        span_id = self.add_custom_span(
            "model.input",
            {
                "messages_preview": preview,
                "model_call_contract": contract,
            },
            span_type="model_input",
            metadata=span_metadata,
        )
        self._record_model_input_effect(
            preview=preview,
            metadata=span_metadata,
            capture_boundary=capture_boundary,
        )
        self._record_model_call_boundary_snapshots(
            preview=preview,
            metadata=span_metadata,
            contract=contract,
            capture_boundary=capture_boundary,
            source_span_id=span_id,
        )
        return span_id

    def _model_input_assembly(
        self,
        *,
        preview: list[dict[str, Any]],
        tool_schema_items: list[dict[str, Any]],
        model_params: dict[str, Any],
        capture_boundary: str,
        fingerprints: dict[str, str],
    ) -> dict[str, Any]:
        """Describe how the final LLM payload is assembled at the model boundary."""

        system_messages = [
            item
            for item in preview
            if str(item.get("role", "")).lower() in {"system", "systemmessage"}
        ]
        conversation_messages = [
            item
            for item in preview
            if str(item.get("role", "")).lower() not in {"system", "systemmessage"}
        ]
        role_counts: dict[str, int] = {}
        for item in preview:
            role = str(item.get("role") or "unknown").lower()
            role_counts[role] = role_counts.get(role, 0) + 1

        system_chars = sum(int(item.get("chars") or 0) for item in system_messages)
        message_chars = sum(int(item.get("chars") or 0) for item in conversation_messages)
        tool_call_count = sum(int(item.get("tool_call_count") or 0) for item in preview)
        bind_kwargs = {
            key: value
            for key, value in {
                "tool_choice": model_params.get("tool_choice"),
                "strict": model_params.get("strict"),
            }.items()
            if value is not None
        }
        return {
            "boundary": capture_boundary,
            "principle": "final_payload_entering_llm",
            "sections": [
                {
                    "key": "system_prompt",
                    "label": "System prompt",
                    "source": "LangChain messages with role=system",
                    "count": len(system_messages),
                    "chars": system_chars,
                    "hash": fingerprints.get("system_prompt_hash"),
                    "included": len(system_messages) > 0,
                    "notes": [
                        "DeepAgents base system prompt and middleware materialized prompt text appear here when present.",
                        "Tools are not counted here unless some middleware writes tool text into the prompt.",
                    ],
                },
                {
                    "key": "messages",
                    "label": "Messages",
                    "source": "LangChain messages payload",
                    "count": len(conversation_messages),
                    "chars": message_chars,
                    "hash": fingerprints.get("messages_hash"),
                    "included": len(conversation_messages) > 0,
                    "roles": role_counts,
                    "tool_call_count": tool_call_count,
                    "notes": [
                        "User, assistant, tool, and AI messages are preserved as structured messages.",
                    ],
                },
                {
                    "key": "tools",
                    "label": "Tools",
                    "source": "ModelClient.bind_tools structured schema",
                    "count": len(tool_schema_items),
                    "hash": fingerprints.get("tool_schema_hash"),
                    "included": len(tool_schema_items) > 0,
                    "binding": {
                        "mode": "bind_tools",
                        "kwargs": bind_kwargs,
                    },
                    "notes": [
                        "Tool schemas are sent as the model API tool/function schema field, not appended to system prompt text.",
                    ],
                },
                {
                    "key": "params",
                    "label": "Model params",
                    "source": "ModelClient runtime configuration",
                    "count": len([value for value in model_params.values() if value is not None]),
                    "included": bool(model_params),
                    "params": model_params,
                    "notes": [
                        "Model, temperature, streaming, and tool binding options are transport parameters around the message payload.",
                    ],
                },
            ],
        }

    def _record_model_call_boundary_snapshots(
        self,
        *,
        preview: list[dict[str, Any]],
        metadata: dict[str, Any],
        contract: dict[str, Any],
        capture_boundary: str,
        source_span_id: str,
    ) -> None:
        snapshot = self._model_input_summary(preview, metadata)
        snapshot["contract"] = {
            "fingerprints": contract.get("fingerprints", {}),
            "params": contract.get("params", {}),
            "tool_schemas": contract.get("tool_schemas", []),
        }
        common_metadata = {
            "model_call_index": metadata.get("model_call_index"),
            "capture_boundary": capture_boundary,
            "source_span_id": source_span_id,
        }
        self.add_hook_boundary_snapshot(
            hook="before_model",
            phase="after",
            title="before_model.after",
            snapshot=snapshot,
            metadata=common_metadata,
            evidence=[
                "Observed final model input after before_model processing.",
                "Boundary snapshot only; not per-middleware attribution.",
            ],
        )
        self.add_hook_boundary_snapshot(
            hook="wrap_model_call",
            phase="before",
            title="wrap_model_call.before",
            snapshot=snapshot,
            metadata=common_metadata,
            evidence=[
                "Observed request immediately before the wrapped LLM boundary.",
                "wrap_model_call is a wrapper around graph.model, not a linear pre-step.",
            ],
        )

    def add_hook_boundary_snapshot(
        self,
        *,
        hook: str,
        phase: str,
        title: str,
        snapshot: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        evidence: list[str] | None = None,
    ) -> dict[str, Any]:
        snapshot_metadata = dict(metadata) if metadata else {}
        sequence = snapshot_metadata.get("event_order")
        if not isinstance(sequence, int):
            sequence = self._next_event_order()
            snapshot_metadata["event_order"] = sequence
        item = {
            "id": self._new_id("hook-boundary"),
            "hook": hook,
            "phase": phase,
            "title": title,
            "snapshot": TraceSpan._serialize(snapshot),
            "metadata": snapshot_metadata,
            "evidence": evidence or [],
            "created_at": time.time(),
            "sequence": sequence,
        }
        self._hook_boundary_snapshots.append(item)
        self._emit(
            "hook_boundary_snapshot",
            {
                "snapshot": item,
                "trace_id": self.trace_id,
                "query_id": self.query_id,
                "session_id": self.session_id,
            },
        )
        return item

    def _record_model_input_effect(
        self,
        *,
        preview: list[dict[str, Any]],
        metadata: dict[str, Any],
        capture_boundary: str,
    ) -> None:
        current = self._model_input_summary(preview, metadata)
        previous = self._last_model_input_summary
        diff = self._summary_diff(previous, current)
        evidence = [
            f"{current['message_count']} messages / ~{current['estimated_tokens']} tokens",
            f"{current['tool_schema_count']} tool schemas",
            f"capture boundary: {capture_boundary}",
        ]
        if previous is None:
            evidence.insert(0, "first final payload entering the LLM")
        else:
            deltas = [
                f"messages {self._signed_delta(diff.get('message_count_delta'))}",
                f"tokens {self._signed_delta(diff.get('estimated_tokens_delta'))}",
                f"tools {self._signed_delta(diff.get('tool_schema_count_delta'))}",
            ]
            evidence.insert(0, "changed since previous model input: " + ", ".join(deltas))
        self.add_middleware_effect(
            category="model_input",
            title="Model input boundary",
            hook="before_model",
            middleware=self._middleware_candidates("model_input"),
            before=previous,
            after=current,
            diff=diff,
            evidence=evidence,
            metadata={
                "model_call_index": metadata.get("model_call_index"),
                "capture_boundary": capture_boundary,
            },
        )
        self._last_model_input_summary = current

    def _record_skill_prompt_effect(self, preview: list[dict[str, Any]]) -> None:
        if self._skill_effect_recorded:
            return
        skills = self.runtime_inventory.get("skills") or []
        if not skills:
            return
        mounted_skills = [
            {
                "name": str(skill.get("name") or ""),
                "location": str(skill.get("location") or ""),
                "description": skill.get("description") or "",
            }
            for skill in skills
        ]
        self.add_middleware_effect(
            category="skills",
            title="Skills metadata loaded into state",
            hook="before_agent",
            middleware=self._middleware_candidates("skills"),
            before={"skills_in_state": 0},
            after={
                "skills_in_state": len(skills),
                "mounted_skills": len(skills),
                "skills_metadata": mounted_skills[:12],
            },
            diff={
                "skills_in_state_delta": len(skills),
            },
            evidence=[
                f"{len(skills)} skills available in state.skills_metadata",
                "SkillsMiddleware.before_agent loads metadata; prompt injection happens later at model request time.",
            ],
            metadata={"state_field": "skills_metadata", "prompt_injection_stage": "before_model"},
        )
        self._skill_effect_recorded = True

    def _record_memory_prompt_effect(self, preview: list[dict[str, Any]]) -> None:
        if self._memory_effect_recorded:
            return
        memory_middlewares = self._middleware_candidates("memory")
        if not memory_middlewares:
            return
        system_text = self._system_text(preview)
        if not system_text:
            return
        source_candidates = ["/AGENTS.md", "/gstack/AGENTS.md"]
        matched_sources = [source for source in source_candidates if source in system_text]
        agent_memory_present = "<agent_memory>" in system_text and "</agent_memory>" in system_text
        if not agent_memory_present and not matched_sources:
            return
        evidence = [
            "agent_memory block found in final system prompt" if agent_memory_present else "",
            *[f"memory source present: {source}" for source in matched_sources],
        ]
        memory_after = {
            "agent_memory_present": agent_memory_present,
            "matched_sources": matched_sources,
            "source_count": len(matched_sources),
        }
        memory_diff = {
            "memory_contents_loaded": agent_memory_present or bool(matched_sources),
            "source_count_delta": len(matched_sources),
        }
        self.add_middleware_effect(
            category="memory",
            title="Memory loaded into agent state",
            hook="before_agent",
            middleware=memory_middlewares,
            before={"memory_contents_loaded": False},
            after=memory_after,
            diff=memory_diff,
            evidence=[item for item in evidence if item],
            metadata={
                "system_prompt_checked": True,
                "memory_sources": matched_sources,
                "state_field": "memory_contents",
            },
        )
        self.add_middleware_effect(
            category="memory",
            title="Memory injected into system prompt",
            hook="wrap_model_call",
            middleware=memory_middlewares,
            before={"agent_memory_in_system_prompt": False},
            after=memory_after,
            diff={
                "agent_memory_in_system_prompt": agent_memory_present,
                "source_count_delta": len(matched_sources),
            },
            evidence=[item for item in evidence if item],
            metadata={
                "system_prompt_checked": True,
                "memory_sources": matched_sources,
                "request_field": "system_message",
                "state_field": "memory_contents",
            },
        )
        self._memory_effect_recorded = True

    def _record_subagent_prompt_effect(self, preview: list[dict[str, Any]]) -> None:
        if self._subagent_effect_recorded:
            return
        subagent_middlewares = self._middleware_candidates("subagent")
        if not subagent_middlewares:
            return
        system_text = self._system_text(preview)
        if not system_text:
            return
        subagents = self.runtime_inventory.get("subagents") or []
        matched = [
            {
                "name": str(item.get("name") or ""),
                "source": str(item.get("source") or ""),
                "route_trigger": str(item.get("route_trigger") or ""),
            }
            for item in subagents
            if str(item.get("name") or "") and str(item.get("name") or "") in system_text
        ]
        native_marker_present = "Available subagent types:" in system_text or "## `task` (subagent spawner)" in system_text
        if not native_marker_present and not matched:
            return
        evidence = [
            "DeepAgents task/subagent instructions found in final system prompt"
            if native_marker_present
            else "",
            f"{len(matched)} subagent names found in final system prompt",
        ]
        self.add_middleware_effect(
            category="subagent",
            title="SubAgents exposed to main agent",
            hook="wrap_model_call",
            middleware=subagent_middlewares,
            before={"available_subagent_types": 0},
            after={
                "native_marker_present": native_marker_present,
                "mounted_subagents": len(subagents),
                "matched_subagents": matched[:12],
            },
            diff={
                "subagent_prompt_injected": native_marker_present or bool(matched),
                "matched_subagents_delta": len(matched),
            },
            evidence=[item for item in evidence if item],
            metadata={"system_prompt_checked": True},
        )
        self._subagent_effect_recorded = True

    @staticmethod
    def _system_text(preview: list[dict[str, Any]]) -> str:
        return "\n".join(
            str(item.get("content") or item.get("preview") or "")
            for item in preview
            if str(item.get("role", "")).lower() in {"system", "systemmessage"}
        )

    @staticmethod
    def _signed_delta(value: Any) -> str:
        if not isinstance(value, (int, float)):
            return "+0"
        return f"{value:+g}"

    @staticmethod
    def _model_input_summary(
        preview: list[dict[str, Any]], metadata: dict[str, Any]
    ) -> dict[str, Any]:
        role_counts: dict[str, int] = {}
        tool_call_count = 0
        for item in preview:
            role = str(item.get("role") or "unknown").lower()
            role_counts[role] = role_counts.get(role, 0) + 1
            tool_call_count += int(item.get("tool_call_count") or 0)
        non_system = [
            {
                "role": item.get("role"),
                "name": item.get("name"),
                "chars": item.get("chars"),
                "preview": item.get("preview"),
                "tool_calls": item.get("tool_calls") or [],
            }
            for item in preview
            if str(item.get("role", "")).lower() not in {"system", "systemmessage"}
        ]
        return {
            "model_call_index": metadata.get("model_call_index"),
            "message_count": metadata.get("message_count", len(preview)),
            "estimated_tokens": metadata.get("estimated_tokens", 0),
            "system_prompt_chars": metadata.get("system_prompt_chars", 0),
            "tool_schema_count": metadata.get("tool_schema_count", 0),
            "fingerprints": metadata.get("fingerprints", {}),
            "tool_call_count": tool_call_count,
            "roles": role_counts,
            "recent_messages": non_system[-4:],
        }

    @staticmethod
    def _stable_hash(value: Any) -> str:
        payload = json.dumps(
            TraceCollector._canonicalize(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _canonicalize(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {
                str(key): TraceCollector._canonicalize(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, (list, tuple)):
            return [TraceCollector._canonicalize(item) for item in value]
        return TraceSpan._serialize(value)

    @staticmethod
    def _tool_schema_contract(tool: Any) -> dict[str, Any]:
        serialized = TraceSpan._serialize(tool)
        if isinstance(serialized, dict):
            function = serialized.get("function") if isinstance(serialized.get("function"), dict) else {}
            name = serialized.get("name") or function.get("name") or getattr(tool, "name", None)
            description = serialized.get("description") or function.get("description") or getattr(tool, "description", None)
            schema = (
                serialized.get("parameters")
                or function.get("parameters")
                or serialized.get("args_schema")
                or serialized.get("schema")
            )
        else:
            name = getattr(tool, "name", None) or getattr(tool, "__name__", None) or type(tool).__name__
            description = getattr(tool, "description", None) or getattr(tool, "__doc__", None)
            schema = getattr(tool, "args_schema", None)
        schema_value = TraceSpan._serialize(schema)
        return {
            "name": str(name or "tool"),
            "description": str(description or "")[:240],
            "schema_hash": TraceCollector._stable_hash(schema_value),
        }

    @staticmethod
    def _summary_diff(
        before: dict[str, Any] | None, after: dict[str, Any]
    ) -> dict[str, Any]:
        if before is None:
            return {
                "message_count_delta": after.get("message_count", 0),
                "estimated_tokens_delta": after.get("estimated_tokens", 0),
                "system_prompt_chars_delta": after.get("system_prompt_chars", 0),
                "tool_schema_count_delta": after.get("tool_schema_count", 0),
                "tool_call_count_delta": after.get("tool_call_count", 0),
                "initial": True,
            }
        keys = [
            "message_count",
            "estimated_tokens",
            "system_prompt_chars",
            "tool_schema_count",
            "tool_call_count",
        ]
        diff: dict[str, Any] = {}
        for key in keys:
            before_value = before.get(key, 0)
            after_value = after.get(key, 0)
            if isinstance(before_value, (int, float)) and isinstance(after_value, (int, float)):
                diff[f"{key}_delta"] = after_value - before_value
        diff["roles_before"] = before.get("roles", {})
        diff["roles_after"] = after.get("roles", {})
        return diff

    @classmethod
    def summarize_hook_payload(cls, value: Any) -> dict[str, Any]:
        """Build a stable, comparable summary for middleware hook payloads."""

        if cls._looks_like_model_request(value):
            return cls._model_request_summary(value)
        if cls._looks_like_tool_request(value):
            return cls._tool_request_summary(value)
        if cls._looks_like_model_response(value):
            return cls._message_collection_summary(
                getattr(value, "result", []) or [],
                extra={"payload_kind": "model_response"},
            )
        if isinstance(value, dict) and "messages" in value:
            return cls._state_summary(value)
        if isinstance(value, dict):
            return {
                "payload_kind": "dict",
                "keys": sorted(str(key) for key in value.keys())[:30],
                "payload_hash": cls._stable_hash(value),
            }
        return {
            "payload_kind": type(value).__name__,
            "payload_hash": cls._stable_hash(TraceSpan._serialize(value)),
            "preview": str(value)[:600],
        }

    @classmethod
    def merged_state_summary(cls, state: Any, update: Any) -> dict[str, Any]:
        """Summarize state after applying a middleware update when possible."""

        if isinstance(state, dict) and isinstance(update, dict):
            merged = {**state, **update}
            return cls.summarize_hook_payload(merged)
        if update is None:
            return cls.summarize_hook_payload(state)
        return cls.summarize_hook_payload(update)

    @classmethod
    def hook_summary_diff(
        cls, before: dict[str, Any] | None, after: dict[str, Any] | None
    ) -> dict[str, Any]:
        if not before or not after:
            return {}
        diff: dict[str, Any] = {}
        for key in [
            "message_count",
            "estimated_tokens",
            "system_prompt_chars",
            "tool_schema_count",
            "tool_call_count",
            "todo_count",
        ]:
            before_value = before.get(key)
            after_value = after.get(key)
            if isinstance(before_value, (int, float)) and isinstance(after_value, (int, float)):
                delta = after_value - before_value
                if delta:
                    diff[f"{key}_delta"] = delta
        for key in ["messages_hash", "system_prompt_hash", "tool_schema_hash", "state_hash", "payload_hash"]:
            before_value = before.get(key)
            after_value = after.get(key)
            if before_value is not None and after_value is not None and before_value != after_value:
                diff[f"{key}_changed"] = True
        diff.update(cls._state_field_diff(before, after))
        return diff

    @classmethod
    def _state_field_diff(cls, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        before_fields = before.get("state_fields")
        after_fields = after.get("state_fields")
        if not isinstance(before_fields, dict) or not isinstance(after_fields, dict):
            return {}
        before_keys = set(before_fields)
        after_keys = set(after_fields)
        added = sorted(after_keys - before_keys)
        removed = sorted(before_keys - after_keys)
        changed = sorted(
            key
            for key in before_keys & after_keys
            if before_fields.get(key, {}).get("hash") != after_fields.get(key, {}).get("hash")
        )
        diff: dict[str, Any] = {}
        if added:
            diff["state_keys_added"] = added
        if removed:
            diff["state_keys_removed"] = removed
        if changed:
            diff["state_fields_changed"] = changed
        for key in sorted(before_keys & after_keys):
            before_count = before_fields.get(key, {}).get("count")
            after_count = after_fields.get(key, {}).get("count")
            if isinstance(before_count, (int, float)) and isinstance(after_count, (int, float)):
                delta = after_count - before_count
                if delta:
                    diff[f"state_{key}_count_delta"] = delta
        return diff

    def record_middleware_hook_attribution(
        self,
        *,
        hook: str,
        middleware: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        status: str = "read",
        evidence: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        diff = self.hook_summary_diff(before, after)
        resolved_status = "error" if status == "error" else ("changed" if diff else status)
        invocation = self.add_middleware_invocation(
            hook=hook,
            middleware=[middleware],
            category=self._hook_category(hook),
            title=f"{middleware}.{hook}",
            status=resolved_status,
            evidence=evidence or ["Observed by PuddingClaw middleware proxy."],
            before=before,
            after=after,
            diff=diff,
            metadata={
                **(metadata or {}),
                "attribution": "middleware_proxy",
                "coverage": "direct",
            },
        )
        common_metadata = {
            **(metadata or {}),
            "middleware_invocation_id": invocation["id"],
            "middleware": middleware,
            "attribution": "middleware_proxy",
            "coverage": "direct",
        }
        if before is not None:
            self.add_hook_boundary_snapshot(
                hook=hook,
                phase="before",
                title=f"{middleware}.{hook}.before",
                snapshot=before,
                metadata=common_metadata,
                evidence=["Captured immediately before the proxied middleware hook."],
            )
        if after is not None:
            self.add_hook_boundary_snapshot(
                hook=hook,
                phase="after",
                title=f"{middleware}.{hook}.after",
                snapshot=after,
                metadata=common_metadata,
                evidence=["Captured immediately after the proxied middleware hook returned."],
            )
        return invocation

    def record_wrap_hook_attribution(
        self,
        *,
        hook: str,
        middleware: str,
        request_before: dict[str, Any] | None,
        request_sent: dict[str, Any] | None,
        response_observed: dict[str, Any] | None = None,
        status: str = "read",
        evidence: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record wrap hook attribution without diffing request against response."""

        diff = self.hook_summary_diff(request_before, request_sent)
        resolved_status = "error" if status == "error" else ("changed" if diff else status)
        payload_after = {
            "request_sent": request_sent,
            "response_observed": response_observed,
        }
        invocation = self.add_middleware_invocation(
            hook=hook,
            middleware=[middleware],
            category=self._hook_category(hook),
            title=f"{middleware}.{hook}",
            status=resolved_status,
            evidence=evidence or ["Observed by PuddingClaw middleware proxy."],
            before=request_before,
            after=payload_after,
            diff=diff,
            metadata={
                **(metadata or {}),
                "attribution": "middleware_proxy",
                "coverage": "direct",
                "wrap_semantics": "request_diff",
            },
        )
        common_metadata = {
            **(metadata or {}),
            "middleware_invocation_id": invocation["id"],
            "middleware": middleware,
            "attribution": "middleware_proxy",
            "coverage": "direct",
            "wrap_semantics": "request_diff",
        }
        if request_before is not None:
            self.add_hook_boundary_snapshot(
                hook=hook,
                phase="before",
                title=f"{middleware}.{hook}.request_before_wrapper",
                snapshot=request_before,
                metadata=common_metadata,
                evidence=["Captured before entering the proxied wrap hook."],
            )
        if request_sent is not None:
            self.add_hook_boundary_snapshot(
                hook=hook,
                phase="request",
                title=f"{middleware}.{hook}.request_sent_to_handler",
                snapshot=request_sent,
                metadata=common_metadata,
                evidence=["Captured when the wrap hook called handler(request)."],
            )
        if response_observed is not None:
            self.add_hook_boundary_snapshot(
                hook=hook,
                phase="response",
                title=f"{middleware}.{hook}.response_observed",
                snapshot=response_observed,
                metadata=common_metadata,
                evidence=["Observed handler result; response is not diffed against request."],
            )
        return invocation

    @staticmethod
    def _hook_category(hook: str) -> str:
        if hook in {"before_model", "wrap_model_call"}:
            return "model_input"
        if hook in {"before_agent", "after_agent"}:
            return "context"
        if hook in {"after_model"}:
            return "state"
        if hook in {"wrap_tool_call"}:
            return "tools"
        return "middleware"

    @staticmethod
    def _looks_like_model_request(value: Any) -> bool:
        return all(hasattr(value, attr) for attr in ("messages", "model", "tools")) and hasattr(value, "system_message")

    @staticmethod
    def _looks_like_model_response(value: Any) -> bool:
        return hasattr(value, "result") and isinstance(getattr(value, "result", None), list)

    @staticmethod
    def _looks_like_tool_request(value: Any) -> bool:
        return hasattr(value, "tool_call") and hasattr(value, "tool")

    @classmethod
    def _state_summary(cls, state: dict[str, Any]) -> dict[str, Any]:
        summary = cls._message_collection_summary(state.get("messages") or [], extra={"payload_kind": "state"})
        todos = state.get("todos")
        if isinstance(todos, list):
            summary["todo_count"] = len(todos)
            summary["todo_hash"] = cls._stable_hash(todos)
        skills_metadata = state.get("skills_metadata")
        if isinstance(skills_metadata, list):
            summary["skills_count"] = len(skills_metadata)
            summary["skills_hash"] = cls._stable_hash(skills_metadata)
        summary["state_keys"] = sorted(str(key) for key in state.keys())[:30]
        summary["state_field_count"] = len(state)
        summary["state_fields"] = {
            str(key): cls._state_field_summary(str(key), value)
            for key, value in sorted(state.items(), key=lambda item: str(item[0]))
        }
        summary["state_hash"] = cls._stable_hash(state)
        return summary

    @classmethod
    def _state_field_summary(cls, key: str, value: Any) -> dict[str, Any]:
        serialized = TraceSpan._serialize(value)
        summary: dict[str, Any] = {
            "type": type(value).__name__,
            "hash": cls._stable_hash(serialized),
        }
        if isinstance(value, list):
            summary["count"] = len(value)
            if key == "messages":
                message_summary = cls._message_collection_summary(value)
                summary.update(
                    {
                        "roles": message_summary.get("roles", {}),
                        "tool_call_count": message_summary.get("tool_call_count", 0),
                        "recent_messages": message_summary.get("recent_messages", []),
                    }
                )
            elif key == "skills_metadata":
                summary["names"] = [
                    str(item.get("name") or "")
                    for item in value
                    if isinstance(item, dict)
                ][:20]
            elif key == "todos":
                status_counts: dict[str, int] = {}
                for item in value:
                    status = str(item.get("status") or "unknown") if isinstance(item, dict) else "unknown"
                    status_counts[status] = status_counts.get(status, 0) + 1
                summary["status_counts"] = status_counts
            else:
                summary["sample"] = TraceSpan._serialize(value[:3])
        elif isinstance(value, dict):
            keys = sorted(str(item) for item in value.keys())
            summary["count"] = len(value)
            summary["keys"] = keys[:20]
        elif isinstance(value, str):
            summary["chars"] = len(value)
            summary["preview"] = value[:300]
        elif value is None:
            summary["is_null"] = True
        else:
            summary["preview"] = str(value)[:300]
        return summary

    @classmethod
    def _model_request_summary(cls, request: Any) -> dict[str, Any]:
        messages = []
        system_message = getattr(request, "system_message", None)
        if system_message is not None:
            messages.append(system_message)
        messages.extend(list(getattr(request, "messages", []) or []))
        tools = list(getattr(request, "tools", []) or [])
        summary = cls._message_collection_summary(
            messages,
            extra={
                "payload_kind": "model_request",
                "tool_schema_count": len(tools),
                "tool_schema_hash": cls._stable_hash([cls._tool_schema_contract(tool) for tool in tools]),
            },
        )
        summary["model"] = type(getattr(request, "model", None)).__name__
        summary["tool_choice"] = TraceSpan._serialize(getattr(request, "tool_choice", None))
        return summary

    @classmethod
    def _tool_request_summary(cls, request: Any) -> dict[str, Any]:
        tool_call = getattr(request, "tool_call", None)
        preview = cls._tool_call_preview(tool_call)
        tool = getattr(request, "tool", None)
        return {
            "payload_kind": "tool_request",
            "tool_name": preview.get("name") or getattr(tool, "name", None),
            "tool_call_id": preview.get("id"),
            "tool_args_hash": cls._stable_hash(preview.get("args")),
            "tool_schema": cls._tool_schema_contract(tool) if tool is not None else None,
            "payload_hash": cls._stable_hash(preview),
        }

    @classmethod
    def _message_collection_summary(
        cls, messages: list[Any], extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        preview = [cls._message_preview(message) for message in messages]
        estimated_tokens = sum(item.get("estimated_tokens", 0) for item in preview)
        system_prompt_text = "\n\n".join(
            str(item.get("content") or "")
            for item in preview
            if str(item.get("role", "")).lower() in {"system", "systemmessage"}
        )
        system_prompt_chars = sum(
            item.get("chars", 0)
            for item in preview
            if str(item.get("role", "")).lower() in {"system", "systemmessage"}
        )
        tool_call_count = sum(int(item.get("tool_call_count") or 0) for item in preview)
        role_counts: dict[str, int] = {}
        for item in preview:
            role = str(item.get("role") or "unknown").lower()
            role_counts[role] = role_counts.get(role, 0) + 1
        summary = {
            "message_count": len(messages),
            "estimated_tokens": estimated_tokens,
            "system_prompt_chars": system_prompt_chars,
            "tool_call_count": tool_call_count,
            "roles": role_counts,
            "messages_hash": cls._stable_hash(preview),
            "system_prompt_hash": cls._stable_hash(system_prompt_text),
            "recent_messages": [
                {
                    "role": item.get("role"),
                    "name": item.get("name"),
                    "chars": item.get("chars"),
                    "preview": item.get("preview"),
                    "tool_calls": item.get("tool_calls") or [],
                }
                for item in preview[-4:]
            ],
        }
        if extra:
            summary.update(extra)
        return summary

    def _middleware_candidates(self, category: str) -> list[str]:
        stack = self.runtime_inventory.get("middleware", {}).get("stack", [])
        names: list[str] = []
        for entry in stack:
            name = str(entry.get("name") or "")
            hooks = [str(hook) for hook in entry.get("hooks") or []]
            lowered = name.lower()
            if category == "skills" and "skill" in lowered:
                names.append(name)
            elif category == "state" and (
                "todo" in lowered or "memory" in lowered or "patchtoolcalls" in lowered
            ):
                names.append(name)
            elif category == "memory" and "memory" in lowered:
                names.append(name)
            elif category == "subagent" and "subagent" in lowered:
                names.append(name)
            elif category == "model_input" and (
                any("model" in hook for hook in hooks)
                or "memory" in lowered
                or "skill" in lowered
                or "subagent" in lowered
                or "prompt" in lowered
                or "summarization" in lowered
                or "patchtoolcalls" in lowered
            ):
                names.append(name)
        return names

    def add_middleware_effect(
        self,
        *,
        category: str,
        title: str,
        hook: str | None = None,
        middleware: list[str] | None = None,
        before: Any = None,
        after: Any = None,
        diff: dict[str, Any] | None = None,
        evidence: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        effect_id = self._new_id("effect")
        effect_metadata = dict(metadata) if metadata else {}
        effect_metadata.update(
            {
                "category": category,
                "hook": hook,
                "middleware": middleware or [],
            }
        )
        effect = {
            "id": effect_id,
            "category": category,
            "title": title,
            "hook": hook,
            "middleware": middleware or [],
            "before": TraceSpan._serialize(before),
            "after": TraceSpan._serialize(after),
            "diff": diff or {},
            "evidence": evidence or [],
            "metadata": effect_metadata,
            "created_at": time.time(),
        }
        self._middleware_effects.append(effect)
        source_span_id = self.add_custom_span(
            title,
            {
                "before": effect["before"],
                "after": effect["after"],
                "diff": effect["diff"],
                "evidence": effect["evidence"],
            },
            span_type="middleware",
            metadata=effect_metadata,
        )
        effect["metadata"]["source_span_id"] = source_span_id
        normalized_hook = hook or self._hook_for_category(category)
        if not self._is_hook_level_boundary_effect(category=category, title=title) and not self._has_direct_middleware_invocation(
            normalized_hook, middleware or []
        ):
            self.add_middleware_invocation(
                hook=normalized_hook,
                middleware=middleware or [],
                category=category,
                title=title,
                status=self._middleware_status(diff=diff, before=before, after=after),
                evidence=evidence or [],
                before=before,
                after=after,
                diff=diff or {},
                metadata={
                    **effect_metadata,
                    "effect_id": effect_id,
                    "source_span_id": source_span_id,
                    "coverage": "inferred",
                    "semantic_order": self._hook_semantic_order(normalized_hook),
                },
            )
        return source_span_id

    @staticmethod
    def _hook_semantic_order(hook: str | None) -> int:
        order = {
            "before_agent": 10,
            "before_model": 20,
            "wrap_model_call": 30,
            "after_model": 40,
            "wrap_tool_call": 50,
            "after_agent": 60,
        }
        return order.get(str(hook or ""), 99)

    @staticmethod
    def _is_hook_level_boundary_effect(*, category: str, title: str) -> bool:
        return category == "model_input" and title == "Model input boundary"

    def _has_direct_middleware_invocation(self, hook: str | None, middleware: list[str]) -> bool:
        if not hook or not middleware:
            return False
        middleware_names = {str(item) for item in middleware}
        for invocation in self._middleware_invocations:
            if invocation.get("hook") != hook:
                continue
            if invocation.get("metadata", {}).get("coverage") != "direct":
                continue
            invocation_names = {str(item) for item in invocation.get("middleware") or []}
            if middleware_names & invocation_names:
                return True
        return False

    def add_middleware_invocation(
        self,
        *,
        hook: str | None,
        middleware: list[str] | None = None,
        category: str | None = None,
        title: str | None = None,
        status: str = "read",
        evidence: list[str] | None = None,
        before: Any = None,
        after: Any = None,
        diff: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        flow_ref: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a precise middleware hook invocation.

        This is intentionally separate from spans: spans describe the runtime
        tree, while invocations describe which LangChain/DeepAgents middleware
        hook fired during this query. The frontend can render hook counts and
        locate the related span/effect without guessing.
        """

        normalized_hook = str(hook or "unknown")
        invocation_index = self._middleware_invocation_counts.get(normalized_hook, 0)
        self._middleware_invocation_counts[normalized_hook] = invocation_index + 1
        invocation_metadata = dict(metadata) if metadata else {}
        invocation_metadata.setdefault("semantic_order", self._hook_semantic_order(normalized_hook))
        sequence = invocation_metadata.get("event_order")
        if not isinstance(sequence, int):
            sequence = self._next_event_order()
            invocation_metadata["event_order"] = sequence
        invocation = {
            "id": self._new_id("middleware-invocation"),
            "hook": normalized_hook,
            "middleware": middleware or [],
            "category": category,
            "title": title or normalized_hook,
            "invocation_index": invocation_index,
            "sequence": sequence,
            "status": status,
            "evidence": evidence or [],
            "before": TraceSpan._serialize(before),
            "after": TraceSpan._serialize(after),
            "diff": diff or {},
            "metadata": invocation_metadata,
            "flow_ref": flow_ref or {},
            "created_at": time.time(),
        }
        self._middleware_invocations.append(invocation)
        self._emit(
            "middleware_invocation",
            {
                "invocation": invocation,
                "trace_id": self.trace_id,
                "query_id": self.query_id,
                "session_id": self.session_id,
            },
        )
        return invocation

    @staticmethod
    def _hook_for_category(category: str) -> str:
        if category == "skills":
            return "before_agent"
        if category == "model_input":
            return "before_model"
        if category == "state":
            return "after_model"
        if category == "context":
            return "before_model"
        return "unknown"

    @staticmethod
    def _middleware_status(*, diff: dict[str, Any] | None, before: Any, after: Any) -> str:
        if diff:
            return "changed"
        if before != after:
            return "changed"
        return "read"

    @staticmethod
    def _message_preview(message: Any) -> dict[str, Any]:
        role = getattr(message, "type", None) or getattr(message, "role", None)
        if role is None:
            role = type(message).__name__
        content = getattr(message, "content", "")
        if isinstance(message, dict):
            content = message.get("content", content)
            role = message.get("role", role)
        content_text = TraceCollector._message_content_text(content)
        tool_calls = getattr(message, "tool_calls", None)
        if isinstance(message, dict):
            tool_calls = message.get("tool_calls", tool_calls)
        name = getattr(message, "name", None)
        if isinstance(message, dict):
            name = message.get("name", name)
        return {
            "role": str(role),
            "name": name,
            "chars": len(content_text),
            "estimated_tokens": max(1, len(content_text) // 4) if content_text else 0,
            "tool_call_count": len(tool_calls) if isinstance(tool_calls, list) else 0,
            "tool_calls": TraceCollector._message_tool_calls(tool_calls),
            "content": content_text,
            "preview": content_text[:600],
        }

    @staticmethod
    def _message_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
        if not isinstance(tool_calls, list):
            return []
        return [TraceCollector._tool_call_preview(tool_call) for tool_call in tool_calls]

    @staticmethod
    def _tool_call_preview(tool_call: Any) -> dict[str, Any]:
        if isinstance(tool_call, dict):
            name = tool_call.get("name") or tool_call.get("tool") or tool_call.get("function", {}).get("name")
            args = (
                tool_call.get("args")
                or tool_call.get("arguments")
                or tool_call.get("input")
                or tool_call.get("function", {}).get("arguments")
            )
            call_id = tool_call.get("id") or tool_call.get("tool_call_id")
        else:
            name = getattr(tool_call, "name", None) or getattr(tool_call, "tool", None)
            args = getattr(tool_call, "args", None) or getattr(tool_call, "arguments", None)
            call_id = getattr(tool_call, "id", None) or getattr(tool_call, "tool_call_id", None)
        return {
            "id": str(call_id or ""),
            "name": str(name or "tool"),
            "args": TraceSpan._serialize(args),
        }

    @staticmethod
    def _message_content_text(content: Any) -> str:
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "image_url":
                        parts.append(TraceCollector._image_part_summary(part))
                        continue
                    text = part.get("text") or part.get("content")
                    parts.append(str(text if text is not None else part))
                else:
                    parts.append(str(part))
            return "\n".join(parts)
        if isinstance(content, dict):
            text = content.get("text") or content.get("content")
            return str(text if text is not None else content)
        text = str(content or "")
        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                parsed = ast.literal_eval(stripped)
                if parsed is not content:
                    return TraceCollector._message_content_text(parsed)
            except Exception:
                return text
        return text

    @staticmethod
    def _image_part_summary(part: dict[str, Any]) -> str:
        image_url = part.get("image_url") or {}
        if isinstance(image_url, dict):
            url = str(image_url.get("url") or "")
        else:
            url = str(image_url or "")
        if url.startswith("data:"):
            header, _, payload = url.partition(",")
            mime = header.removeprefix("data:").split(";", 1)[0] or "image"
            return f"[image_url {mime} base64_chars={len(payload)}]"
        if url:
            return "[image_url remote]"
        return "[image_url]"

    def start_tool_span(
        self,
        name: str,
        tool_call_id: str,
        input_data: Any = None,
        span_type: str = "tool",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        span_id = self._new_id("tool")
        parent = self._current_parent()
        span_metadata = {"tool_call_id": tool_call_id}
        if metadata:
            span_metadata.update(metadata)
        span_metadata.setdefault("event_order", self._next_event_order())
        span = TraceSpan(
            span_id=span_id,
            parent_id=parent.id,
            span_type=span_type,
            name=name,
            started_at=time.time(),
            input_data=input_data,
            metadata=span_metadata,
        )
        parent.children.append(span)
        self._active_spans[span_id] = span
        self._tool_span_by_call_id[tool_call_id] = span
        self._span_stack.append(span)
        self._emit("trace_span_start", self._event_payload(span))
        return span_id

    def finish_tool_span(
        self,
        tool_call_id: str,
        output: Any = None,
        is_error: bool = False,
    ) -> None:
        span = self._tool_span_by_call_id.pop(tool_call_id, None)
        if span is None:
            return
        # Remove from stack if still present.
        if span in self._span_stack:
            self._span_stack.remove(span)
        span.completed_at = time.time()
        span.output = output
        span.status = "error" if is_error else "completed"
        self._emit("trace_span_end", self._event_payload(span))

    def add_reasoning_span(self, content: str) -> str:
        # Merge consecutive reasoning into a single span to avoid noise.
        parent = self._current_parent()
        if parent.children and parent.children[-1].type == "reasoning":
            span = parent.children[-1]
            span.output = (span.output or "") + content
            span.completed_at = time.time()
            return span.id

        span_id = self._new_id("reasoning")
        span = TraceSpan(
            span_id=span_id,
            parent_id=parent.id,
            span_type="reasoning",
            name="reasoning",
            started_at=time.time(),
            input_data=None,
            metadata={"event_order": self._next_event_order()},
        )
        span.output = content
        span.completed_at = time.time()
        span.status = "completed"
        parent.children.append(span)
        self._active_spans[span_id] = span
        self._emit("trace_span_start", self._event_payload(span))
        self._emit("trace_span_end", self._event_payload(span))
        return span_id

    def add_todo_span(
        self,
        todos: list[dict[str, Any]],
        diff: dict[str, Any] | None = None,
    ) -> str:
        span_id = self._new_id("todo")
        parent = self._current_parent()
        span = TraceSpan(
            span_id=span_id,
            parent_id=parent.id,
            span_type="todo",
            name="todos_updated",
            started_at=time.time(),
            metadata={
                "event_order": self._next_event_order(),
                "harness": {
                    "mechanism": "feature_list",
                    "pillars": [
                        {"name": "context_engineering", "role": "primary"},
                        {"name": "architectural_constraints", "role": "supporting"},
                        {"name": "garbage_collection", "role": "supporting"},
                    ],
                },
                "todo_diff": diff or {"added": [], "updated": [], "removed": []},
            },
        )
        span.output = todos
        span.completed_at = time.time()
        span.status = "completed"
        parent.children.append(span)
        self._active_spans[span_id] = span
        self._emit("trace_span_start", self._event_payload(span))
        self._emit("trace_span_end", self._event_payload(span))
        todo_diff = diff or {"added": [], "updated": [], "removed": []}
        self.add_middleware_effect(
            category="state",
            title="Todo state updated",
            hook="after_model",
            middleware=self._middleware_candidates("state"),
            before=None,
            after={
                "todo_count": len(todos),
                "todos": todos,
            },
            diff={
                "added": len(todo_diff.get("added") or []),
                "updated": len(todo_diff.get("updated") or []),
                "removed": len(todo_diff.get("removed") or []),
            },
            evidence=[
                f"{len(todo_diff.get('added') or [])} added",
                f"{len(todo_diff.get('updated') or [])} updated",
                f"{len(todo_diff.get('removed') or [])} removed",
            ],
            metadata={"source_span_id": span_id},
        )
        return span_id

    def add_custom_span(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        *,
        span_type: str = "custom",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        span_id = self._new_id("custom")
        parent = self._current_parent()
        span_metadata = dict(metadata) if metadata else {}
        span_metadata.setdefault("event_order", self._next_event_order())
        span = TraceSpan(
            span_id=span_id,
            parent_id=parent.id,
            span_type=span_type,
            name=name,
            started_at=time.time(),
            metadata=span_metadata,
        )
        span.output = payload
        span.completed_at = time.time()
        span.status = "completed"
        parent.children.append(span)
        self._active_spans[span_id] = span
        self._emit("trace_span_start", self._event_payload(span))
        self._emit("trace_span_end", self._event_payload(span))
        return span_id

    def add_rag_span(
        self,
        stage: str,
        payload: dict[str, Any] | None = None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Record a white-box RAG step under the active tool span."""

        span_metadata = {
            "rag_stage": stage,
            "harness": {
                "mechanism": "context_management",
                "pillars": [
                    {"name": "context_engineering", "role": "primary"},
                    {"name": "architectural_constraints", "role": "supporting"},
                ],
            },
        }
        if metadata:
            span_metadata.update(metadata)
        return self.add_custom_span(
            f"rag.{stage}",
            payload or {},
            span_type="rag",
            metadata=span_metadata,
        )

    def add_graph_node_span(self, node: str) -> str:
        lower = node.lower()
        node_kind = "graph"
        if "middleware" in lower or ".before_" in lower or ".after_" in lower:
            node_kind = "middleware"
        elif "memory" in lower:
            node_kind = "memory"
        elif "skill" in lower:
            node_kind = "skill"
        elif "tool" in lower:
            node_kind = "tool"
        return self.add_custom_span(
            f"graph.{node}",
            {"node": node},
            span_type="graph",
            metadata={"graph_node": node, "graph_node_kind": node_kind},
        )

    def _flatten_spans(self, span: TraceSpan) -> list[dict[str, Any]]:
        result = [span.to_dict()]
        for child in span.children:
            result.extend(self._flatten_spans(child))
        return result

    def finish(self, status: str = "completed", error: str | None = None) -> dict[str, Any]:
        self.completed_at = time.time()
        self.status = status
        self.root.completed_at = self.completed_at
        self.root.status = status
        if error:
            self.root.metadata["error"] = error
        self._emit("trace_span_end", self._event_payload(self.root))
        return {
            "trace_id": self.trace_id,
            "query_id": self.query_id,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "runtime_inventory": self.runtime_inventory,
            "middleware_effects": self._middleware_effects,
            "middleware_invocations": self._middleware_invocations,
            "hook_boundary_snapshots": self._hook_boundary_snapshots,
            "spans": self._flatten_spans(self.root),
        }
