"""POST /api/sessions/{session_id}/compress — Compress conversation history."""

import os
import traceback
from typing import Any

from fastapi import APIRouter, HTTPException
from langchain_core.messages import HumanMessage

from config import get_compress_ratio, get_llm_config
from graph.session_manager import session_manager

router = APIRouter()


async def _generate_summary(messages: list[dict[str, Any]]) -> str:
    """Use DeepSeek to generate a compressed summary of messages."""
    from langchain_deepseek import ChatDeepSeek

    cfg = get_llm_config()
    llm = ChatDeepSeek(
        model=cfg["model"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        temperature=0.3,
    )

    # Format messages for summary
    formatted = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if content:
            formatted.append(f"{role}: {content[:500]}")

    conversation_text = "\n".join(formatted)

    prompt = (
        "请将以下对话历史压缩为简洁的中文摘要。\n"
        "必须保留以下类型的关键信息（如果存在）：\n"
        "1. 用户做出的技术决策（如选型、配置变更）\n"
        "2. 尚未解决的问题或待办事项\n"
        "3. 关键的事实性结论（如性能数据、错误原因）\n"
        "4. 用户明确表达的偏好或约束\n"
        "摘要不超过500字。只输出摘要内容，不要添加额外说明。\n\n"
        f"{conversation_text}"
    )

    result = await llm.ainvoke([HumanMessage(content=prompt)])
    return result.content.strip()


async def auto_compress_session(session_id: str) -> dict[str, Any] | None:
    """Auto-compress session history when triggered by chat pipeline.

    Returns a result dict on success, or None if skipped / failed.
    Never raises — failures are printed and swallowed so the caller is unaffected.
    """
    try:
        messages = session_manager.load_session(session_id)
        if len(messages) < 4:
            return None

        ratio = get_compress_ratio()
        num_to_remove = max(4, int(len(messages) * ratio))
        messages_to_compress = messages[:num_to_remove]

        summary = await _generate_summary(messages_to_compress)

        # 压缩摘要质量验证
        original_total_len = sum(len(m.get("content", "")) for m in messages_to_compress)
        summary_len = len(summary)

        # 检查 1：摘要不能过短（低于原文 5% 说明可能丢失关键信息）
        # 短对话（<200字符）跳过此检查，避免误拦合理摘要
        if original_total_len > 200 and summary_len < original_total_len * 0.05:
            print(f"[compress] WARNING: summary too short ({summary_len} chars vs {original_total_len} original), skipping compression")
            return None

        # 检查 2：摘要不能为空或只有标点
        if not summary or len(summary.strip("。，、；：！？…\n ")) < 5:
            print(f"[compress] WARNING: summary is empty or trivial, skipping compression")
            return None

        session_manager.compress_history(session_id, summary, num_to_remove)
        remaining = len(messages) - num_to_remove
        return {
            "archived_count": num_to_remove,
            "remaining_count": remaining,
        }
    except Exception:
        traceback.print_exc()
        return None


@router.post("/sessions/{session_id}/compress")
async def compress_session(session_id: str) -> dict[str, Any]:
    """Compress conversation history into a summary (manual trigger)."""
    messages = session_manager.load_session(session_id)
    if len(messages) < 4:
        raise HTTPException(
            status_code=400,
            detail="Not enough messages to compress (need at least 4)",
        )

    result = await auto_compress_session(session_id)
    if result is None:
        raise HTTPException(status_code=500, detail="Compression failed")
    return result
