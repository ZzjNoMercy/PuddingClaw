"""POST /api/chat — SSE streaming chat with Agent.

基于 V5 结构，融合魔镜Claw 的 API 层优化：
- 口头写入检测与补偿
- 错误分类（用户友好提示）
- context_usage / new_response / error 事件透传
- 保留 V5 的整体结构和前端兼容性
"""

import asyncio
import json
import os
import re
import traceback
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from graph.agent import agent_manager
from graph.session_manager import session_manager
from config import get_compress_trigger_count, get_llm_config, get_memory_backend
from api.compress import auto_compress_session

BASE_DIR = Path(__file__).resolve().parent.parent

router = APIRouter()


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

        from langchain_deepseek import ChatDeepSeek
        from langchain_core.messages import HumanMessage as HM

        cfg = get_llm_config()
        llm = ChatDeepSeek(
            model=cfg["model"],
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            temperature=0.3,
        )

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

_MEMORY_CLAIM_PATTERNS = [
    re.compile(r"write_file.*memory/MEMORY\.md", re.IGNORECASE),
    re.compile(r"已.*保存.*MEMORY", re.IGNORECASE),
    re.compile(r"已.*更新.*MEMORY", re.IGNORECASE),
    re.compile(r"已.*写入.*MEMORY", re.IGNORECASE),
    re.compile(r"记忆.*保存成功", re.IGNORECASE),
    re.compile(r"已记住", re.IGNORECASE),
]


def _llm_claimed_memory_write(segments: list[dict]) -> bool:
    for seg in segments:
        text = seg.get("content", "")
        for pattern in _MEMORY_CLAIM_PATTERNS:
            if pattern.search(text):
                return True
    return False


def _actually_called_write_file(segments: list[dict]) -> bool:
    for seg in segments:
        for tc in seg.get("tool_calls", []):
            if tc.get("tool") == "write_file":
                tool_input = tc.get("input", "")
                if "MEMORY" in tool_input or "memory" in tool_input:
                    return True
    return False


async def _detect_and_retry_memory_write(
    user_message: str, segments: list[dict], session_id: str
) -> None:
    """检测口头写入并补偿：如果 LLM 声称写了但没实际调用，用独立 LLM 调用提取并写入。"""
    if not _llm_claimed_memory_write(segments):
        return
    if _actually_called_write_file(segments):
        return

    print(f"[WARN] Fake memory write detected in session {session_id}, triggering compensation")

    try:
        from langchain_deepseek import ChatDeepSeek
        from langchain_core.messages import HumanMessage as HM

        memory_path = BASE_DIR / "memory" / "MEMORY.md"
        if not memory_path.exists():
            return
        from graph.prompt_builder import _read_component
        current_memory = _read_component(memory_path)

        assistant_reply = "\n".join(seg.get("content", "") for seg in segments)

        cfg = get_llm_config()
        llm = ChatDeepSeek(
            model=cfg["model"],
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            temperature=0,
        )

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

        if len(new_content) < len(current_memory) * 0.8:
            print("[WARN] Compensation result too short, skipping")
            return

        memory_path.write_text(new_content, encoding="utf-8")
        print(f"[INFO] Memory compensation write completed for session {session_id}")

    except Exception as e:
        print(f"[ERROR] Memory compensation failed: {e}")


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


# ── SSE 事件生成器 ──────────────────────────────────────────────────

async def event_generator(message: str, session_id: str, user_id: str = "default_user") -> AsyncGenerator[dict, None]:
    """Generate SSE events from agent stream.

    相比 V5 新增透传的事件：
    - context_usage: { used_tokens, total_tokens, percentage }
    - new_response: 工具执行后 LLM 重新开始生成时
    - error: 带分类的错误提示
    """
    from tools.memory_tools import current_user_id
    current_user_id.set(user_id)

    segments: list[dict] = []
    current_segment: dict = {"content": "", "tool_calls": []}
    conversation_saved = False
    stream_error: Exception | None = None

    try:
        history = session_manager.load_session_for_agent(session_id)
        is_first_message = len(history) == 0

        async for event in agent_manager.astream(message, history, user_id=user_id):
            event_type = event.get("type", "unknown")

            if event_type == "retrieval":
                yield {
                    "event": "retrieval",
                    "data": json.dumps(
                        {"query": event["query"], "results": event["results"]},
                        ensure_ascii=False,
                    ),
                }

            elif event_type == "context_usage":
                # 新增：上下文使用率，前端可选展示
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

            elif event_type == "token":
                current_segment["content"] += event["content"]
                yield {
                    "event": "token",
                    "data": json.dumps({"content": event["content"]}, ensure_ascii=False),
                }

            elif event_type == "new_response":
                # 新增：工具执行后新回复段开始
                segments.append(current_segment)
                current_segment = {"content": "", "tool_calls": []}
                yield {
                    "event": "new_response",
                    "data": json.dumps({}, ensure_ascii=False),
                }

            elif event_type == "tool_start":
                if current_segment["content"]:
                    segments.append(current_segment)
                    current_segment = {"content": "", "tool_calls": []}
                current_segment["tool_calls"].append({
                    "tool": event["tool"],
                    "input": event.get("input", ""),
                    "id": event.get("id", ""),
                })
                yield {
                    "event": "tool_start",
                    "data": json.dumps(
                        {"tool": event["tool"], "input": event["input"]},
                        ensure_ascii=False,
                    ),
                }

            elif event_type == "tool_end":
                tc_id = event.get("id", "")
                matched = False
                if tc_id:
                    for tc in current_segment["tool_calls"]:
                        if tc.get("id") == tc_id and "output" not in tc:
                            tc["output"] = event["output"]
                            matched = True
                            break
                if not matched:
                    for tc in reversed(current_segment["tool_calls"]):
                        if tc["tool"] == event["tool"] and "output" not in tc:
                            tc["output"] = event["output"]
                            break
                yield {
                    "event": "tool_end",
                    "data": json.dumps(
                        {"tool": event["tool"], "output": event["output"]},
                        ensure_ascii=False,
                    ),
                }

            elif event_type == "done":
                segments.append(current_segment)

                session_manager.save_message(session_id, "user", message)
                for seg in segments:
                    tc = seg["tool_calls"] if seg["tool_calls"] else None
                    session_manager.save_message(
                        session_id, "assistant", seg["content"], tool_calls=tc
                    )
                conversation_saved = True

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

                # 口头写入检测与补偿
                await _detect_and_retry_memory_write(message, segments, session_id)

    except Exception as e:
        traceback.print_exc()
        stream_error = e

    finally:
        if not conversation_saved:
            try:
                segments.append(current_segment)
                has_content = any(
                    seg["content"] or seg["tool_calls"] for seg in segments
                )
                if has_content:
                    session_manager.save_message(session_id, "user", message)
                    for seg in segments:
                        if seg["content"] or seg["tool_calls"]:
                            tc = seg["tool_calls"] if seg["tool_calls"] else None
                            session_manager.save_message(
                                session_id, "assistant", seg["content"], tool_calls=tc
                            )
                    conversation_saved = True
                    print(f"[WARN] Stream interrupted, partial conversation saved for session {session_id}")
            except Exception as save_err:
                print(f"[ERROR] Failed to save partial conversation: {save_err}")

        if stream_error is not None:
            error_payload = _get_user_friendly_error(stream_error)
            is_api_error = "请稍后重试" in error_payload["error"] or "联系管理员" in error_payload["error"]
            if not conversation_saved or is_api_error:
                yield {
                    "event": "error",
                    "data": json.dumps(error_payload, ensure_ascii=False),
                }


# ── 路由 ────────────────────────────────────────────────────────────

@router.post("/chat")
async def chat(chat_request: ChatRequest):
    """SSE streaming chat endpoint."""
    if chat_request.stream:
        return EventSourceResponse(
            event_generator(
                chat_request.message,
                chat_request.session_id,
                chat_request.user_id,
            )
        )

    from tools.memory_tools import current_user_id
    current_user_id.set(chat_request.user_id)
    result = await agent_manager.ainvoke(
        chat_request.message, chat_request.session_id, user_id=chat_request.user_id
    )
    return {"reply": result}
