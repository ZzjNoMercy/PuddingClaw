"""Shared helpers for crossing JSON persistence and transport boundaries."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID


def to_json_compatible(value: Any) -> Any:
    """Recursively normalize common Python values to JSON-compatible data.

    Tool and trace payloads can contain database-native scalar values even
    when their outer container is already a ``dict``. Normalizing only the top
    level lets values such as ``datetime`` abort an otherwise healthy run.
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID, Path)):
        return str(value)
    if isinstance(value, Enum):
        return to_json_compatible(value.value)
    if isinstance(value, type):
        return f"{value.__module__}.{value.__qualname__}"
    if is_dataclass(value) and not isinstance(value, type):
        return to_json_compatible(asdict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return to_json_compatible(model_dump(mode="json"))
        except Exception:
            # Pydantic raises PydanticSerializationError (not TypeError) when
            # a model contains runtime-only values such as a tool's
            # ``args_schema`` model class. Python mode preserves those values
            # so the recursive normalizer can represent them safely.
            try:
                return to_json_compatible(model_dump(mode="python"))
            except Exception:
                try:
                    return to_json_compatible(model_dump())
                except Exception:
                    return str(value)
    if isinstance(value, Mapping):
        return {str(key): to_json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json_compatible(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [to_json_compatible(item) for item in sorted(value, key=str)]

    # numpy/pandas scalar wrappers expose ``item`` and commonly appear in
    # database previews without requiring those packages at this boundary.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            unwrapped = item()
        except Exception:
            unwrapped = value
        if unwrapped is not value:
            return to_json_compatible(unwrapped)

    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return str(value)
    return value
