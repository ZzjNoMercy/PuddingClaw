"""GET /api/stats/* — Token usage statistics API."""

from typing import Any

from fastapi import APIRouter, Query

from graph.token_usage_store import get_ranking, get_total, get_daily

router = APIRouter()


@router.get("/stats/tokens/ranking")
async def tokens_ranking(limit: int = Query(10, ge=1, le=100)) -> dict[str, Any]:
    """用户 Token 消耗排行."""
    data = get_ranking(limit=limit)
    return {"ranking": data}


@router.get("/stats/tokens/total")
async def tokens_total() -> dict[str, Any]:
    """全局累计 Token 统计."""
    data = get_total()
    return data


@router.get("/stats/tokens/daily")
async def tokens_daily(days: int = Query(7, ge=1, le=90)) -> dict[str, Any]:
    """最近 N 天每日 Token 消耗趋势."""
    data = get_daily(days=days)
    return {"daily": data}
