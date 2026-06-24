"""GET /api/capabilities — 返回可选基础设施健康状态。"""

from __future__ import annotations

from fastapi import APIRouter

import capabilities

router = APIRouter()


@router.get("/capabilities")
async def get_capabilities() -> dict:
    """返回 Higress / Milvus / MinerU 的可用性状态。"""
    caps = await capabilities.detect_capabilities(force=True)
    return caps.to_dict()
