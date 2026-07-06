"""Parsers for LLM-generated table-analysis plans."""

from __future__ import annotations

import json
import re
from typing import Any

from .errors import PandasQueryEngineError


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract a JSON object from raw LLM text or fenced markdown."""

    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if match:
        parsed = json.loads(match.group(0))
        if isinstance(parsed, dict):
            return parsed

    raise PandasQueryEngineError(f"模型没有返回可解析的 JSON：{text[:500]}")
