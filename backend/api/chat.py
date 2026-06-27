"""POST /api/chat — SSE streaming chat with Agent."""

import asyncio
import json
import os
import re
import time
import traceback
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from graph.agent import agent_manager
from graph.session_manager import session_manager
from graph.citations import dedupe_sources, finalize_citations, normalize_source
from config import get_cache_config, get_memory_backend, get_middleware_config

BASE_DIR = Path(__file__).resolve().parent.parent

router = APIRouter()

MISSING_TOOL_OUTPUT_PLACEHOLDER = (
    "[工具执行失败/无返回] 工具调用已开始，但没有收到完成事件；"
    "可能是流中断、工具服务异常或连接中断。"
)


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    user_id: str = "default_user"
    stream: bool = True


async def _generate_title(session_id: str) -> str | None:
    """Generate a title for a session using DeepSeek. Returns title or None."""
    try:
        messages = session_manager.load_session_for_agent(session_id)
        first_user = ""
        first_assistant = ""
        for msg in messages:
            if msg["role"] == "user" and not first_user:
                first_user = msg["content"][:200]
            elif msg["role"] == "assistant" and not first_assistant:
                first_assistant = msg["content"][:200]
            if first_user and first_assistant:
                break

        if not first_user:
            return None

        from langchain_core.messages import HumanMessage as HM
        from llm.model_client import ModelClient

        llm = ModelClient(role="title", temperature=0.3)

        prompt = (
            f"根据以下对话内容，生成一个不超过10个字的中文标题，只输出标题文本，不要加引号或标点。\n\n"
            f"用户: {first_user}\n"
            f"助手: {first_assistant}"
        )

        result = await llm.ainvoke([HM(content=prompt)])
        title = result.content.strip().strip('"\'""''')[:20]
        session_manager.update_title(session_id, title)
        return title
    except Exception:
        traceback.print_exc()
        return None


# ── 口头写入检测与补偿 ──────────────────────────────────────────────

# LLM 回复中出现这些关键词，说明它"声称"写入了 MEMORY.md
_MEMORY_CLAIM_PATTERNS = [
    re.compile(r"write_file.*memory/MEMORY\.md", re.IGNORECASE),
    re.compile(r"已.*保存.*MEMORY", re.IGNORECASE),
    re.compile(r"已.*更新.*MEMORY", re.IGNORECASE),
    re.compile(r"已.*写入.*MEMORY", re.IGNORECASE),
    re.compile(r"记忆.*保存成功", re.IGNORECASE),
    re.compile(r"已记住", re.IGNORECASE),
]


def _llm_claimed_memory_write(segments: list[dict]) -> bool:
    """检测 LLM 回复文本中是否声称写入了 MEMORY.md"""
    for seg in segments:
        text = seg.get("content", "")
        for pattern in _MEMORY_CLAIM_PATTERNS:
            if pattern.search(text):
                return True
    return False


def _actually_called_write_file(segments: list[dict]) -> bool:
    """检测 LLM 是否真正调用了 write_file 工具写入 MEMORY.md"""
    for seg in segments:
        for tc in seg.get("tool_calls", []):
            if tc.get("tool") == "write_file":
                tool_input = tc.get("input", "")
                if "MEMORY" in tool_input or "memory" in tool_input:
                    return True
    return False


def _segment_has_payload(segment: dict) -> bool:
    """Return True when an assistant segment has text or tool call data."""
    return bool(segment.get("content") or segment.get("tool_calls") or segment.get("sources"))


def _ensure_tool_call_outputs(segment: dict) -> dict:
    """Ensure every persisted tool_call has an output.

    Chat history is later reconstructed into AIMessage(tool_calls) followed by
    ToolMessage entries. Persisting a tool_call without output creates an
    invalid provider request on the next turn.
    """
    normalized = {
        "content": segment.get("content", ""),
        "tool_calls": list(segment.get("tool_calls", []) or []),
        "sources": dedupe_sources(list(segment.get("sources", []) or [])),
        "citations": list(segment.get("citations", []) or []),
    }
    for tc in normalized["tool_calls"]:
        output = tc.get("output")
        if output is None or str(output).strip() == "":
            tc["output"] = MISSING_TOOL_OUTPUT_PLACEHOLDER
            tc["is_error"] = True
            tc.setdefault("summary_source", "missing_tool_output")
    return normalized


def _missing_tool_end_events(segment: dict) -> list[dict]:
    """Return synthetic tool_end events for tool_calls that never completed."""
    fixed = _ensure_tool_call_outputs(segment)
    events: list[dict] = []
    for tc in fixed.get("tool_calls", []):
        if tc.get("summary_source") != "missing_tool_output":
            continue
        events.append({
            "tool": tc.get("tool", ""),
            "id": tc.get("id", ""),
            "output": tc.get("output", MISSING_TOOL_OUTPUT_PLACEHOLDER),
            "output_preview": str(tc.get("output", MISSING_TOOL_OUTPUT_PLACEHOLDER))[:2000],
            "summary_source": tc.get("summary_source"),
            "is_error": True,
        })
    return events


async def _detect_and_retry_memory_write(
    user_message: str, segments: list[dict], session_id: str
) -> None:
    """检测口头写入并补偿：如果 LLM 声称写了但没实际调用，用独立 LLM 调用提取并写入。

    这是对 DeepSeek 模型"模拟工具调用"行为的代码级兜底。
    """
    if not _llm_claimed_memory_write(segments):
        return  # LLM 没声称写入，无需处理
    if _actually_called_write_file(segments):
        return  # LLM 确实调用了 write_file，正常情况

    # 到这里说明：LLM 声称写入了但没实际调用 → 口头写入
    print(f"[WARN] Fake memory write detected in session {session_id}, triggering compensation")

    try:
        from langchain_core.messages import HumanMessage as HM
        from llm.model_client import ModelClient

        # 读取当前 MEMORY.md
        memory_path = BASE_DIR / "memory" / "MEMORY.md"
        if not memory_path.exists():
            return
        from graph.prompt_builder import _read_component
        current_memory = _read_component(memory_path)

        # 收集 LLM 的完整回复
        assistant_reply = "\n".join(seg.get("content", "") for seg in segments)

        # 用独立 LLM 调用提取需要记忆的内容并生成更新后的 MEMORY.md
        llm = ModelClient(role="memory", temperature=0)

        prompt = f"""你是一个记忆管理助手。根据以下对话内容，将需要记住的信息追加到 MEMORY.md 中。

## 用户消息
{user_message}

## 助手回复
{assistant_reply}

## 当前 MEMORY.md 内容
{current_memory}

## 任务
请输出更新后的完整 MEMORY.md 内容。规则：
1. 保留所有已有内容，不删除任何现有条目
2. 在对应章节下追加新条目（格式：`- 内容描述`）
3. 如果没有合适的章节，在 `## 重要事项` 下新建 `###` 子章节
4. 只输出 MEMORY.md 的完整内容，不要加任何解释
"""

        result = await llm.ainvoke([HM(content=prompt)])
        new_content = result.content.strip()

        # 基础校验：新内容不能比原内容短（防止误删）
        if len(new_content) < len(current_memory) * 0.8:
            print("[WARN] Compensation result too short, skipping")
            return

        # 写入
        memory_path.write_text(new_content, encoding="utf-8")
        print(f"[INFO] Memory compensation write completed for session {session_id}")

    except Exception as e:
        print(f"[ERROR] Memory compensation failed: {e}")


_MEM0_CLAIM_PATTERNS = [
    re.compile(r"已.*保存.*长期记忆", re.IGNORECASE),
    re.compile(r"已.*记录.*长期记忆", re.IGNORECASE),
    re.compile(r"已.*写入.*长期记忆", re.IGNORECASE),
    re.compile(r"已.*更新.*长期记忆", re.IGNORECASE),
    re.compile(r"长期记忆.*已.*保存", re.IGNORECASE),
    re.compile(r"已记住", re.IGNORECASE),
    re.compile(r"记忆.*保存成功", re.IGNORECASE),
    # 兼容 markdown 模式遗留表述（MEMORY.md）
    re.compile(r"已.*保存.*MEMORY", re.IGNORECASE),
    re.compile(r"已.*更新.*MEMORY", re.IGNORECASE),
    re.compile(r"已.*写入.*MEMORY", re.IGNORECASE),
]


def _llm_claimed_mem0_write(segments: list[dict]) -> bool:
    """检测 LLM 回复文本中是否声称写入了长期记忆（mem0 模式专用）"""
    for seg in segments:
        text = seg.get("content", "")
        for pattern in _MEM0_CLAIM_PATTERNS:
            if pattern.search(text):
                return True
    return False


async def _detect_and_retry_mem0_write(
    user_message: str, segments: list[dict], session_id: str, user_id: str
) -> bool:
    """mem0 模式下的口头写入兜底。

    当 LLM 回复声称"已保存到长期记忆"但未调用任何 save_*_memory 工具时，
    直接调用 mem0_manager.add() 让 mem0 LLM 裁判从本轮对话提取记忆。
    返回 True 表示触发了兜底写入（供上层跳过 SmartExtractor 以免重复）。
    """
    # 条件一：LLM 声称写入
    if not _llm_claimed_mem0_write(segments):
        return False
    # 条件二：未实际调用 save_*_memory（互斥 SmartExtractor 的 mark_agent_wrote）
    agent_saved = any(
        tc.get("tool", "").startswith("save_") and tc.get("tool", "").endswith("_memory")
        for seg in segments
        for tc in seg.get("tool_calls", [])
    )
    if agent_saved:
        return False

    print(f"[WARN] Fake mem0 write detected in session {session_id}, triggering compensation")
    try:
        from graph.mem0_manager import mem0_manager
        assistant_reply = "\n".join(seg.get("content", "") for seg in segments if seg.get("content"))
        messages = [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": assistant_reply},
        ]
        # 让 mem0 LLM 裁判自动分类；不手动打 metadata.type（保持与 SmartExtractor 写入路径一致）
        import asyncio, functools
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, functools.partial(mem0_manager.add, messages, user_id)
        )
        print(f"[INFO] mem0 compensation write completed for session {session_id}")
        return True
    except Exception as e:
        print(f"[ERROR] mem0 compensation failed: {e}")
        return False


# ── 错误分类（用户友好提示）─────────────────────────────────────────────

def _get_user_friendly_error(err: Exception) -> dict[str, str]:
    """将内部异常转换为用户可理解的错误消息。"""
    err_str = str(err).lower()
    raw = str(err)

    if any(k in err_str for k in ("429", "ratelimit", "quota", "insufficient_quota", "exceeded your current quota")):
        return {"error": f"模型 API 调用额度已用完（429），请稍后重试或联系管理员。 [{raw}]"}

    if any(k in err_str for k in ("401", "unauthorized", "authentication", "api key", "invalid key")):
        return {"error": f"API 密钥无效或已过期（401），请联系管理员检查配置。 [{raw}]"}

    if any(k in err_str for k in ("503", "502", "500", "service unavailable", "bad gateway", "internal server error")):
        return {"error": f"模型服务暂时不可用（5xx），请稍后重试。 [{raw}]"}

    if any(k in err_str for k in ("timeout", "timed out", "connection timed out")):
        return {"error": f"请求超时，请稍后重试。 [{raw}]"}

    return {"error": f"生成回复时出错: {raw}"}


MIDDLE_TRIM_SUMMARY_PROMPT = """请总结以下被移出活跃上下文的历史对话。

这是内部上下文优化摘要，用户不会直接看到；请只保留事实，不要补充原文没有的信息。

重点保留：
1. 用户提出过的任务和问题
2. 每个任务的状态：已完成、失败、部分完成、未完成
3. 已执行的重要工具操作、命令、安装、配置、文件路径、版本号和结果
4. 关键结论、错误信息、后续待办
5. 哪些内容属于旧任务，不能在后续新任务中续写或当作当前任务结果

输出要求：
- 使用中文短 bullet
- 偏“任务状态摘要”，不要写成聊天复述
- 不要编造，不确定就写“不确定”

历史对话：
{history}
"""


def _message_to_summary_text(msg: dict) -> str:
    role = msg.get("role", "unknown")
    parts = [f"[{role}] {msg.get('content', '')}"]
    for tc in msg.get("tool_calls", []) or []:
        parts.append(f"[tool_call] {tc.get('tool', '')} input={tc.get('input', '')}")
        if tc.get("output"):
            parts.append(f"[tool_output] {tc.get('output', '')}")
    return "\n".join(parts)


def _rough_message_tokens(msg: dict) -> int:
    try:
        from graph.middlewares.compression import count_text_tokens
        return count_text_tokens(_message_to_summary_text(msg))
    except Exception:
        return max(1, len(_message_to_summary_text(msg)) // 4)


def _select_middle_trim_span(messages: list[dict], cfg: dict) -> tuple[int, int] | None:
    """Return active-message slice [start, end) to summarize/archive, or None."""
    if not cfg.get("enabled", True):
        return None

    max_tokens = cfg.get("max_tokens", 200000)
    if sum(_rough_message_tokens(m) for m in messages) <= max_tokens:
        return None

    head_keep = cfg.get("head_keep", 2)
    keep_recent = cfg.get("keep_recent", 30)
    if len(messages) <= head_keep + keep_recent:
        return None

    start = head_keep
    end = len(messages) - keep_recent

    # 保留的 tail 必须从用户消息开始，避免给 LLM 留半截最近任务。
    while end > start and messages[end].get("role") != "user":
        end -= 1

    if end <= start:
        return None
    return start, end


def _should_preannounce_tool_result_clear(history: list[dict]) -> bool:
    """Mirror ToolResultClear trigger enough to show UI status immediately.

    The actual compression still happens in ToolResultClearMiddleware. This
    preflight only prevents a silent delay before LangGraph reaches before_model.
    """
    cfg = get_middleware_config()
    if not cfg.get("enabled", True):
        return False

    tool_cfg = cfg.get("tool_clear", {})
    keep_recent = tool_cfg.get("keep_recent", 10)
    min_summary_length = tool_cfg.get("min_summary_length", 500)
    summary_prefix = "[摘要] "

    candidates: list[dict] = []
    for msg in history:
        for tc in msg.get("tool_calls", []) or []:
            output = str(tc.get("output", "") or "")
            if output:
                candidates.append(tc)

    if len(candidates) <= keep_recent:
        return False

    to_clear = candidates if keep_recent == 0 else candidates[:-keep_recent]
    for tc in to_clear:
        output = str(tc.get("output", "") or "")
        if tc.get("summary_source") == "tool_result_clear":
            continue
        if output.startswith(summary_prefix):
            continue
        if len(output) >= min_summary_length:
            return True
    return False


def _build_middle_trim_history_text(messages: list[dict], budget_chars: int) -> str:
    parts: list[str] = []
    used = 0
    for msg in messages:
        text = _message_to_summary_text(msg)
        remaining = budget_chars - used
        if remaining <= 0:
            parts.append("[更多中段历史因摘要预算限制已省略]")
            break
        if len(text) > remaining:
            parts.append(text[:remaining] + "\n[单条消息已截断]")
            break
        parts.append(text)
        used += len(text)
    return "\n\n".join(parts)


async def _maybe_middle_trim_session(session_id: str) -> None:
    """Summarize/archive active middle history before building LLM context.

    This keeps frontend history complete via archive + current messages, while
    preventing the LLM context from containing a request-only middle with its
    completion/tool evidence removed.
    """
    cache_cfg = get_cache_config()
    cfg = cache_cfg.get("middle_trim") or {
        "enabled": True,
        "max_tokens": 200000,
        "head_keep": 2,
        "keep_recent": 30,
        "summary_budget_chars": 60000,
    }

    active_messages = session_manager.get_active_messages(session_id)
    span = _select_middle_trim_span(active_messages, cfg)
    if not span:
        return

    start, end = span
    middle = active_messages[start:end]
    if not middle:
        return

    history_text = _build_middle_trim_history_text(
        middle,
        cfg.get("summary_budget_chars", 60000),
    )
    prompt = MIDDLE_TRIM_SUMMARY_PROMPT.format(history=history_text)

    try:
        from langchain_core.messages import HumanMessage as HM
        from llm.model_client import ModelClient

        llm = ModelClient(role="summary", temperature=0)
        result = await llm.ainvoke([HM(content=prompt)])
        summary = result.content.strip()
    except Exception as e:
        print(f"[middle-trim] 摘要失败，跳过本次中段裁剪: {type(e).__name__}: {e}")
        return

    if not summary:
        return

    archive_name = session_manager.middle_trim_history(
        session_id,
        summary,
        start,
        end,
        metadata={
            "reason": "middle_trim",
            "active_message_count_before": len(active_messages),
            "summary_budget_chars": cfg.get("summary_budget_chars", 60000),
        },
    )
    if archive_name:
        print(
            f"[middle-trim] session={session_id} archived active messages[{start}:{end}] "
            f"to {archive_name}"
        )


async def event_generator(message: str, session_id: str, user_id: str = "default_user") -> AsyncGenerator[dict, None]:
    """Generate SSE events from agent stream.

    Tracks multiple response segments — each time the agent finishes
    tool calls and starts generating new text, a new_response event is
    emitted and a new segment begins. Each segment is saved as a
    separate assistant message in the session history.

    Conversation is saved in three scenarios:
    1. Normal completion → saved in "done" event handler
    2. Exception (API timeout, etc.) → saved in except block
    3. Client disconnect (GeneratorExit) → saved in finally block
    """
    # 设置请求级 user_id，供 memory_tools 中的 @tool 函数读取
    from tools.memory_tools import current_user_id
    current_user_id.set(user_id)
    session_manager.update_metadata(
        session_id,
        {
            "runtime_mode": "chat",
            "project_id": None,
            "project_path": None,
            "workspace_type": None,
            "workspace_path": None,
        },
    )

    segments: list[dict] = []
    current_segment: dict = {"content": "", "tool_calls": [], "sources": [], "citations": []}
    turn_sources: list[dict] = []
    conversation_saved = False
    stream_error: Exception | None = None
    waiting_for_first_action = True

    try:
        yield {
            "event": "context_maintenance",
            "data": json.dumps(
                {
                    "status": "start",
                    "phase": "agent_start",
                    "message": "正在分析请求并选择合适的工具...",
                },
                ensure_ascii=False,
            ),
        }
        await _maybe_middle_trim_session(session_id)

        # Use merged history for agent context (combines consecutive assistant msgs)
        history = session_manager.load_session_for_agent(session_id)
        is_first_message = len(history) == 0
        preannounced_tool_clear = _should_preannounce_tool_result_clear(history)
        if preannounced_tool_clear:
            yield {
                "event": "context_maintenance",
                "data": json.dumps(
                    {
                        "status": "start",
                        "phase": "tool_result_clear",
                        "message": "正在整理历史工具结果...",
                    },
                    ensure_ascii=False,
                ),
            }

        async for event in agent_manager.astream(message, history, user_id=user_id, session_id=session_id):
            event_type = event.get("type", "unknown")

            if waiting_for_first_action and event_type in {
                "token", "new_response", "tool_start", "tool_end", "error"
            }:
                waiting_for_first_action = False
                yield {
                    "event": "context_maintenance",
                    "data": json.dumps(
                        {"status": "done", "phase": "agent_start", "message": ""},
                        ensure_ascii=False,
                    ),
                }

            if event_type == "retrieval":
                retrieval_sources = [
                    normalize_source({
                        "title": result.get("source") or "记忆检索",
                        "uri": result.get("source", ""),
                        "document_id": result.get("source", ""),
                        "chunk_id": str(index),
                        "source_type": "knowledge_base",
                        "quote": result.get("text", ""),
                        "score": result.get("score"),
                    })
                    for index, result in enumerate(event.get("results", []))
                ]
                turn_sources = dedupe_sources(turn_sources + retrieval_sources)
                current_segment["sources"] = dedupe_sources(
                    current_segment.get("sources", []) + retrieval_sources
                )
                yield {
                    "event": "retrieval",
                    "data": json.dumps(
                        {"query": event["query"], "results": event["results"]},
                        ensure_ascii=False,
                    ),
                }
                for source in retrieval_sources:
                    yield {
                        "event": "source_found",
                        "data": json.dumps(
                            {"tool_call_id": "retrieval", "source": source},
                            ensure_ascii=False,
                        ),
                    }

            elif event_type == "context_usage":
                # 记录运行时 token 用量峰值
                try:
                    session_manager.update_context_usage_peak(
                        session_id, event["used_tokens"]
                    )
                except Exception:
                    pass
                yield {
                    "event": "context_usage",
                    "data": json.dumps(
                        {
                            "used_tokens": event["used_tokens"],
                            "total_tokens": event["total_tokens"],
                            "percentage": event["percentage"],
                        },
                        ensure_ascii=False,
                    ),
                }

            elif event_type == "tool_result_clear":
                # ToolResultClearMiddleware 摘要了历史 tool output，持久化到 session.json
                try:
                    session_manager.update_tool_call_output(
                        session_id,
                        event["tool_call_id"],
                        f"{event.get('summary_prefix', '[摘要] ')}{event['summary']}",
                        summary_source=event.get("summary_source", "tool_result_clear"),
                    )
                except Exception:
                    traceback.print_exc()
                yield {
                    "event": "tool_result_clear",
                    "data": json.dumps(
                        {
                            "tool_call_id": event["tool_call_id"],
                            "tool": event.get("tool", ""),
                            "summary_source": event.get("summary_source", "tool_result_clear"),
                        },
                        ensure_ascii=False,
                    ),
                }

            elif event_type == "context_maintenance":
                yield {
                    "event": "context_maintenance",
                    "data": json.dumps(
                        {
                            "status": event.get("status", ""),
                            "phase": event.get("phase", ""),
                            "message": event.get("message", ""),
                        },
                        ensure_ascii=False,
                    ),
                }

            elif event_type == "compaction":
                # CompactionMiddleware 触发全局 reset，归档旧消息并写入 compressed_context
                try:
                    session_manager.compress_history(
                        session_id, event["summary"], event["num_to_remove"]
                    )
                except Exception:
                    traceback.print_exc()
                yield {
                    "event": "compaction",
                    "data": json.dumps(
                        {
                            "summary": event["summary"],
                            "num_to_remove": event["num_to_remove"],
                        },
                        ensure_ascii=False,
                    ),
                }

            elif event_type == "token":
                current_segment["content"] += event["content"]
                yield {
                    "event": "token",
                    "data": json.dumps({"content": event["content"]}, ensure_ascii=False),
                }

            elif event_type == "new_response":
                if _segment_has_payload(current_segment):
                    current_segment["sources"] = dedupe_sources(turn_sources)
                    current_segment["citations"] = finalize_citations(
                        current_segment.get("content", ""), turn_sources
                    )
                    segments.append(current_segment)
                current_segment = {"content": "", "tool_calls": [], "sources": [], "citations": []}
                yield {
                    "event": "new_response",
                    "data": json.dumps({}, ensure_ascii=False),
                }

            elif event_type == "tool_start":
                current_segment["tool_calls"].append({
                    "tool": event["tool"],
                    "input": event.get("input", ""),
                    "id": event.get("id", ""),
                })
                yield {
                    "event": "tool_start",
                    "data": json.dumps(
                        {"tool": event["tool"], "input": event["input"], "id": event.get("id", "")},
                        ensure_ascii=False,
                    ),
                }

            elif event_type == "tool_end":
                tc_id = event.get("id", "")
                event_sources = dedupe_sources(list(event.get("sources", []) or []))
                if event_sources:
                    turn_sources = dedupe_sources(turn_sources + event_sources)
                    current_segment["sources"] = dedupe_sources(
                        current_segment.get("sources", []) + event_sources
                    )
                    for source in event_sources:
                        yield {
                            "event": "source_found",
                            "data": json.dumps(
                                {
                                    "tool_call_id": tc_id,
                                    "source": source,
                                },
                                ensure_ascii=False,
                            ),
                        }
                matched = False
                if tc_id:
                    for tc in current_segment["tool_calls"]:
                        if tc.get("id") == tc_id and "output" not in tc:
                            tc["output"] = event["output"]
                            tc["raw_output"] = event.get("raw_output", event["output"])
                            tc["summary_source"] = event.get("summary_source")
                            tc["is_error"] = bool(event.get("is_error"))
                            if event_sources:
                                tc["sources"] = event_sources
                            matched = True
                            break
                if not matched:
                    for tc in reversed(current_segment["tool_calls"]):
                        if tc["tool"] == event["tool"] and "output" not in tc:
                            tc["output"] = event["output"]
                            tc["raw_output"] = event.get("raw_output", event["output"])
                            tc["summary_source"] = event.get("summary_source")
                            tc["is_error"] = bool(event.get("is_error"))
                            if event_sources:
                                tc["sources"] = event_sources
                            break
                yield {
                    "event": "tool_end",
                    "data": json.dumps(
                        {
                            "tool": event["tool"],
                            "id": event.get("id", ""),
                            "output": event.get("output_preview", event["output"]),
                            "output_full_length": len(event["output"]),
                            "summary_source": event.get("summary_source"),
                            "is_error": bool(event.get("is_error")),
                            "sources": event_sources,
                        },
                        ensure_ascii=False,
                    ),
                }

            elif event_type == "error":
                error_msg = event.get("message") or event.get("error") or "Unknown error"
                yield {
                    "event": "error",
                    "data": json.dumps(
                        {"error": error_msg, "message": error_msg},
                        ensure_ascii=False,
                    ),
                }

            elif event_type == "done":
                for missing_event in _missing_tool_end_events(current_segment):
                    yield {
                        "event": "tool_end",
                        "data": json.dumps(
                            {
                                "tool": missing_event["tool"],
                                "id": missing_event.get("id", ""),
                                "output": missing_event["output_preview"],
                                "output_full_length": len(missing_event["output"]),
                                "summary_source": missing_event.get("summary_source"),
                                "is_error": True,
                            },
                            ensure_ascii=False,
                        ),
                    }

                if _segment_has_payload(current_segment):
                    current_segment["sources"] = dedupe_sources(turn_sources)
                    current_segment["citations"] = finalize_citations(
                        current_segment.get("content", ""), turn_sources
                    )
                    segments.append(current_segment)

                session_manager.save_message(session_id, "user", message)
                for seg in segments:
                    seg = _ensure_tool_call_outputs(seg)
                    tc = seg["tool_calls"] if seg["tool_calls"] else None
                    session_manager.save_message(
                        session_id,
                        "assistant",
                        seg["content"],
                        tool_calls=tc,
                        sources=seg.get("sources"),
                        citations=seg.get("citations"),
                    )
                conversation_saved = True

                final_citations = [
                    citation for seg in segments for citation in seg.get("citations", [])
                ]
                yield {
                    "event": "citations_finalized",
                    "data": json.dumps(
                        {
                            "citations": final_citations,
                            "cited_source_ids": list(dict.fromkeys(
                                citation["source_id"] for citation in final_citations
                            )),
                        },
                        ensure_ascii=False,
                    ),
                }

                yield {
                    "event": "done",
                    "data": json.dumps(
                        {"content": event["content"], "session_id": session_id},
                        ensure_ascii=False,
                    ),
                }

                if is_first_message:
                    title = await _generate_title(session_id)
                    if title:
                        yield {
                            "event": "title",
                            "data": json.dumps(
                                {"session_id": session_id, "title": title},
                                ensure_ascii=False,
                            ),
                        }

                # 长期记忆写入：根据 memory_backend 选择写入方式
                memory_backend = get_memory_backend()

                if memory_backend == "mem0":
                    # mem0 模式：先跑口头写入兜底，再通过 SmartExtractor 节流提取
                    try:
                        compensated = await _detect_and_retry_mem0_write(
                            message, segments, session_id, user_id
                        )
                    except Exception as e:
                        compensated = False
                        print(f"[mem0] 兜底调度异常（不影响对话）: {e}")

                    try:
                        from graph.smart_extractor import smart_extractor

                        # user_id already passed as parameter
                        # 收集本轮对话的 user + assistant 消息
                        mem0_messages = [{"role": "user", "content": message}]
                        for seg in segments:
                            if seg["content"]:
                                mem0_messages.append({"role": "assistant", "content": seg["content"]})

                        # 互斥检测：Agent 是否在本轮通过 save_*_memory tool 主动写入了记忆
                        agent_saved = any(
                            tc.get("tool", "").startswith("save_") and tc.get("tool", "").endswith("_memory")
                            for s in segments
                            for tc in s.get("tool_calls", [])
                        )
                        # 互斥：agent 本轮直接写入 OR 兜底已触发 → 都让 SmartExtractor 跳过
                        if agent_saved or compensated:
                            smart_extractor.mark_agent_wrote(session_id)

                        # fire-and-forget：节流提取不阻塞用户响应
                        task = asyncio.create_task(
                            smart_extractor.async_on_turn_end(
                                mem0_messages, user_id, session_id
                            )
                        )
                        task.add_done_callback(
                            lambda t: t.exception() and print(
                                f"[mem0] 后台提取异常: {t.exception()}"
                            )
                        )
                    except Exception as e:
                        print(f"[mem0] 记忆节流提取调度失败（不影响对话）: {e}")
                else:
                    # markdown 模式：保持原有口头写入检测与补偿逻辑
                    await _detect_and_retry_memory_write(
                        message, segments, session_id
                    )

    except Exception as e:
        traceback.print_exc()
        stream_error = e

    finally:
        # Save partial conversation on ANY interruption:
        # - Exception (API timeout, token limit, network error)
        # - GeneratorExit (client disconnect, browser closed)
        # - CancelledError (anyio cancel scope from sse-starlette)
        if not conversation_saved:
            try:
                current_segment["sources"] = dedupe_sources(turn_sources)
                current_segment["citations"] = finalize_citations(
                    current_segment.get("content", ""), turn_sources
                )
                segments.append(current_segment)
                has_content = any(
                    seg["content"] or seg["tool_calls"] for seg in segments
                )
                if has_content:
                    session_manager.save_message(session_id, "user", message)
                    for seg in segments:
                        seg = _ensure_tool_call_outputs(seg)
                        if seg["content"] or seg["tool_calls"]:
                            tc = seg["tool_calls"] if seg["tool_calls"] else None
                            session_manager.save_message(
                                session_id,
                                "assistant",
                                seg["content"],
                                tool_calls=tc,
                                sources=seg.get("sources"),
                                citations=seg.get("citations"),
                            )
                    print(f"[WARN] Stream interrupted, partial conversation saved for session {session_id}")
            except Exception as save_err:
                print(f"[ERROR] Failed to save partial conversation: {save_err}")

    # Yield error event outside finally (cannot yield inside finally)
    if stream_error is not None:
        error_payload = _get_user_friendly_error(stream_error)
        is_api_error = "请稍后重试" in error_payload["error"] or "联系管理员" in error_payload["error"]
        if not conversation_saved or is_api_error:
            yield {
                "event": "error",
                "data": json.dumps(error_payload, ensure_ascii=False),
            }


@router.post("/chat")
async def chat(request: ChatRequest):
    session_manager.update_metadata(
        request.session_id,
        {
            "runtime_mode": "chat",
            "project_id": None,
            "project_path": None,
            "workspace_type": None,
            "workspace_path": None,
        },
    )
    if request.stream:
        return EventSourceResponse(
            event_generator(request.message, request.session_id, request.user_id)
        )
    # Non-streaming fallback — 同样需要设置请求级 user_id
    from tools.memory_tools import current_user_id
    current_user_id.set(request.user_id)
    result = await agent_manager.ainvoke(request.message, request.session_id, request.user_id)
    return {"reply": result}
