"""Run-local task intent classification and verification-pack selection.

The selected analytics model is deliberately treated as available context,
never as proof that the current Run is an analytics task.
"""

from __future__ import annotations

import re
from typing import Any

from harness.models import RunTaskProfile

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

INTENT_REGISTRY: dict[str, dict[str, Any]] = {
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
        "packs": ["core", "web_research"],
    },
    "semantic_dimension": {
        "keywords": ["构建维度", "刷新维度", "车系维度", "crosswalk", "实体匹配", "规范实体"],
        "skill": "build-semantic-dimension",
        "packs": ["core", "analytics"],
    },
    "logical_dataset": {
        "keywords": ["逻辑数据集", "纵向合并", "concat", "合并表", "追加这个表", "追加表格"],
        "skill": "build-logical-dataset",
        "packs": ["core", "analytics"],
    },
    "database_analysis": {
        "keywords": [
            "问数",
            "查询数据库",
            "查询数据",
            "统计数据",
            "计算指标",
            "分析销量",
            "分析收入",
            "分析毛利",
            "配置率",
            "搭载率",
            "配备率",
            "空气悬架",
            "激光雷达",
            "同比",
            "环比",
            "占比",
        ],
        "patterns": [
            r"(?:查询|统计|计算|对比).{0,10}(?:数据库|sql|指标|数据|销量|收入|利润|同比|环比|占比)",
            r"分析.{0,10}(?:数据库|sql|指标|销量|收入|利润|同比|环比|占比)",
            r"(?:指标|销量|收入|利润).{0,10}(?:分析|报告|口径|趋势|原因|贡献|变化)",
            r"(?:执行|运行).{0,8}(?:sql|查询语句)",
            r"生成.{0,8}查询语句",
        ],
        "skill": "database-analysis",
        "packs": ["core", "analytics"],
    },
    "table_analysis": {
        "keywords": ["excel", "xlsx", "csv", "tsv", "上险量", "表格分析", "数据分析"],
        "skill": "table-analysis",
        "packs": ["core", "analytics"],
    },
    "knowledge_search": {
        "keywords": ["知识库", "白皮书", "文档检索", "pdf", "markdown"],
        "skill": "knowledge-search",
        "packs": ["core", "web_research"],
    },
    "web_research": {
        "keywords": [
            "网页搜索",
            "打开网页",
            "检索网页",
            "访问链接",
            "最新新闻",
            "联网搜索",
            "搜索一下",
            "最近有什么新闻",
            "最新消息",
            "http://",
            "https://",
        ],
        "skill": "tavily-search",
        "packs": ["core", "web_research"],
    },
    "code": {
        "keywords": [
            "修改代码",
            "修复代码",
            "实现代码",
            "重构代码",
            "运行测试",
            "pytest",
            "单元测试",
            "静态检查",
            "typescript",
            "python代码",
            "python 代码",
            "数据结构代码",
            "更新项目依赖",
            "升级项目依赖",
            "运行构建",
            "执行构建",
            "创建页面",
            "实现页面",
        ],
        "patterns": [
            r"(?:修复|修改|实现|重构|调试).{0,10}(?:代码|函数|接口|组件|bug)",
            r"(?:python|typescript|javascript|flutter|dart).{0,10}(?:代码|测试|报错|实现|修复)",
            r"(?:更新|升级).{0,8}(?:依赖|package|包).{0,12}(?:构建|测试|运行)?",
            r"(?:创建|实现|修改).{0,10}(?:网页|页面|组件|接口)",
        ],
        "packs": ["core", "code"],
    },
    "artifact": {
        "keywords": [
            "生成报告",
            "创建报告",
            "更新报告",
            "刷新报告",
            "生成文件",
            "创建文件",
            "生成文档",
            "创建文档",
            "更新模板",
            "刷新模板",
            "/workspace/",
        ],
        "patterns": [
            r"(?:生成|创建|更新|刷新).{0,20}(?:报告|模板|文档|表格|图表|文件)",
        ],
        "packs": ["core", "artifact"],
    },
}

_PRIMARY_PRIORITY = (
    "semantic_dimension",
    "logical_dataset",
    "database_analysis",
    "table_analysis",
    "code",
    "ai_insights",
    "web_research",
    "knowledge_search",
    "artifact",
)


def _keyword_is_negated(text: str, keyword_start: int) -> bool:
    clause_start = 0
    for match in _CLAUSE_BOUNDARY_RE.finditer(text, 0, keyword_start):
        clause_start = match.end()
    prefix = text[clause_start:keyword_start]
    if prefix.endswith("不"):
        return True
    last_negative = max((prefix.rfind(cue) for cue in _NEGATION_CUES), default=-1)
    last_positive_reset = max(
        (prefix.rfind(cue) for cue in _POSITIVE_RESET_CUES),
        default=-1,
    )
    return last_negative > last_positive_reset


def _intent_matches(text: str, intent: dict[str, Any]) -> bool:
    for keyword in intent.get("keywords", []):
        for match in re.finditer(re.escape(str(keyword).lower()), text):
            if not _keyword_is_negated(text, match.start()):
                return True
    for pattern in intent.get("patterns", []):
        for match in re.finditer(str(pattern), text):
            if not _keyword_is_negated(text, match.start()):
                return True
    return False


class TaskProfileClassifier:
    """Classify only the current user request; session history is not input."""

    @classmethod
    def classify(
        cls,
        *,
        message: str,
        analytics_model_id: str | None = None,
    ) -> RunTaskProfile:
        normalized = message.strip().lower()
        intents = [
            intent_id
            for intent_id, definition in INTENT_REGISTRY.items()
            if normalized and _intent_matches(normalized, definition)
        ]

        if "ai_insights" in intents and "web_research" in intents:
            explicit_web = _intent_matches(
                normalized,
                {"keywords": ["网页", "链接", "联网搜索", "搜索一下", "http://", "https://"]},
            )
            if not explicit_web:
                intents.remove("web_research")

        packs: list[str] = []
        for intent_id in intents:
            packs.extend(str(item) for item in INTENT_REGISTRY[intent_id].get("packs", []))
        primary = next(
            (intent_id for intent_id in _PRIMARY_PRIORITY if intent_id in intents),
            "general",
        )
        available_context = (
            [f"analytics_model:{analytics_model_id}"]
            if str(analytics_model_id or "").strip()
            else []
        )
        return RunTaskProfile(
            primary_intent=primary,
            intents=intents,
            initial_packs=list(dict.fromkeys(packs)),
            available_context_refs=available_context,
            reasons=[f"intent:{intent_id}" for intent_id in intents],
        )

    @staticmethod
    def skill_ids(profile: RunTaskProfile) -> list[str]:
        return list(
            dict.fromkeys(
                str(INTENT_REGISTRY[intent_id].get("skill") or "")
                for intent_id in profile.intents
                if intent_id in INTENT_REGISTRY
                and str(INTENT_REGISTRY[intent_id].get("skill") or "")
            )
        )


__all__ = ["INTENT_REGISTRY", "TaskProfileClassifier"]
