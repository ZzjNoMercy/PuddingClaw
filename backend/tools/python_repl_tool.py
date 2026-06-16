"""Python REPL Tool — wraps LangChain experimental PythonREPLTool."""

from typing import Type

from langchain_core.tools import BaseTool
from langchain_experimental.tools import PythonREPLTool as _OriginalPythonREPLTool
from pydantic import BaseModel, Field


class PythonREPLInput(BaseModel):
    query: str = Field(description="Valid Python code to execute. Use print() to see output.")


class SafePythonREPLTool(BaseTool):
    """Wrapper around PythonREPLTool that adds risk_level field."""

    name: str = "python_repl"
    description: str = (
        "Execute Python code in an interactive REPL environment. "
        "Use this for calculations, data processing, running scripts, "
        "and any task that benefits from programmatic execution. "
        "Input should be valid Python code. "
        "Use print() to see output."
    )
    args_schema: Type[BaseModel] = PythonREPLInput
    risk_level: str = "dangerous"

    def _run(self, query: str) -> str:
        repl = _OriginalPythonREPLTool()
        return repl.run(query)


def create_python_repl_tool() -> BaseTool:
    return SafePythonREPLTool()
