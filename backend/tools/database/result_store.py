"""Tool-layer wrapper for persisted database query result access."""

from __future__ import annotations

from typing import Any

from analytics.nl2sql.result_store import QueryResultStoreError, get_query_result_page


async def read_query_result_page(
    session: Any,
    result_id: str,
    *,
    page: int,
    page_size: int | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    return await get_query_result_page(
        session,
        result_id,
        page=page,
        page_size=page_size,
        session_id=session_id or None,
    )


__all__ = ["QueryResultStoreError", "read_query_result_page"]
