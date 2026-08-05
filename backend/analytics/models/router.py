"""Bounded routing from one Worker question to an allowed Analytics Model."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage


@dataclass(frozen=True)
class AnalyticsModelRoute:
    status: str
    selected_id: str | None
    confidence: float
    strategy: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_ROUTER_PROMPT = """你是 PuddingClaw 分析模型路由器。分析模型不是底层 LLM，而是数据资产、语义资产、关系和 Guardrail 的业务边界。

任务：只根据用户问题和给定候选模型，判断哪个候选最适合承载本次任务。

规则：
1. 只能选择候选列表里的一个 id，不能创造 id。
2. 优先比较 description、tags 和适用问题；不要因为所有候选都属于同一行业就随意选择。
3. 若两个模型都合理、问题缺少业务域信息或没有合适模型，selected_id 必须为 null。
4. confidence 是 0 到 1；只有明确唯一匹配时才能高于 0.72。
5. 不回答用户问题，不生成 SQL。

只返回 JSON：
{"selected_id":"候选 id 或 null","confidence":0.0,"reason":"简短路由原因"}
"""


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(item.get("text") or item.get("content") or "")
            for item in value
            if isinstance(item, dict)
        )
    return str(value or "")


def _json_payload(value: Any) -> dict[str, Any]:
    text = _content_text(getattr(value, "content", value)).strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("analytics model router response must be an object")
    return payload


class AnalyticsModelRouter:
    """High-precision deterministic routing with one bounded semantic fallback."""

    @staticmethod
    def _safe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        safe: list[dict[str, Any]] = []
        for item in candidates:
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            safe.append(
                {
                    "id": model_id,
                    "name": str(item.get("name") or model_id)[:200],
                    "description": str(item.get("description") or "")[:1000],
                    "tags": [str(tag)[:100] for tag in (item.get("tags") or [])[:30]],
                    "applicability": str(item.get("applicability") or "")[:4000],
                }
            )
        return safe

    @classmethod
    def deterministic(
        cls,
        message: str,
        candidates: list[dict[str, Any]],
    ) -> AnalyticsModelRoute | None:
        safe = cls._safe_candidates(candidates)
        if not safe:
            return AnalyticsModelRoute("unmatched", None, 1.0, "deterministic", "no_allowed_models")
        if len(safe) == 1:
            return AnalyticsModelRoute("matched", safe[0]["id"], 1.0, "single_allowed", "only_allowed_model")
        normalized = " ".join(str(message or "").casefold().split())
        explicit = [
            item
            for item in safe
            if any(
                len(label) >= 2 and label.casefold() in normalized
                for label in {item["id"], item["name"]}
            )
        ]
        if len(explicit) == 1:
            return AnalyticsModelRoute("matched", explicit[0]["id"], 1.0, "explicit_name", "model_name_in_question")
        tag_hits: list[tuple[dict[str, Any], list[str]]] = []
        for item in safe:
            matched = [tag for tag in item["tags"] if len(tag.strip()) >= 2 and tag.casefold() in normalized]
            if matched:
                tag_hits.append((item, matched))
        if len(tag_hits) == 1:
            item, matched = tag_hits[0]
            return AnalyticsModelRoute(
                "matched",
                item["id"],
                0.92,
                "unique_tag",
                "matched_tags:" + ",".join(matched[:3]),
            )
        return None

    @classmethod
    async def route(
        cls,
        *,
        message: str,
        candidates: list[dict[str, Any]],
        model: Any | None,
        confidence_threshold: float = 0.72,
    ) -> AnalyticsModelRoute:
        deterministic = cls.deterministic(message, candidates)
        if deterministic is not None:
            return deterministic
        safe = cls._safe_candidates(candidates)
        if model is None:
            return AnalyticsModelRoute("ambiguous", None, 0.0, "fallback", "classifier_unavailable")
        try:
            response = await model.ainvoke(
                [
                    SystemMessage(content=_ROUTER_PROMPT),
                    HumanMessage(
                        content=(
                            "<question>\n"
                            + str(message or "")[:20_000]
                            + "\n</question>\n<candidates>\n"
                            + json.dumps(safe, ensure_ascii=False, sort_keys=True)
                            + "\n</candidates>"
                        )
                    ),
                ]
            )
            payload = _json_payload(response)
            selected_id = str(payload.get("selected_id") or "").strip() or None
            confidence = min(1.0, max(0.0, float(payload.get("confidence") or 0.0)))
            allowed_ids = {item["id"] for item in safe}
            reason = str(payload.get("reason") or "semantic_classifier")[:300]
            if selected_id not in allowed_ids or confidence < confidence_threshold:
                return AnalyticsModelRoute("ambiguous", None, confidence, "semantic", reason or "low_confidence")
            return AnalyticsModelRoute("matched", selected_id, confidence, "semantic", reason)
        except Exception as exc:
            return AnalyticsModelRoute(
                "ambiguous",
                None,
                0.0,
                "fallback",
                f"classifier_error:{type(exc).__name__}",
            )
