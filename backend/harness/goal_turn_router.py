"""Route one user turn relative to a standing Goal before creating a Run.

The router is deliberately separate from task-profile classification.  It
decides lifecycle ownership (observe, execute, revise, control, or detach),
while TaskProfileClassifier decides capabilities and verification for the Run
that is eventually created.
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, model_validator

from harness.models import GoalTurnIntent


class GoalTurnDecision(BaseModel):
    intent: GoalTurnIntent
    target_goal_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""
    classifier: str
    control_action: str | None = None
    revised_objective: str | None = None

    @model_validator(mode="after")
    def validate_intent_payload(self) -> GoalTurnDecision:
        if self.intent == GoalTurnIntent.CONTROL_GOAL and self.control_action not in {
            "pause",
            "resume",
            "cancel",
        }:
            raise ValueError("control_goal requires pause, resume, or cancel")
        return self


_INSPECT_PATTERNS = (
    r"(?:不要|先别|无需|不用).{0,8}(?:继续|执行|推进).{0,16}(?:总结|进度|做到哪|剩余|todo|状态)",
    r"(?:总结|汇总|概括).{0,16}(?:已完成|完成的|进度|工作|结果|情况)",
    r"(?:现在|当前).{0,8}(?:做到哪|进度|状态|完成了什么|完成多少)",
    r"(?:列出|展示|看看|查看|告诉我).{0,12}(?:剩余|未完成|todo|进度|状态|已完成)",
    r"(?:还剩|剩下).{0,10}(?:什么|哪些|多少|任务|工作)",
    r"(?:为什么|为何).{0,12}(?:失败|中断|停止|验收)",
    r"\b(?:summari[sz]e|status|progress|what(?:'s| is) left|remaining work)\b",
)
_CONTINUE_PATTERNS = (
    r"^(?:请)?(?:开始|开始执行|开始处理|开始吧)[。.!！?？\s]*$",
    r"^(?:请)?(?:继续|接着|恢复|从中断处恢复|继续执行|继续处理|继续完成|接着做|接着处理)(?:吧|一下|任务|工作|这个任务|这个 goal)?[。.!！?？\s]*$",
    r"(?:把|将).{0,12}(?:剩余|未完成).{0,8}(?:做完|完成|继续处理|推进)",
    r"(?:继续|恢复|接着).{0,12}(?:执行|推进|完成|开发|任务|goal)",
    r"\b(?:continue|resume|pick\s+up|carry\s+on|finish\s+the\s+rest)\b",
)
_CONTROL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("cancel", r"^(?:请)?(?:取消|终止|放弃)(?:这个|当前)?(?:目标|goal)[。.!！?？\s]*$"),
    (
        "pause",
        r"^(?:请)?(?:(?:暂停)(?:这个|当前)?(?:目标|goal)|"
        r"(?:不要|别|先别)(?:再)?(?:继续|执行|推进|处理|做)(?:了|啦|任务|这个任务)?)"
        r"[。.!！?？\s]*$",
    ),
    ("resume", r"^(?:请)?(?:恢复)(?:这个|当前)?(?:目标|goal)[。.!！?？\s]*$"),
)
_REVISE_HINT = re.compile(
    r"(?:改成|改为|改用|调整为|范围改|目标改|增加|补充|去掉|移除|"
    r"不要|别再|禁止|避免|无需|不用|必须|务必|只要|继续使用|继续复用|复用)",
    flags=re.IGNORECASE,
)
_CONTEXTUAL_EXECUTION_CORRECTION_HINT = re.compile(
    r"(?:"
    r"(?:直接|改在|放到|写到|保存到|输出到|也?可以).{0,20}"
    r"(?:外部|原来|原|目标|当前|这个)?(?:目录|路径|文件夹).{0,20}"
    r"(?:完成|处理|写|生成|保存|输出|交付|即可|就行|也行)"
    r"|"
    r"(?:文件|产物|报告|页面).{0,16}(?:直接|改为|改在|放到|写到|保存到|输出到)"
    r".{0,20}(?:目录|路径|文件夹)"
    r")",
    flags=re.IGNORECASE,
)

_ROUTER_PROMPT = """你是 Goal 回合控制路由器，只判断当前用户消息如何关联 standing Goal，不执行任务。

可选 intent：
- inspect_goal：只询问已有进度、结果、Todo、证据、失败原因或要求总结；没有新的交付授权。
- continue_goal：明确要求继续、恢复、推进或完成 standing Goal。
- revise_goal：明确修改 standing Goal 的范围、约束或交付物，并希望按新目标执行。
- control_goal：明确暂停、恢复或取消 Goal。
- standalone_task：当前请求与 standing Goal 无关，是独立新任务。
- clarify：无法可靠判断是否应该修改状态。

规则：
1. 当前用户消息优先，active Goal 不是隐式继续命令。
2. “不要继续，先总结”必须是 inspect_goal。
3. recent_execution_context 是解析“这种依赖”“不要这样”“换一种方式”等省略表达的权威上下文。
   执行过程中的命令式纠偏或约束属于 revise_goal，并隐含继续执行；不是 inspect_goal，也不是 clarify。
4. 用户刚中断执行后给出纠偏时，应结合最近工具动作判断；不能因为消息短就忽略 standing Goal。
5. 低置信度必须返回 clarify，不能默认 continue_goal。
6. revise_goal 只负责识别修订意图；服务端会保留原目标并原样追加用户约束，
   因此 revised_objective 可以为 null，不能擅自重写或删减目标。
7. control_goal 的 control_action 只能是 pause、resume 或 cancel。

只返回 JSON：
{"intent":"inspect_goal","confidence":0.98,"reason":"progress_question","control_action":null,"revised_objective":null}
"""


class GoalTurnRouter:
    """Hybrid high-precision rules plus bounded semantic classification."""

    @staticmethod
    def deterministic(
        message: str,
        *,
        goal_id: str,
    ) -> GoalTurnDecision | None:
        normalized = " ".join(str(message or "").strip().split())
        if not normalized:
            return GoalTurnDecision(
                intent=GoalTurnIntent.CLARIFY,
                target_goal_id=goal_id,
                confidence=1.0,
                reason="empty_message",
                classifier="deterministic",
            )
        inspection_match = any(
            re.search(pattern, normalized, flags=re.IGNORECASE)
            for pattern in _INSPECT_PATTERNS
        )
        continuation_match = any(
            re.search(pattern, normalized, flags=re.IGNORECASE)
            for pattern in _CONTINUE_PATTERNS
        )
        # Mixed requests such as “总结后继续执行” contain both read and write
        # intent. They need semantic ordering instead of whichever regex is
        # checked first. Explicit negative inspection remains deterministic.
        if inspection_match and continuation_match:
            return None
        if inspection_match:
            return GoalTurnDecision(
                intent=GoalTurnIntent.INSPECT_GOAL,
                target_goal_id=goal_id,
                confidence=0.99,
                reason="explicit_goal_inspection",
                classifier="deterministic",
            )
        for action, pattern in _CONTROL_PATTERNS:
            if re.search(pattern, normalized, flags=re.IGNORECASE):
                return GoalTurnDecision(
                    intent=GoalTurnIntent.CONTROL_GOAL,
                    target_goal_id=goal_id,
                    confidence=0.99,
                    reason=f"explicit_goal_{action}",
                    classifier="deterministic",
                    control_action=action,
                )
        if continuation_match:
            return GoalTurnDecision(
                intent=GoalTurnIntent.CONTINUE_GOAL,
                target_goal_id=goal_id,
                confidence=0.99,
                reason="explicit_goal_continuation",
                classifier="deterministic",
            )
        # Scope changes and execution corrections need semantic context.  They
        # intentionally do not become an ever-growing regex decision tree.
        return None

    @classmethod
    async def classify(
        cls,
        *,
        message: str,
        goal: dict[str, Any],
        model: Any | None,
        recent_execution_context: dict[str, Any] | None = None,
        confidence_threshold: float = 0.72,
    ) -> GoalTurnDecision:
        goal_id = str(goal.get("goal_id") or "")
        deterministic = cls.deterministic(message, goal_id=goal_id)
        if deterministic is not None:
            return deterministic
        if model is None:
            return cls.contextual_fallback(
                message=message,
                goal=goal,
                recent_execution_context=recent_execution_context,
                reason="model_unavailable",
            )
        goal_summary = {
            "goal_id": goal_id,
            "objective": str(goal.get("objective") or "")[:20_000],
            "status": str(goal.get("status") or ""),
            "revision": int(goal.get("objective_revision") or 1),
            "round": int(goal.get("round") or 0),
            "max_rounds": int(goal.get("max_rounds") or 0),
            "gaps": list(goal.get("gaps") or [])[:20],
            "recent_execution_context": recent_execution_context or {},
        }
        try:
            response = await model.ainvoke(
                [
                    SystemMessage(content=_ROUTER_PROMPT),
                    HumanMessage(
                        content=(
                            "<current_user_message>\n"
                            + str(message or "")
                            + "\n</current_user_message>\n"
                            + "<standing_goal>\n"
                            + json.dumps(goal_summary, ensure_ascii=False, sort_keys=True)
                            + "\n</standing_goal>"
                        )
                    ),
                ]
            )
            payload = cls._parse_payload(getattr(response, "content", response))
            decision = GoalTurnDecision(
                **payload,
                target_goal_id=goal_id,
                classifier="llm",
            )
        except Exception:
            return cls.contextual_fallback(
                message=message,
                goal=goal,
                recent_execution_context=recent_execution_context,
                reason="classifier_error",
            )
        if decision.confidence < confidence_threshold:
            return cls.contextual_fallback(
                message=message,
                goal=goal,
                recent_execution_context=recent_execution_context,
                reason="low_confidence",
            )
        if decision.intent == GoalTurnIntent.REVISE_GOAL:
            decision.revised_objective = cls._append_revision(
                objective=str(goal.get("objective") or ""),
                message=message,
                recent_execution_context=recent_execution_context,
            )
        return decision

    @classmethod
    def contextual_fallback(
        cls,
        *,
        message: str,
        goal: dict[str, Any],
        recent_execution_context: dict[str, Any] | None,
        reason: str,
    ) -> GoalTurnDecision:
        """Fail safely without turning clear execution corrections into a quiz."""

        goal_id = str(goal.get("goal_id") or "")
        normalized = " ".join(str(message or "").strip().split())
        looks_interrogative = bool(
            re.search(
                r"(?:要不要|是否|能不能|可不可以|可以吗|行吗|吗[。.!！?？\s]*$)",
                normalized,
                flags=re.IGNORECASE,
            )
        )
        context = (
            recent_execution_context
            if isinstance(recent_execution_context, dict)
            else {}
        )
        latest_run = (
            context.get("latest_run")
            if isinstance(context.get("latest_run"), dict)
            else {}
        )
        has_recent_execution_anchor = bool(
            context.get("recent_tools")
            or context.get("recent_assistant_actions")
            or str(latest_run.get("status") or "")
            in {"running", "cancelled", "interrupted"}
        )
        explicit_revision = bool(_REVISE_HINT.search(normalized))
        contextual_correction = bool(
            has_recent_execution_anchor
            and _CONTEXTUAL_EXECUTION_CORRECTION_HINT.search(normalized)
        )
        if (explicit_revision or contextual_correction) and not looks_interrogative:
            return GoalTurnDecision(
                intent=GoalTurnIntent.REVISE_GOAL,
                target_goal_id=goal_id,
                confidence=0.8,
                reason=(
                    f"contextual_execution_correction:{reason}"
                    if contextual_correction and not explicit_revision
                    else f"contextual_execution_constraint:{reason}"
                ),
                classifier="fallback_contextual",
                revised_objective=cls._append_revision(
                    objective=str(goal.get("objective") or ""),
                    message=normalized,
                    recent_execution_context=recent_execution_context,
                ),
            )
        return cls._safe_fallback(goal_id, reason)

    @staticmethod
    def _append_revision(
        *,
        objective: str,
        message: str,
        recent_execution_context: dict[str, Any] | None,
    ) -> str:
        """Keep the original Goal and the user's correction verbatim."""

        context_note = ""
        recent_tools = (
            recent_execution_context.get("recent_tools")
            if isinstance(recent_execution_context, dict)
            else None
        )
        if isinstance(recent_tools, list) and recent_tools:
            latest = recent_tools[-1]
            if isinstance(latest, dict):
                tool_name = str(latest.get("tool") or "").strip()
                target = str(latest.get("target") or "").strip()
                if tool_name or target:
                    context_note = (
                        f"\n该约束针对中断前最近动作：{tool_name}"
                        + (f"（{target}）" if target else "")
                    )
        return (
            str(objective).rstrip()
            + "\n\n用户追加约束（优先于上文冲突项）："
            + " ".join(str(message or "").strip().split())
            + context_note
        )

    @staticmethod
    def _parse_payload(content: Any) -> dict[str, Any]:
        if isinstance(content, list):
            content = "".join(
                str(item.get("text") or item.get("content") or "")
                for item in content
                if isinstance(item, dict)
            )
        text = str(content or "").strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)
        else:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                text = text[start : end + 1]
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("Goal turn classifier returned a non-object")
        return payload

    @staticmethod
    def _safe_fallback(goal_id: str, reason: str) -> GoalTurnDecision:
        return GoalTurnDecision(
            intent=GoalTurnIntent.CLARIFY,
            target_goal_id=goal_id,
            confidence=0.0,
            reason=reason,
            classifier="fallback",
        )


__all__ = ["GoalTurnDecision", "GoalTurnRouter"]
