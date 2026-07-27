"""The model-visible declaration used to finish an active Goal.

The control middleware owns persistence because it has the trusted runtime and
provider Tool Call identity.  Keeping this tool intentionally inert prevents a
subagent or a forged argument from manufacturing that identity.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field


class UpdateGoalInput(BaseModel):
    completed: bool | None = Field(
        default=None,
        description="Set only to true after all required work and proportionate checks are complete.",
    )
    blocked_reason: str | None = Field(default=None, max_length=2000)
    message: str | None = Field(default=None, max_length=2000)


class UpdateGoalTool(BaseTool):
    name: str = "update_goal"
    description: str = (
        "Record an explicit Goal completion declaration. Use completed=true only after checking "
        "the original Goal and actual results. After a successful completion declaration, generate "
        "only the final response; any further tool work invalidates it and requires a new declaration."
    )
    args_schema: type[BaseModel] = UpdateGoalInput
    risk_level: str = "safe"

    def _run(self, **_: object) -> str:
        return "update_goal must be executed through the Goal completion control plane."


def create_update_goal_tool() -> UpdateGoalTool:
    return UpdateGoalTool()
