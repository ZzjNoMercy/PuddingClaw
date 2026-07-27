"""Generic structured HITL tool for material user decisions."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal, Type

from langchain_core.tools import BaseTool, InjectedToolCallId
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field, model_validator

from graph.user_input_resume import user_input_resume_registry


_SENSITIVE_PROMPT = re.compile(
    r"(?:password|passcode|api\s*key|access\s*token|secret|密码|口令|密钥|验证码)",
    re.IGNORECASE,
)


class UserInputOption(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][\w.-]{0,63}$")
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=240)
    recommended: bool = False


class UserInputQuestion(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][\w.-]{0,63}$")
    prompt: str = Field(min_length=1, max_length=300)
    type: Literal["single_select", "multi_select", "text"]
    options: list[UserInputOption] = Field(default_factory=list, max_length=12)
    required: bool = True
    allow_other: bool = False
    min_selections: int = Field(default=0, ge=0, le=12)
    max_selections: int | None = Field(default=None, ge=1, le=12)
    max_length: int = Field(default=1000, ge=1, le=4000)

    @model_validator(mode="after")
    def validate_shape(self):
        if self.type in {"single_select", "multi_select"} and len(self.options) < 2:
            raise ValueError("选择题至少需要两个选项")
        if self.type == "text" and self.options:
            raise ValueError("文本题不能包含 options")
        if self.type == "single_select" and self.max_selections not in {None, 1}:
            raise ValueError("单选题 max_selections 只能为 1")
        if self.type == "single_select" and self.min_selections not in {0, 1}:
            raise ValueError("单选题 min_selections 只能为 0 或 1")
        option_ids = [item.id for item in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("同一问题的 option id 必须唯一")
        if self.type == "single_select" and sum(item.recommended for item in self.options) > 1:
            raise ValueError("单选题最多只能有一个推荐项")
        recommended_count = sum(item.recommended for item in self.options)
        if (
            self.type == "multi_select"
            and self.max_selections is not None
            and recommended_count > self.max_selections
        ):
            raise ValueError("多选题的推荐项数量不能超过 max_selections")
        if self.max_selections is not None and self.min_selections > self.max_selections:
            raise ValueError("min_selections 不能大于 max_selections")
        return self


class RequestUserInputArgs(BaseModel):
    title: str = Field(default="需要你的选择", min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=500)
    questions: list[UserInputQuestion] = Field(min_length=1, max_length=3)
    allow_agent_decide: bool = True
    tool_call_id: Annotated[str, InjectedToolCallId]

    @model_validator(mode="after")
    def validate_questions(self):
        ids = [item.id for item in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("question id 必须唯一")
        combined = " ".join([self.title, self.reason, *(item.prompt for item in self.questions)])
        if _SENSITIVE_PROMPT.search(combined):
            raise ValueError("request_user_input 不得索取密码、密钥、验证码或其他秘密")
        return self


class RequestUserInputTool(BaseTool):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = "request_user_input"
    description: str = (
        "Pause the current Run and ask 1-3 structured questions only when a material decision "
        "cannot be safely inferred. Do not use for permissions, destructive-action approval, "
        "secrets, or choices the available tools/data can resolve. Prefer a recommended default "
        "and continue autonomously for low-impact preferences."
    )
    args_schema: Type[BaseModel] = RequestUserInputArgs
    risk_level: str = "safe"
    session_id: str = ""
    query_id: str = ""
    run_id: str = ""
    goal_id: str = ""
    goal_revision: int | None = None

    def _run(self, **kwargs: Any) -> str:
        raise RuntimeError("Use async execution for user-input HITL")

    async def _arun(self, **kwargs: Any) -> str:
        if not self.session_id or not self.query_id or not self.run_id:
            return "❌ 无法请求用户输入：缺少当前 Run 的可信绑定。"
        tool_call_id = str(kwargs.pop("tool_call_id", "") or "")
        if not tool_call_id:
            return "❌ 无法请求用户输入：缺少 tool_call_id。"
        questions = [
            item.model_dump(mode="json") if isinstance(item, BaseModel) else dict(item)
            for item in kwargs["questions"]
        ]
        try:
            request = user_input_resume_registry.create(
                session_id=self.session_id,
                query_id=self.query_id,
                run_id=self.run_id,
                goal_id=self.goal_id,
                goal_revision=self.goal_revision,
                tool_call_id=tool_call_id,
                payload={
                    "title": kwargs["title"],
                    "reason": kwargs["reason"],
                    "questions": questions,
                    "allow_agent_decide": bool(kwargs.get("allow_agent_decide", True)),
                },
            )
        except ValueError as exc:
            return f"❌ 无法请求用户输入：{exc}"
        decision = interrupt(
            {
                "type": "user_input_request",
                "request": request,
                "decisions": [
                    {"action": "submit"},
                    {"action": "agent_decide"},
                    {"action": "cancel"},
                ],
            }
        )
        if not isinstance(decision, dict):
            return "❌ 用户输入恢复失败：返回值无效。"
        action = str(decision.get("action") or "")
        if action == "cancel":
            return "用户取消了本次问题；不要把取消解释为授权或默认选择。"
        if action == "agent_decide":
            recommended = {
                question["id"]: [
                    option["id"]
                    for option in question.get("options") or []
                    if option.get("recommended")
                ]
                for question in questions
            }
            return (
                "用户选择由 Agent 决定。优先采用下列明确推荐项；没有推荐项时采用稳妥默认值，"
                "并在最终回复中简要说明：\n"
                + json.dumps(recommended, ensure_ascii=False)
            )
        return "用户已提交结构化答案：\n" + json.dumps(
            {"request_id": request["id"], "answers": decision.get("answers") or []},
            ensure_ascii=False,
            indent=2,
        )


def create_request_user_input_tool(
    *,
    session_id: str = "",
    query_id: str = "",
    run_id: str = "",
    goal_id: str = "",
    goal_revision: int | None = None,
) -> RequestUserInputTool:
    return RequestUserInputTool(
        session_id=session_id,
        query_id=query_id,
        run_id=run_id,
        goal_id=goal_id,
        goal_revision=goal_revision,
    )
