"""Generic entity candidate recommendation for NL2SQL training.

Entity candidates are not business rules. They are lightweight hints that help
the UI suggest which columns may contain reusable business values, such as
regions, products, customers, statuses, departments, categories, or any
domain-specific label chosen by the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


TEXTUAL_DTYPE_MARKERS = (
    "object",
    "string",
    "category",
    "bool",
    "boolean",
    "text",
    "char",
    "varchar",
    "character",
)

BUSINESS_ENTITY_TYPE_LABELS = {
    ("vehicle_params", "car_name"): "款型",
}

POSITIVE_NAME_HINTS = (
    "name",
    "title",
    "type",
    "category",
    "class",
    "group",
    "status",
    "region",
    "city",
    "province",
    "country",
    "area",
    "market",
    "segment",
    "channel",
    "store",
    "customer",
    "supplier",
    "vendor",
    "product",
    "item",
    "名称",
    "名字",
    "标题",
    "类型",
    "类别",
    "分类",
    "分组",
    "状态",
    "地区",
    "区域",
    "城市",
    "省份",
    "国家",
    "市场",
    "渠道",
    "门店",
    "客户",
    "供应商",
    "产品",
    "商品",
)

NEGATIVE_NAME_HINTS = (
    "id",
    "uuid",
    "guid",
    "code",
    "hash",
    "url",
    "uri",
    "path",
    "file",
    "date",
    "time",
    "timestamp",
    "created",
    "updated",
    "amount",
    "price",
    "cost",
    "qty",
    "quantity",
    "count",
    "ratio",
    "rate",
    "score",
    "value",
    "编号",
    "编码",
    "代码",
    "路径",
    "链接",
    "日期",
    "时间",
    "金额",
    "价格",
    "成本",
    "数量",
    "销量",
    "占比",
    "比例",
    "得分",
    "数值",
)


@dataclass(frozen=True)
class EntityCandidate:
    column: str
    suggested_entity_type: str
    score: float
    reasons: list[str]
    sample_values: list[str]
    table_column: str | None = None
    distinct_count: int | None = None
    distinct_ratio: float | None = None
    dtype: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "suggested_entity_type": self.suggested_entity_type,
            "score": round(self.score, 3),
            "reasons": self.reasons,
            "sample_values": self.sample_values,
            "table_column": self.table_column,
            "distinct_count": self.distinct_count,
            "distinct_ratio": self.distinct_ratio,
            "dtype": self.dtype,
        }


def recommend_entity_candidates(
    profile: dict[str, Any],
    *,
    table_name: str | None = None,
    max_candidates: int = 12,
) -> list[dict[str, Any]]:
    """Recommend generic entity columns from a table profile.

    The function deliberately avoids domain-specific labels. The output is only
    a starting point for the user to confirm and name the entity type before it
    becomes Vanna training data.
    """

    columns = profile.get("columns") if isinstance(profile, dict) else None
    if not isinstance(columns, list):
        return []

    shape = profile.get("shape") if isinstance(profile.get("shape"), list) else []
    row_count = int(shape[0]) if shape else None
    candidates: list[EntityCandidate] = []

    for column in columns:
        if not isinstance(column, dict):
            continue
        candidate = _score_column(column, row_count=row_count, table_name=table_name)
        if candidate and candidate.score >= 0.3:
            candidates.append(candidate)

    candidates.sort(key=lambda item: item.score, reverse=True)
    return [candidate.to_dict() for candidate in candidates[:max_candidates]]


def _score_column(column: dict[str, Any], *, row_count: int | None, table_name: str | None) -> EntityCandidate | None:
    name = str(column.get("name") or "").strip()
    if not name:
        return None

    dtype = str(column.get("dtype") or "").lower()
    sample_values = [str(value) for value in column.get("sample_values") or [] if str(value).strip()]
    non_null = _safe_int(column.get("non_null"))
    distinct_count = _safe_int(column.get("distinct_count"))
    distinct_ratio = _safe_float(column.get("distinct_ratio"))
    if distinct_ratio is None and distinct_count is not None and row_count:
        distinct_ratio = distinct_count / max(row_count, 1)

    score = 0.0
    reasons: list[str] = []

    if any(marker in dtype for marker in TEXTUAL_DTYPE_MARKERS):
        score += 0.25
        reasons.append("文本/枚举类型")
    elif dtype:
        score -= 0.2
        reasons.append("非文本类型，默认不优先作为实体")

    lowered_name = name.lower()
    if any(hint in lowered_name for hint in POSITIVE_NAME_HINTS):
        score += 0.25
        reasons.append("列名像业务维度")
    if any(hint in lowered_name for hint in NEGATIVE_NAME_HINTS):
        score -= 0.3
        reasons.append("列名像技术字段或数值指标")

    if distinct_count is not None:
        if 2 <= distinct_count <= 5000:
            score += 0.25
            reasons.append("唯一值数量适合做标准值映射")
        elif distinct_count <= 1:
            score -= 0.25
            reasons.append("唯一值过少")
        else:
            score -= 0.1
            reasons.append("唯一值较多，可能不是稳定实体")

    if distinct_ratio is not None:
        if 0.001 <= distinct_ratio <= 0.5:
            score += 0.15
            reasons.append("唯一值占比适中")
        elif distinct_ratio > 0.9:
            score -= 0.25
            reasons.append("几乎每行不同，像 ID 或明细文本")

    if sample_values:
        avg_len = sum(len(value) for value in sample_values) / len(sample_values)
        if avg_len <= 80:
            score += 0.1
            reasons.append("样例值较短，适合别名/标准值")
        else:
            score -= 0.2
            reasons.append("样例值较长，像自由文本")

    if non_null is not None and row_count and non_null / max(row_count, 1) < 0.2:
        score -= 0.15
        reasons.append("非空率偏低")

    score = max(0.0, min(1.0, score))
    if score <= 0:
        return None

    table_column = f"{table_name}.{name}" if table_name else name
    return EntityCandidate(
        column=name,
        suggested_entity_type=_normalize_entity_type(name, table_name=table_name),
        score=score,
        reasons=reasons or ["通用候选"],
        sample_values=sample_values[:10],
        table_column=table_column,
        distinct_count=distinct_count,
        distinct_ratio=round(distinct_ratio, 4) if distinct_ratio is not None else None,
        dtype=str(column.get("dtype") or ""),
    )


def _normalize_entity_type(name: str, *, table_name: str | None = None) -> str:
    clean_name = name.strip()
    clean_table = (table_name or "").strip().split(".")[-1]
    mapped_label = BUSINESS_ENTITY_TYPE_LABELS.get((clean_table, clean_name))
    if mapped_label:
        return mapped_label

    normalized = re.sub(r"\s+", "_", name.strip().lower())
    normalized = re.sub(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", "_", normalized)
    normalized = normalized.strip("_")
    return normalized or "entity"


def _safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
