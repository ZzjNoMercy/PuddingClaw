"""Deterministic semantic asset resolver for database QA."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .registry import get_semantic_asset_registry

TOKEN_RE = re.compile(r"[0-9A-Za-z_\u4e00-\u9fff]+")
MAX_MATCHED_ASSETS = 8
MAX_BODY_CHARS = 2400
GENERIC_CJK_BIGRAMS = {
    "车型",
    "配置",
    "置率",
    "查询",
    "统计",
    "分析",
    "计算",
    "数据",
    "规则",
    "用户",
    "字段",
    "口径",
    "维度",
    "类型",
    "目标",
    "当前",
    "时间",
    "型的",
    "某车",
    "型配",
    "搭载",
    "间为",
    "为小",
    "为快",
}


@dataclass(frozen=True)
class ResolvedSemanticAsset:
    id: str
    name: str
    type: str
    path: str
    match_score: float
    match_reason: str
    body: str
    aliases: list[str]
    tags: list[str]
    parent_id: str = ""

    def to_prompt_block(self) -> str:
        labels = {
            "measure": "度量值",
            "dimension": "维度",
            "grain": "颗粒度",
            "measure_reference": "度量值 Reference",
        }
        label = labels.get(self.type, self.type)
        body = self.body.strip()
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS].rstrip() + "\n...（已截断）"
        return f"[{label}：{self.name}]\n路径：{self.path}\n命中原因：{self.match_reason}\n\n{body}"

    def to_trace(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "path": self.path,
            "match_score": round(self.match_score, 3),
            "match_reason": self.match_reason,
            "aliases": self.aliases,
            "tags": self.tags,
            "parent_id": self.parent_id,
            "body_preview": self.body[:1200],
            "body_chars": len(self.body),
        }


def _tokens(value: str | None) -> set[str]:
    return {token.lower() for token in TOKEN_RE.findall(str(value or "")) if token.strip()}


def _cjk_bigrams(value: str | None) -> set[str]:
    chars = [char for char in str(value or "") if "\u4e00" <= char <= "\u9fff"]
    return {
        chars[index] + chars[index + 1]
        for index in range(len(chars) - 1)
        if chars[index] + chars[index + 1] not in GENERIC_CJK_BIGRAMS
    }


def _contains_phrase(question: str, phrase: str) -> bool:
    phrase = phrase.strip()
    if not phrase:
        return False
    return phrase.lower() in question.lower()


def _score_text_against_question(
    question: str,
    *,
    name: str,
    aliases: list[str],
    tags: list[str],
    description: str,
    body: str,
) -> tuple[float, str]:
    question_tokens = _tokens(question)
    score = 0.0
    reasons: list[str] = []

    if _contains_phrase(question, name):
        score += 30.0
        reasons.append(f"name contains {name}")
    for alias in aliases:
        if _contains_phrase(question, alias):
            score += 20.0
            reasons.append(f"alias contains {alias}")
    for tag in tags:
        if _contains_phrase(question, tag):
            score += 8.0
            reasons.append(f"tag contains {tag}")

    metadata_tokens = _tokens(" ".join([name, *aliases, *tags, description]))
    metadata_hits = sorted(question_tokens & metadata_tokens)
    if metadata_hits:
        score += min(12.0, 4.0 * len(metadata_hits))
        reasons.append(f"metadata token hit: {', '.join(metadata_hits[:6])}")

    body_hits = sorted(question_tokens & _tokens(body))
    if body_hits:
        score += min(10.0, 1.5 * len(body_hits))
        reasons.append(f"body token hit: {', '.join(body_hits[:6])}")

    cjk_hits = sorted(
        _cjk_bigrams(question)
        & _cjk_bigrams(" ".join([name, *aliases, *tags, description, body[:1200]]))
    )
    if cjk_hits:
        score += min(10.0, 2.0 * len(cjk_hits))
        reasons.append(f"phrase hit: {', '.join(cjk_hits[:6])}")

    return score, "；".join(reasons[:4])


def _reference_name_from_path(path: str) -> str:
    stem = Path(path).stem
    return stem.replace("_", " ").replace("-", " ")


def _resolve_measure_references(
    question: str,
    *,
    base_dir: Path,
    measure: ResolvedSemanticAsset,
    registry: Any,
    max_references: int = 4,
) -> list[ResolvedSemanticAsset]:
    try:
        detail = registry.get_asset(measure.id)
    except Exception:
        return []
    files = detail.get("files") if isinstance(detail, dict) else []
    if not isinstance(files, list):
        return []

    scored: list[tuple[float, str, ResolvedSemanticAsset]] = []
    for raw_file in files:
        if not isinstance(raw_file, dict):
            continue
        relative_path = str(raw_file.get("relative_path") or "")
        path = str(raw_file.get("path") or "")
        if not relative_path.startswith("references/") or not path.endswith(".md"):
            continue
        full_path = (base_dir / path).resolve()
        try:
            body = full_path.read_text(encoding="utf-8")
        except Exception:
            continue
        name = _reference_name_from_path(relative_path)
        score, reason = _score_text_against_question(
            question,
            name=name,
            aliases=[],
            tags=[],
            description="",
            body=body,
        )
        if score < 6:
            continue
        scored.append(
            (
                score,
                reason,
                ResolvedSemanticAsset(
                    id=f"{measure.id}:references/{Path(relative_path).stem}",
                    name=name,
                    type="measure_reference",
                    path=path,
                    match_score=score,
                    match_reason=f"parent {measure.name}；{reason}",
                    body=body,
                    aliases=[],
                    tags=[],
                    parent_id=measure.id,
                ),
            )
        )
    scored.sort(key=lambda item: item[0], reverse=True)
    return [item[2] for item in scored[:max_references]]


def resolve_semantic_assets(
    question: str,
    *,
    base_dir: Path | None = None,
    requested_ids: list[str] | None = None,
    max_assets: int = MAX_MATCHED_ASSETS,
) -> dict[str, Any]:
    """Resolve semantic assets relevant to a question.

    This intentionally avoids an LLM call. It returns a transparent, traceable
    candidate set based on explicit ids plus name/alias/tag/body token matches.
    """

    registry = get_semantic_asset_registry(base_dir)
    resolved_base_dir = (base_dir or Path(__file__).resolve().parents[2]).resolve()
    snapshot = registry.list_assets()
    requested = {item.strip() for item in (requested_ids or []) if item and item.strip()}
    scored: list[tuple[float, str, dict[str, Any]]] = []

    for summary in snapshot.get("assets") or []:
        asset_id = str(summary.get("id") or "")
        # Asset relations are model-scoped graph edges. They are resolved only
        # through the selected analytics model, never by free-text retrieval.
        if str(summary.get("type") or "") == "relation":
            continue
        try:
            detail = registry.get_asset(asset_id)
        except Exception:
            continue
        name = str(detail.get("name") or "")
        aliases = [str(item) for item in detail.get("aliases") or []]
        tags = [str(item) for item in detail.get("tags") or []]
        body = str(detail.get("body") or "")
        asset_type = str(detail.get("type") or "")
        score = 0.0
        reasons: list[str] = []

        if asset_id in requested:
            score += 100.0
            reasons.append("explicit asset id")
        text_score, text_reason = _score_text_against_question(
            question,
            name=name,
            aliases=aliases,
            tags=tags,
            description=str(detail.get("description") or ""),
            body=body,
        )
        score += text_score
        if text_reason:
            reasons.append(text_reason)

        if asset_type == "grain" and asset_id not in requested:
            explicit_grain_hit = _contains_phrase(question, name) or any(
                _contains_phrase(question, alias) for alias in aliases
            )
            if not explicit_grain_hit:
                score = 0.0
                reasons = []

        if score > 0:
            scored.append((score, "；".join(reasons[:4]), detail))

    scored.sort(key=lambda item: item[0], reverse=True)
    matched: list[ResolvedSemanticAsset] = []
    for score, reason, detail in scored[:max_assets]:
        matched.append(
            ResolvedSemanticAsset(
                id=str(detail.get("id") or ""),
                name=str(detail.get("name") or ""),
                type=str(detail.get("type") or ""),
                path=str(detail.get("path") or ""),
                match_score=score,
                match_reason=reason,
                body=str(detail.get("body") or ""),
                aliases=[str(item) for item in detail.get("aliases") or []],
                tags=[str(item) for item in detail.get("tags") or []],
            )
        )

    references: list[ResolvedSemanticAsset] = []
    for asset in matched:
        if asset.type != "measure":
            continue
        references.extend(
            _resolve_measure_references(
                question,
                base_dir=resolved_base_dir,
                measure=asset,
                registry=registry,
            )
        )

    matched_ids = {item.id for item in matched}
    unmatched_requested = sorted(requested - matched_ids)
    return {
        "matched": matched,
        "references": references,
        "matched_count": len(matched),
        "reference_count": len(references),
        "available_count": snapshot.get("count", 0),
        "type_counts": snapshot.get("type_counts") or {},
        "unmatched_requested_ids": unmatched_requested,
    }


def format_semantic_assets_for_prompt(resolution: dict[str, Any]) -> str:
    matched = resolution.get("matched") or []
    references = resolution.get("references") or []
    if not matched and not references:
        return (
            "语义资产定义：本轮未命中任何度量值、颗粒度、维度或度量值 references。\n"
            "如果问题涉及业务口径，请不要自行从字段名或款型名称猜测；必要时生成保守 SQL 或要求补充定义。"
        )
    blocks = [asset.to_prompt_block() for asset in matched]
    reference_blocks = [asset.to_prompt_block() for asset in references]
    if reference_blocks:
        blocks.extend(reference_blocks)
    return (
        "已命中语义资产定义。以下定义优先级高于字段名猜测和模型自行推断：\n\n"
        + "\n\n---\n\n".join(blocks)
        + "\n\n规则：\n"
        "- 必须优先遵守以上语义资产定义。\n"
        "- 命中度量值后，如存在匹配的 Reference，必须优先遵守 Reference 中的特定识别规则。\n"
        "- 维度定义高于字段名猜测。\n"
        "- 度量值定义高于模型自行推断。\n"
        "- 如果语义资产声明禁止某种字段推断，不得使用该推断。"
    )


def semantic_resolution_to_trace(resolution: dict[str, Any]) -> dict[str, Any]:
    matched = resolution.get("matched") or []
    references = resolution.get("references") or []
    return {
        "matched": [asset.to_trace() for asset in matched],
        "references": [asset.to_trace() for asset in references],
        "matched_count": len(matched),
        "reference_count": len(references),
        "available_count": resolution.get("available_count", 0),
        "type_counts": resolution.get("type_counts") or {},
        "unmatched_requested_ids": resolution.get("unmatched_requested_ids") or [],
        "prompt_injected": bool(matched or references),
    }
