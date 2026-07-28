from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from harness.goal_turn_router import GoalTurnRouter
from harness.models import GoalTurnIntent

GOAL = {
    "goal_id": "goal-1",
    "objective": "刷新 2021-2026 产品配置报告并完成 E2E",
    "status": "active",
    "objective_revision": 1,
    "round": 1,
    "max_rounds": 8,
}


@pytest.mark.parametrize(
    "message",
    [
        "总结一下已经完成的工作",
        "现在做到哪了？",
        "列出剩余工作",
        "为什么刚才验收失败？",
        "不要继续，先总结进度",
    ],
)
def test_explicit_progress_questions_are_read_only(message: str) -> None:
    decision = GoalTurnRouter.deterministic(message, goal_id="goal-1")

    assert decision is not None
    assert decision.intent == GoalTurnIntent.INSPECT_GOAL
    assert decision.target_goal_id == "goal-1"


@pytest.mark.parametrize(
    "message",
    ["继续执行", "从中断处恢复", "把剩余工作完成", "接着做", "开始"],
)
def test_explicit_continuations_execute_goal(message: str) -> None:
    decision = GoalTurnRouter.deterministic(message, goal_id="goal-1")

    assert decision is not None
    assert decision.intent == GoalTurnIntent.CONTINUE_GOAL


class _FakeModel:
    def __init__(self, payload: dict):
        self.payload = payload

    async def ainvoke(self, _messages):
        return AIMessage(content=json.dumps(self.payload, ensure_ascii=False))


@pytest.mark.asyncio
async def test_semantic_revision_returns_complete_revised_objective() -> None:
    decision = await GoalTurnRouter.classify(
        message="时间范围改成 2022-2026，其他要求不变",
        goal=GOAL,
        model=_FakeModel(
            {
                "intent": "revise_goal",
                "confidence": 0.96,
                "reason": "explicit_scope_change",
                "control_action": None,
                "revised_objective": "刷新 2022-2026 产品配置报告并完成 E2E",
            }
        ),
    )

    assert decision.intent == GoalTurnIntent.REVISE_GOAL
    assert decision.revised_objective == (
        "刷新 2021-2026 产品配置报告并完成 E2E\n\n"
        "用户追加约束（优先于上文冲突项）：时间范围改成 2022-2026，其他要求不变"
    )


@pytest.mark.asyncio
async def test_low_confidence_never_defaults_to_execution() -> None:
    decision = await GoalTurnRouter.classify(
        message="那这个呢",
        goal=GOAL,
        model=_FakeModel(
            {
                "intent": "continue_goal",
                "confidence": 0.4,
                "reason": "ambiguous",
                "control_action": None,
                "revised_objective": None,
            }
        ),
    )

    assert decision.intent == GoalTurnIntent.CLARIFY
    assert decision.classifier == "fallback"


@pytest.mark.asyncio
async def test_mixed_summary_then_continue_uses_semantic_ordering() -> None:
    assert GoalTurnRouter.deterministic(
        "先总结当前进度，然后继续执行任务",
        goal_id="goal-1",
    ) is None

    decision = await GoalTurnRouter.classify(
        message="先总结当前进度，然后继续执行任务",
        goal=GOAL,
        model=_FakeModel(
            {
                "intent": "continue_goal",
                "confidence": 0.91,
                "reason": "summary_then_explicit_execution",
                "control_action": None,
                "revised_objective": None,
            }
        ),
    )

    assert decision.intent == GoalTurnIntent.CONTINUE_GOAL
    assert decision.classifier == "llm"


@pytest.mark.asyncio
async def test_interrupted_copy_correction_revises_goal_without_clarifying() -> None:
    recent_context = {
        "latest_run": {
            "status": "cancelled",
            "outcome": "cancelled",
            "error": "client_cancelled",
        },
        "recent_tools": [
            {
                "tool": "copy_file",
                "target": "/report/vendor/echarts.min.js",
                "status": "running",
                "is_error": "false",
            }
        ],
    }

    decision = await GoalTurnRouter.classify(
        message="不要复制这种依赖",
        goal=GOAL,
        model=None,
        recent_execution_context=recent_context,
    )

    assert decision.intent == GoalTurnIntent.REVISE_GOAL
    assert decision.classifier == "fallback_contextual"
    assert "用户追加约束（优先于上文冲突项）：不要复制这种依赖" in (
        decision.revised_objective or ""
    )
    assert "copy_file（/report/vendor/echarts.min.js）" in (
        decision.revised_objective or ""
    )


@pytest.mark.asyncio
async def test_interrupted_workspace_plan_can_be_redirected_to_external_directory() -> None:
    recent_context = {
        "latest_run": {
            "status": "cancelled",
            "outcome": "cancelled",
            "error": "client_cancelled",
        },
        "recent_tools": [],
        "recent_assistant_actions": [
            {
                "content": "现在复制模板到工作区并应用所有 V3 变更。",
                "status": "cancelled",
                "interrupted": "true",
            }
        ],
    }

    decision = await GoalTurnRouter.classify(
        message="直接在外部目录也可以完成工作",
        goal=GOAL,
        model=None,
        recent_execution_context=recent_context,
    )

    assert decision.intent == GoalTurnIntent.REVISE_GOAL
    assert decision.classifier == "fallback_contextual"
    assert decision.reason.startswith("contextual_execution_correction:")
    assert "用户追加约束（优先于上文冲突项）：直接在外部目录也可以完成工作" in (
        decision.revised_objective or ""
    )


@pytest.mark.asyncio
async def test_question_about_a_possible_constraint_still_clarifies_without_model() -> None:
    decision = await GoalTurnRouter.classify(
        message="这个依赖要不要复制？",
        goal=GOAL,
        model=None,
        recent_execution_context={
            "recent_tools": [{"tool": "copy_file", "target": "echarts.min.js"}]
        },
    )

    assert decision.intent == GoalTurnIntent.CLARIFY


class _CapturingModel(_FakeModel):
    def __init__(self, payload: dict):
        super().__init__(payload)
        self.messages = []

    async def ainvoke(self, messages):
        self.messages = messages
        return await super().ainvoke(messages)


@pytest.mark.asyncio
async def test_semantic_router_receives_recent_execution_context() -> None:
    model = _CapturingModel(
        {
            "intent": "revise_goal",
            "confidence": 0.95,
            "reason": "correction_to_recent_copy",
            "control_action": None,
            "revised_objective": None,
        }
    )
    recent_context = {
        "latest_run": {"status": "cancelled", "error": "client_cancelled"},
        "recent_tools": [{"tool": "copy_file", "target": "echarts.min.js"}],
    }

    decision = await GoalTurnRouter.classify(
        message="这个别这样处理",
        goal=GOAL,
        model=model,
        recent_execution_context=recent_context,
    )

    assert decision.intent == GoalTurnIntent.REVISE_GOAL
    prompt = "\n".join(str(message.content) for message in model.messages)
    assert "copy_file" in prompt
    assert "echarts.min.js" in prompt
    assert "这个别这样处理" in (decision.revised_objective or "")
