"""Stable structured metric logs without package-import side effects."""

from __future__ import annotations

import logging
from typing import Any


def emit_harness_metric(
    logger: logging.Logger,
    name: str,
    *,
    session_id: str = "",
    value: int | float = 1,
    **labels: Any,
) -> None:
    """Emit one parseable metric without coupling callers to a metric backend."""

    normalized = " ".join(
        f"{key}={str(label_value).replace(' ', '_')}"
        for key, label_value in sorted(labels.items())
        if label_value is not None and str(label_value) != ""
    )
    logger.info(
        "[harness-metric] metric=%s value=%s session=%s%s",
        name,
        value,
        session_id or "-",
        f" {normalized}" if normalized else "",
    )
