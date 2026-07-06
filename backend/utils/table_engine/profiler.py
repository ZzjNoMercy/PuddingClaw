"""DataFrame and sheet profiling helpers for table QA."""

from __future__ import annotations

import json
from typing import Any


def profile_dataframe(df: Any, *, preview_rows: int = 5) -> dict[str, Any]:
    """Return a compact, prompt-friendly profile for a DataFrame."""

    return {
        "shape": [int(df.shape[0]), int(df.shape[1])],
        "columns": [str(col) for col in df.columns],
        "dtypes": {str(key): str(value) for key, value in df.dtypes.to_dict().items()},
        "preview": json.loads(df.head(preview_rows).to_json(orient="records", force_ascii=False, date_format="iso")),
    }
