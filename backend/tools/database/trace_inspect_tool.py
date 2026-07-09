"""Saved session trace inspection tool for database Agent workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from .models import DatabaseQueryTraceInspectInput


class DatabaseQueryTraceInspectTool(BaseTool):
    name: str = "database_query_trace_inspect"
    description: str = (
        "Inspect saved session messages for database-related tool calls and SQL. "
        "Use this to review prior database_sql_generate/database_sql_execute/database_knowledge_query behavior."
    )
    args_schema: Type[BaseModel] = DatabaseQueryTraceInspectInput
    risk_level: str = "safe"
    base_dir: str = ""

    class Config:
        arbitrary_types_allowed = True

    def _run(self, session_id: str, latest: bool = True, limit: int = 5) -> str:
        try:
            sid = str(session_id or "").strip()
            if not sid.startswith("session-"):
                sid = f"session-{sid}"
            path = Path(self.base_dir) / "sessions" / f"{sid}.json"
            if not path.exists():
                return f"🧮 Trace 检查失败：session 文件不存在：{path}"
            payload = json.loads(path.read_text(encoding="utf-8"))
            calls: list[dict[str, Any]] = []
            for message_index, message in enumerate(payload.get("messages") or []):
                if not isinstance(message, dict):
                    continue
                for call in message.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    tool_name = str(call.get("tool") or "")
                    if not tool_name.startswith("database_"):
                        continue
                    output = str(call.get("output") or call.get("raw_output") or "")
                    calls.append(
                        {
                            "message_index": message_index,
                            "tool": tool_name,
                            "input": str(call.get("input") or "")[:1200],
                            "sql": extract_sql_block(output),
                            "output_preview": output[:1200],
                            "is_error": bool(call.get("is_error")),
                        }
                    )
            if latest:
                calls = list(reversed(calls))
            calls = calls[: max(1, min(int(limit or 5), 50))]
            if not calls:
                return f"🧮 Trace 检查结果：{sid} 中没有 database_* 工具调用。"
            lines = [f"🧮 Trace 数据库调用摘要：{sid}", f"- 返回 {len(calls)} 条", ""]
            for index, call in enumerate(calls, start=1):
                lines.extend(
                    [
                        f"## {index}. {call['tool']} / message[{call['message_index']}]",
                        f"- is_error：{call['is_error']}",
                        f"- input：`{call['input']}`",
                    ]
                )
                if call["sql"]:
                    lines.extend(["- SQL：", "```sql", call["sql"], "```"])
                else:
                    lines.extend(["- output 预览：", "```text", call["output_preview"], "```"])
                lines.append("")
            return "\n".join(lines)
        except Exception as exc:
            return f"🧮 Trace 检查失败：{type(exc).__name__}: {exc}"

    async def _arun(self, session_id: str, latest: bool = True, limit: int = 5) -> str:
        return self._run(session_id=session_id, latest=latest, limit=limit)


def extract_sql_block(text_value: str) -> str:
    marker = "```sql"
    text_value = str(text_value or "")
    start = text_value.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = text_value.find("```", start)
    if end < 0:
        return text_value[start:].strip()
    return text_value[start:end].strip()
