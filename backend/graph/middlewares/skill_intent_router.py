"""Soft intent routing that recommends a Skill, never a concrete tool."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage

_MARKER = "[系统 Skill 提示]"

# Intent routing is deliberately lightweight, but a negated capability must
# never be promoted.  The nearest cue wins so "不要查数据库，只分析 PDF" can
# suppress database-analysis while still enabling knowledge-search.
_NEGATION_CUES = (
    "不要",
    "无需",
    "不需要",
    "不用",
    "别",
    "禁止",
    "排除",
    "忽略",
    "不查询",
    "不查",
    "不分析",
    "不使用",
    "不走",
    "不读取",
    "不检索",
    "without",
    "do not",
    "don't",
    "exclude",
    "ignore",
)
_POSITIVE_RESET_CUES = ("但", "但是", "而是", "改为", "转为", "只", "仅", "instead")
_CLAUSE_BOUNDARY_RE = re.compile(r"[，,。；;！？!?\n]")

_INTENTS: dict[str, dict[str, Any]] = {
    "ai_insights": {
        "keywords": [
            "ai日报",
            "ai热点",
            "ai资讯",
            "ai新闻",
            "ai动态",
            "ai圈",
            "ai hot",
            "人工智能资讯",
            "人工智能新闻",
            "大模型发布",
            "ai论文",
            "ai行业动态",
        ],
        "patterns": [
            r"(?:今天|昨日|昨天|最近|本周|近一周).{0,8}(?:ai|人工智能|大模型|llm)",
            r"(?:ai|人工智能|大模型|llm).{0,8}(?:热点|资讯|新闻|动态|趋势|洞察|发布|论文|产品)",
            r"(?:openai|anthropic|google\s*(?:ai|deepmind)|deepmind|meta\s*ai|mistral).{0,12}(?:最近|发布|动态|新闻|更新)",
        ],
        "skill": "aihot",
    },
    "semantic_dimension": {
        "keywords": ["构建维度", "刷新维度", "车系维度", "crosswalk", "实体匹配", "规范实体"],
        "skill": "build-semantic-dimension",
    },
    "logical_dataset": {
        "keywords": ["逻辑数据集", "纵向合并", "concat", "合并表", "追加这个表", "追加表格"],
        "skill": "build-logical-dataset",
    },
    "database_analysis": {
        "keywords": ["数据库", "postgres", "postgresql", "sql", "配置率", "搭载率", "空气悬架", "激光雷达"],
        "skill": "database-analysis",
    },
    "table_analysis": {
        "keywords": ["excel", "xlsx", "csv", "tsv", "上险量", "销量", "环比", "同比", "表格分析"],
        "skill": "table-analysis",
    },
    "knowledge_search": {
        "keywords": ["知识库", "白皮书", "文档检索", "pdf", "markdown"],
        "skill": "knowledge-search",
    },
    "web_research": {
        "keywords": ["网页", "链接", "最新新闻", "联网搜索", "搜索一下"],
        "skill": "tavily-search",
    },
}


class SkillIntentRouterMiddleware(AgentMiddleware):
    """Suggest the first project Skill to read from the user intent.

    The middleware intentionally does not activate a Toolset. A successful
    ``read_file(/skills/<id>/SKILL.md)`` is the only activation signal.
    """

    @staticmethod
    def _keyword_is_negated(text: str, keyword_start: int) -> bool:
        clause_start = 0
        for match in _CLAUSE_BOUNDARY_RE.finditer(text, 0, keyword_start):
            clause_start = match.end()
        prefix = text[clause_start:keyword_start]
        last_negative = max((prefix.rfind(cue) for cue in _NEGATION_CUES), default=-1)
        last_positive_reset = max((prefix.rfind(cue) for cue in _POSITIVE_RESET_CUES), default=-1)
        return last_negative > last_positive_reset

    @classmethod
    def _intent_matches(cls, text: str, intent: dict[str, Any]) -> bool:
        for keyword in intent["keywords"]:
            for match in re.finditer(re.escape(keyword.lower()), text):
                if not cls._keyword_is_negated(text, match.start()):
                    return True
        for pattern in intent.get("patterns", []):
            for match in re.finditer(pattern, text):
                if not cls._keyword_is_negated(text, match.start()):
                    return True
        return False

    def _classify_intent(self, text: str) -> dict[str, Any]:
        normalized = text.lower()
        matches = [
            item
            for item in _INTENTS.values()
            if self._intent_matches(normalized, item)
        ]
        matched_skills = {str(item["skill"]) for item in matches}
        if "aihot" in matched_skills and "tavily-search" in matched_skills:
            explicit_web = self._intent_matches(
                normalized,
                {"keywords": ["网页", "链接", "联网搜索", "搜索一下"]},
            )
            if not explicit_web:
                matches = [item for item in matches if item["skill"] != "tavily-search"]
        if not matches:
            return {"matched": False, "skill_ids": [], "routing_prompt": ""}
        skills = list(dict.fromkeys(str(item["skill"]) for item in matches))
        paths = ", ".join(f"/skills/{skill_id}/SKILL.md" for skill_id in skills)
        return {
            "matched": True,
            "skill_ids": skills,
            "routing_prompt": f"本轮问题匹配到项目 Skill。先读取以下最相关的 SKILL.md，再按其中流程执行：{paths}。不要猜测尚未加载 Skill 的业务工具。",
        }

    def _request_with_routing_prompt(self, request: ModelRequest) -> ModelRequest:
        """Add a transient routing hint without writing messages back to state."""
        messages = list(request.messages or [])
        index = next((index for index in range(len(messages) - 1, -1, -1) if isinstance(messages[index], HumanMessage)), None)
        if index is None or not isinstance(messages[index].content, str):
            return request
        original = messages[index]
        content = original.content.split(f"\n\n{_MARKER}")[0]
        decision = self._classify_intent(content)
        if not decision["matched"]:
            return request
        active_skill_ids = {str(item) for item in request.state.get("active_skill_ids") or []}
        missing_skill_ids = [skill_id for skill_id in decision["skill_ids"] if skill_id not in active_skill_ids]
        if not missing_skill_ids:
            return request
        paths = ", ".join(f"/skills/{skill_id}/SKILL.md" for skill_id in missing_skill_ids)
        routing_prompt = (
            "本轮问题匹配到尚未加载的项目 Skill。先读取以下最相关的 SKILL.md，再按其中流程执行："
            f"{paths}。不要猜测尚未加载 Skill 的业务工具。"
        )
        messages[index] = original.model_copy(
            update={"content": f"{content}\n\n{_MARKER} {routing_prompt}"}
        )
        return request.override(messages=messages)

    def wrap_model_call(self, request: ModelRequest, handler: Any) -> ModelResponse:
        return handler(self._request_with_routing_prompt(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self._request_with_routing_prompt(request))
