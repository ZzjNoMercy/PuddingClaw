"""Constrained pandas execution helpers."""

from __future__ import annotations

import json
from typing import Any

from .runner import InProcessPandasRunner, blocked_code_token


def safe_json(value: Any) -> Any:
    try:
        import pandas as pd
    except Exception:
        pd = None
    if pd is not None and isinstance(value, pd.DataFrame):
        return value.head(20).to_dict(orient="records")
    if pd is not None and isinstance(value, pd.Series):
        return value.head(20).to_dict()
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        return str(value)


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
