"""分析模型 acceptance.invariants 的确定性验收引擎。

模型在 model.md frontmatter 声明 ``acceptance.invariants``（type + target），
本引擎按 type 分发到注册的检查函数。未注册的 type 与未声明 acceptance 的
模型一律跳过，避免模型写了未知类型就把 Run 卡死。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

_CONTAINMENT_PATTERN = re.compile(r"(?<!不)包括|(?<!不)含|：|:")
_SENTENCE_SPLIT = re.compile(r"[\n。；;！？!?]+")


def _model_frontmatter(model_id: str) -> dict[str, Any]:
    from analytics.models import get_analytics_model_registry

    detail = get_analytics_model_registry().get_model(model_id)
    frontmatter = detail.get("frontmatter")
    return frontmatter if isinstance(frontmatter, dict) else {}


def _asset_frontmatter(asset_id: str) -> dict[str, Any]:
    from analytics.semantic_assets import get_semantic_asset_registry

    detail = get_semantic_asset_registry().get_asset(asset_id)
    frontmatter = detail.get("frontmatter")
    return frontmatter if isinstance(frontmatter, dict) else {}


def declared_invariants(frontmatter: dict[str, Any]) -> list[dict[str, Any]]:
    acceptance = frontmatter.get("acceptance") if isinstance(frontmatter, dict) else None
    raw = acceptance.get("invariants") if isinstance(acceptance, dict) else None
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def load_model_invariants(analytics_model_id: str | None) -> list[dict[str, Any]]:
    """编译期使用：模型不存在或读取失败时按未声明处理，不影响契约编译。"""

    model_id = str(analytics_model_id or "").strip()
    if not model_id:
        return []
    try:
        return declared_invariants(_model_frontmatter(model_id))
    except Exception:
        return []


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict)
        )
    return str(content or "")


def _final_answer_text(final_state: dict[str, Any]) -> str:
    messages = final_state.get("messages") or []
    for message in reversed(messages):
        if isinstance(message, dict):
            role = str(message.get("role") or message.get("type") or "").lower()
            content = message.get("content")
        else:
            role = str(getattr(message, "type", "") or "").lower()
            content = getattr(message, "content", "")
        if role in {"ai", "assistant", "aimessage"}:
            return _content_text(content)
    context = final_state.get("_harness_context")
    if isinstance(context, dict) and context.get("final_content"):
        return str(context.get("final_content") or "")
    return str(final_state.get("final_content") or "")


def _check_classification_mapping_declaration(
    *,
    target: str,
    final_state: dict[str, Any],
) -> list[str]:
    frontmatter = _asset_frontmatter(target)
    classifications = frontmatter.get("classifications")
    enum_universe = frontmatter.get("enum_universe")
    if not isinstance(classifications, dict) or not isinstance(enum_universe, list):
        return []
    universe = [str(value) for value in enum_universe if str(value).strip()]
    if not universe:
        return []
    answer = _final_answer_text(final_state)
    if not answer:
        return []
    segments = [segment for segment in _SENTENCE_SPLIT.split(answer) if segment.strip()]
    violations: list[str] = []
    for label, declared_raw in classifications.items():
        label_text = str(label)
        if label_text not in answer:
            continue
        declared = {str(value) for value in (declared_raw or [])}
        # 长的枚举值先匹配，避免「柴油+48V轻混系统」同时报出「柴油」两条。
        candidates = sorted(
            (value for value in universe if value not in declared),
            key=len,
            reverse=True,
        )
        for segment in segments:
            if label_text not in segment or not _CONTAINMENT_PATTERN.search(segment):
                continue
            flagged: list[str] = []
            for value in candidates:
                if value not in segment:
                    continue
                if any(value in hit for hit in flagged):
                    continue
                flagged.append(value)
            for value in flagged:
                violations.append(
                    f"答复将枚举值「{value}」归入「{label_text}」，但语义资产 {target} "
                    f"声明的映射为：{'、'.join(sorted(declared)) or '（空）'}"
                )
    return violations


INVARIANT_TYPES: dict[str, Callable[..., list[str]]] = {
    "classification_mapping_declaration": _check_classification_mapping_declaration,
}


def evaluate_model_invariants(
    analytics_model_id: str,
    final_state: dict[str, Any],
) -> list[str]:
    """返回违规描述列表，空列表表示全部通过。"""

    violations: list[str] = []
    for invariant in declared_invariants(_model_frontmatter(str(analytics_model_id or ""))):
        check = INVARIANT_TYPES.get(str(invariant.get("type") or ""))
        if check is None:
            continue
        violations.extend(
            check(target=str(invariant.get("target") or ""), final_state=final_state)
        )
    return violations


__all__ = [
    "INVARIANT_TYPES",
    "declared_invariants",
    "evaluate_model_invariants",
    "load_model_invariants",
]
