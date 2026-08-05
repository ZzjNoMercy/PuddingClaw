from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from analytics.models.router import AnalyticsModelRouter

MODELS = [
    {
        "id": "产品配置分析",
        "name": "产品配置分析",
        "description": "车型配置、配置率和新车迭代分析",
        "tags": ["配置率", "汽车产品配置"],
    },
    {
        "id": "汽车行业综合分析",
        "name": "汽车行业综合分析",
        "description": "汽车销量、产品规划和行业趋势",
        "tags": ["汽车销量", "行业趋势"],
    },
]


class FakeModel:
    def __init__(self, payload: dict):
        self.payload = payload

    async def ainvoke(self, _messages):
        return AIMessage(content=json.dumps(self.payload, ensure_ascii=False))


def test_single_allowed_model_is_selected_without_classifier():
    route = AnalyticsModelRouter.deterministic("任意业务问题", [MODELS[0]])
    assert route is not None
    assert route.selected_id == "产品配置分析"
    assert route.strategy == "single_allowed"


def test_explicit_model_name_and_unique_tag_are_deterministic():
    named = AnalyticsModelRouter.deterministic("请使用汽车行业综合分析看看销量", MODELS)
    tagged = AnalyticsModelRouter.deterministic("空气悬架配置率是多少", MODELS)
    assert named is not None and named.selected_id == "汽车行业综合分析"
    assert tagged is not None and tagged.selected_id == "产品配置分析"


@pytest.mark.asyncio
async def test_semantic_router_can_select_only_an_allowed_candidate():
    route = await AnalyticsModelRouter.route(
        message="今年七月发布了多少新款型",
        candidates=MODELS,
        model=FakeModel({"selected_id": "产品配置分析", "confidence": 0.91, "reason": "new_model_launch"}),
    )
    assert route.status == "matched"
    assert route.selected_id == "产品配置分析"
    assert route.strategy == "semantic"


@pytest.mark.asyncio
async def test_low_confidence_or_unknown_candidate_never_guesses():
    low = await AnalyticsModelRouter.route(
        message="分析一下",
        candidates=MODELS,
        model=FakeModel({"selected_id": "产品配置分析", "confidence": 0.5, "reason": "ambiguous"}),
    )
    unknown = await AnalyticsModelRouter.route(
        message="分析一下",
        candidates=MODELS,
        model=FakeModel({"selected_id": "不存在", "confidence": 0.99, "reason": "invalid"}),
    )
    assert low.status == "ambiguous" and low.selected_id is None
    assert unknown.status == "ambiguous" and unknown.selected_id is None
