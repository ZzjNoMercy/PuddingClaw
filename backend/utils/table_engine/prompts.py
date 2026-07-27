"""Prompts for PuddingClaw's pandas query engine."""

from __future__ import annotations

import json
from typing import Any

CODE_GENERATION_SYSTEM_PROMPT = (
    "你是一个 pandas 数据分析助手。你只能基于给定 DataFrame df 回答问题。"
    "请生成安全、简短的 pandas 代码，不要读取文件、不要写文件、不要联网、不要导入模块、不要访问系统。"
    "不要定义函数、类、循环，也不要调用 pandas 的 read_* / to_* / to_sql 等 IO 方法。"
    "返回严格 JSON：{\"code\":\"...\", \"explanation\":\"...\"}。"
    "code 必须把最终结果赋值给变量 result。"
)

ANSWER_SYNTHESIS_SYSTEM_PROMPT = "你是数据分析结果解释器，只根据执行结果回答。"


def build_code_generation_prompt(
    *,
    query: str,
    profile: dict[str, Any],
    semantic_context: dict[str, Any] | None = None,
    previous_error: str | None = None,
    previous_code: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "question": query,
        "dataframe_profile": {
            "shape": profile.get("shape"),
            "columns": profile.get("columns"),
            "dtypes": profile.get("dtypes"),
            "preview": profile.get("preview"),
        },
        "rules": [
            "只能使用 df、pd、np、math。",
            "优先写一到三行 pandas 代码。",
            "如果问题需要分组，用 groupby。",
            "如果列名包含中文或特殊字符，用 df['列名']。",
            "不要使用 import、open、read_csv、read_excel、to_csv、to_excel、to_sql、read_sql。",
            "不要定义函数、类或循环；只写简单 pandas 表达式和中间变量赋值。",
            "最终结果必须赋值给 result。",
        ],
    }
    if semantic_context:
        payload["semantic_context"] = semantic_context
        payload["rules"].insert(
            0,
            "必须遵守 semantic_context 中的度量值、维度、颗粒度、分类、字段绑定和禁止推断规则。",
        )
    if previous_error:
        payload["previous_code"] = previous_code or ""
        payload["previous_error"] = previous_error[-4000:]
        payload["repair_instruction"] = "上一段代码执行失败，请根据错误和列名修正代码。"
    return json.dumps(payload, ensure_ascii=False)


def build_answer_synthesis_prompt(
    *,
    query: str,
    code: str,
    rendered_result: str,
    semantic_context: dict[str, Any] | None = None,
) -> str:
    compact_semantics: dict[str, Any] | None = None
    if semantic_context:
        compact_semantics = {
            "context_id": semantic_context.get("context_id"),
            "content_hash": semantic_context.get("content_hash"),
            "analytics_model": semantic_context.get("analytics_model"),
            "semantic_assets": [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "type": item.get("type"),
                }
                for item in semantic_context.get("semantic_assets") or []
                if isinstance(item, dict)
            ],
            "references": [
                {"id": item.get("id"), "name": item.get("name")}
                for item in semantic_context.get("references") or []
                if isinstance(item, dict)
            ],
        }
    return json.dumps(
        {
            "question": query,
            "code": code,
            "result": rendered_result[:8000],
            "semantic_context": compact_semantics,
            "instruction": (
                "用中文简洁回答用户问题。如果结果是表格，概括关键行列即可。"
                "保持 semantic_context 中的业务术语和口径，不要重新推断或改变分类。"
            ),
        },
        ensure_ascii=False,
    )
