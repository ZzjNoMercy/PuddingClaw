"""Minimal, dedicated Agent runtime for background LLM Wiki compilation."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, ToolMessage

from llm.model_client import ModelClientChatModel
from tools.llm_wiki_tools import LlmWikiContextTool, LlmWikiLintTool, LlmWikiPublishTool

REQUIRED_TOOL_NAMES = ("llm_wiki_context", "llm_wiki_publish", "llm_wiki_lint")

COMPILER_SYSTEM_PROMPT = """你是 PuddingClaw 内置的 LLM Wiki Compiler Agent。

你的唯一职责是把任务指定的不可变 Raw 快照编译为符合活动 Schema 的 Wiki 页面。你没有聊天、文件系统、联网、MCP、子 Agent 或通用知识库工具，只有以下三个工具：
1. llm_wiki_context：必须首先调用一次，参数必须是 operation=ingest 和任务给出的完整 raw_paths。
2. llm_wiki_publish：根据 context 返回的 AGENTS.md、Schema Bundle、Raw 内容与当前 index，一次提交完整页面。
3. llm_wiki_lint：publish 成功后必须调用，并确认 ok=true。

严格规则：
- 只能使用 context 授权的 Raw；不得臆造来源。
- publish 的 raw_paths 必须与任务给出的完整列表逐项一致。
- sources 必须逐字复制 context 的 snapshot_path，不得添加 raw/ 前缀。
- page slug 相对于 wiki/ 根目录，例如 frameworks/langgraph；不得写成 wiki/frameworks/langgraph。
- wikilink 必须使用 [[<type-directory>/<slug>]] 完整路径；不得再次添加 wiki/。
- 页面必须可读、可追溯、相互链接，并满足所有 frontmatter 与 Schema 约束。
- 不得跳过、调换或重复上述三个步骤；工具失败时不要用其他方式绕过。
- 完成 Lint 后用一句简短中文总结结果，不要向用户提问。
"""

ToolEventCallback = Callable[[str, str, dict[str, Any]], Awaitable[None] | None]


def _decode_tool_output(content: Any) -> dict[str, Any]:
    if isinstance(content, list):
        content = "".join(
            str(block.get("text") or block.get("content") or "")
            for block in content
            if isinstance(block, dict)
        )
    if not isinstance(content, str):
        return {}
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def _emit(callback: ToolEventCallback | None, phase: str, name: str, payload: dict[str, Any]) -> None:
    if callback is None:
        return
    result = callback(phase, name, payload)
    if inspect.isawaitable(result):
        await result


class LlmWikiCompilerAgent:
    """A three-tool Agent with no chat session, generic skills or middleware."""

    def __init__(self, *, base_dir: Path, model_id: str = "") -> None:
        self.base_dir = base_dir.resolve()
        self.model_id = str(model_id or "").strip()

    def _build(self):
        model = ModelClientChatModel(
            role="llm_wiki_compiler",
            temperature=0.2,
            streaming=True,
            thinking_enabled=False,
            model_id_override=self.model_id or None,
            binding="agent",
        )
        tools = [
            LlmWikiContextTool(base_dir=self.base_dir),
            LlmWikiPublishTool(base_dir=self.base_dir),
            LlmWikiLintTool(base_dir=self.base_dir),
        ]
        return create_agent(
            model=model,
            tools=tools,
            system_prompt=COMPILER_SYSTEM_PROMPT,
            name="llm_wiki_compiler",
        )

    async def run(
        self,
        prompt: str,
        *,
        job_id: str,
        raw_paths: list[str],
        on_tool_event: ToolEventCallback | None = None,
    ) -> dict[str, Any]:
        agent = self._build()
        processed_messages = 0
        tool_names_by_call_id: dict[str, str] = {}
        called: dict[str, dict[str, Any]] = {}
        final_text = ""
        expected_tool_index = 0
        active_tool_call_id = ""

        async for state in agent.astream(
            {"messages": [{"role": "user", "content": prompt}]},
            config={
                "recursion_limit": 16,
                "metadata": {
                    "runtime": "llm_wiki_compiler_agent",
                    "job_id": job_id,
                },
            },
            stream_mode="values",
        ):
            messages = state.get("messages") if isinstance(state, dict) else None
            if not isinstance(messages, list):
                continue
            new_messages = messages[processed_messages:]
            processed_messages = len(messages)
            for message in new_messages:
                if isinstance(message, AIMessage):
                    for tool_call in message.tool_calls or []:
                        name = str(tool_call.get("name") or "")
                        call_id = str(tool_call.get("id") or "")
                        args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
                        if call_id:
                            tool_names_by_call_id[call_id] = name
                        if name in REQUIRED_TOOL_NAMES:
                            if active_tool_call_id:
                                raise RuntimeError("Wiki Compiler Agent 不得并行或重复调用编译工具")
                            expected_name = (
                                REQUIRED_TOOL_NAMES[expected_tool_index]
                                if expected_tool_index < len(REQUIRED_TOOL_NAMES)
                                else ""
                            )
                            if name != expected_name:
                                raise RuntimeError(
                                    f"Wiki Compiler Agent 工具顺序错误：期望 {expected_name or '结束'}，实际 {name}"
                                )
                            if name == "llm_wiki_context" and (
                                args.get("operation") != "ingest"
                                or args.get("raw_paths") != raw_paths
                            ):
                                raise RuntimeError("llm_wiki_context 必须使用任务锁定的完整 raw_paths")
                            if name == "llm_wiki_publish" and args.get("raw_paths") != raw_paths:
                                raise RuntimeError("llm_wiki_publish 必须提交任务锁定的完整 raw_paths")
                            active_tool_call_id = call_id
                            await _emit(
                                on_tool_event,
                                "start",
                                name,
                                args,
                            )
                    if isinstance(message.content, str) and message.content.strip():
                        final_text = message.content.strip()
                elif isinstance(message, ToolMessage):
                    name = str(message.name or tool_names_by_call_id.get(str(message.tool_call_id or "")) or "")
                    if name not in REQUIRED_TOOL_NAMES:
                        continue
                    if str(message.tool_call_id or "") != active_tool_call_id:
                        raise RuntimeError(f"Wiki Compiler Agent 收到未授权的工具结果：{name}")
                    result = _decode_tool_output(message.content)
                    if getattr(message, "status", None) == "error" or result.get("ok") is False:
                        raise RuntimeError(str(result.get("error") or message.content or f"{name} 执行失败"))
                    called[name] = result
                    await _emit(on_tool_event, "end", name, result)
                    expected_tool_index += 1
                    active_tool_call_id = ""

        missing = [name for name in REQUIRED_TOOL_NAMES if name not in called]
        if missing:
            raise RuntimeError(f"Wiki Compiler Agent 未完成必要步骤：{', '.join(missing)}")
        return {"called": called, "final_text": final_text, "outcome": "completed"}
