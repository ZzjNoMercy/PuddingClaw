from datetime import datetime, timezone
import json

import pandas as pd

from utils.table_engine.executor import safe_json


def test_safe_json_normalizes_datetimes_nested_in_dataframe_records() -> None:
    frame = pd.DataFrame(
        {
            "occurred_at": [datetime(2026, 7, 26, 9, 30, tzinfo=timezone.utc)],
            "nested": [{"updated_at": datetime(2026, 7, 26, 9, 31)}],
        }
    )

    value = safe_json(frame)

    assert value == [
        {
            "occurred_at": "2026-07-26T09:30:00+00:00",
            "nested": {"updated_at": "2026-07-26T09:31:00"},
        }
    ]
    # Regression guard: this is exactly the ToolMessage/checkpoint boundary
    # that previously raised ``TypeError: datetime is not JSON serializable``.
    json.dumps(value, ensure_ascii=False, allow_nan=False)
