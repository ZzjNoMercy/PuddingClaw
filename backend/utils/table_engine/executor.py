"""Constrained pandas execution helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import Any

from .runner import InProcessPandasRunner, blocked_code_token


def safe_json(value: Any) -> Any:
    """Return a recursively JSON-safe preview of a pandas execution result.

    ``DataFrame.to_dict`` preserves scalar values such as ``Timestamp``.  A
    shallow conversion therefore still lets a Python ``datetime`` reach the
    ToolMessage/checkpoint boundary and abort the entire graph run.  Normalize
    every nested scalar here, which is the one boundary shared by result
    previews, trace metadata, and model-visible tool output.
    """
    try:
        import pandas as pd
    except Exception:
        pd = None

    if pd is not None and isinstance(value, pd.DataFrame):
        return safe_json(value.head(20).to_dict(orient="records"))
    if pd is not None and isinstance(value, pd.Series):
        return safe_json(value.head(20).to_dict())
    if pd is not None and value is pd.NaT:
        return None
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return safe_json(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key) if isinstance(key, (str, int, float, bool)) or key is None else str(safe_json(key)): safe_json(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [safe_json(item) for item in value]
    # numpy scalar values (and similar scalar wrappers) expose ``item``.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            unwrapped = item()
        except Exception:
            unwrapped = value
        if unwrapped is not value:
            return safe_json(unwrapped)
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return str(value)
    return value


def render_value(value: Any) -> str:
    try:
        import pandas as pd
    except Exception:
        pd = None
    if pd is not None and isinstance(value, pd.DataFrame):
        return value.head(50).to_string(index=False)
    if pd is not None and isinstance(value, pd.Series):
        return value.head(50).to_string()
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(safe_json(value), ensure_ascii=False, indent=2)
    return str(value)


def execute_pandas_code(df: Any, code: str) -> Any:
    """Execute generated pandas code via the default local runner."""

    return InProcessPandasRunner().run(df, code)
