"""Markdown and error formatting helpers for database tools."""

from __future__ import annotations

from typing import Any

from analytics.nl2sql.schemas import DatabaseQueryResult
from analytics.nl2sql.service import DatabaseKnowledgeQueryError


def markdown_table(rows: list[dict[str, Any]], columns: list[str], *, max_rows: int = 20) -> list[str]:
    if not columns:
        return ["无结果行。"]
    visible_rows = rows[:max_rows]
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in visible_rows:
        values = [str(row.get(column, ""))[:160].replace("\n", " ") for column in columns]
        lines.append("| " + " | ".join(values) + " |")
    if len(rows) > max_rows:
        lines.append(
            f"\n仅预览前 {max_rows} 行，完整执行明细请在 Trace 面板中选择对应数据库工具调用查看。"
        )
    return lines


def format_profile(profile: dict[str, Any]) -> list[str]:
    if not profile:
        return []
    lines = ["", "- Profile："]
    group_counts = profile.get("group_counts") if isinstance(profile.get("group_counts"), dict) else {}
    for column, counts in group_counts.items():
        if not isinstance(counts, dict) or not counts:
            continue
        lines.append(f"  - {column} 分布：")
        for value, count in list(counts.items())[:20]:
            lines.append(f"    - {value}: {count}")
    date_ranges = profile.get("date_ranges") if isinstance(profile.get("date_ranges"), dict) else {}
    for column, range_info in date_ranges.items():
        if isinstance(range_info, dict):
            lines.append(f"  - {column} 范围：{range_info.get('min')} ~ {range_info.get('max')}")
    numeric_ranges = profile.get("numeric_ranges") if isinstance(profile.get("numeric_ranges"), dict) else {}
    for column, range_info in numeric_ranges.items():
        if isinstance(range_info, dict):
            lines.append(f"  - {column} 范围：{range_info.get('min')} ~ {range_info.get('max')}")
    return lines


def format_actions(actions: list[dict[str, Any]]) -> list[str]:
    if not actions:
        return []
    lines = ["", "- 可用动作："]
    for action in actions:
        action_type = action.get("type")
        available = "可用" if action.get("available") else "不可用"
        detail = ""
        if action_type == "fetch_page" and action.get("available"):
            detail = (
                f"，请调用 database_query_result_page(result_id, page, page_size)，"
                f"默认 page_size={action.get('page_size')}"
            )
        elif action.get("reason"):
            detail = f"，原因：{action.get('reason')}"
        lines.append(f"  - {action_type}: {available}{detail}")
    return lines


def format_query_error(exc: DatabaseKnowledgeQueryError) -> str:
    lines = [f"🧮 数据库问数失败：{exc}"]
    sql = str(getattr(exc, "sql", "") or "").strip()
    if sql:
        lines.extend(["", "生成 SQL：", "```sql", sql, "```"])
    return "\n".join(lines)


def format_result(result: DatabaseQueryResult) -> str:
    execution = result.execution
    result_size = f"{execution.row_count} 行"
    if execution.is_complete:
        result_size += "（全部）"
    else:
        result_size += f"（展示 {execution.preview_count or len(execution.rows)} 行，省略 {execution.omitted_count} 行）"
    lines = [
        "🧮 数据库问数结果",
        f"- 数据源：{result.source.get('name')}",
        f"- 表：{', '.join(result.route.table_names)}",
        f"- 结果：{result_size}",
        f"- 完整性：{'完整明细已进入模型上下文' if execution.is_complete else '预览明细，不能据此判断未展示类别不存在'}",
    ]
    semantic_assets = result.semantic_assets or {}
    matched_assets = semantic_assets.get("matched") if isinstance(semantic_assets.get("matched"), list) else []
    if matched_assets:
        asset_names = [
            f"{item.get('name')}({item.get('type')})"
            for item in matched_assets[:8]
            if isinstance(item, dict) and item.get("name")
        ]
        lines.append(f"- 语义资产：已注入 {len(matched_assets)} 个，{', '.join(asset_names)}")
    else:
        lines.append("- 语义资产：本轮未命中，SQL 未获得度量值/维度正文约束")
    if result.stage_timings:
        total_seconds = (result.stage_timings.get("total_ms") or 0) / 1000
        sql_seconds = (result.stage_timings.get("sql_execution_ms") or 0) / 1000
        generation_seconds = (result.stage_timings.get("sql_generation_ms") or 0) / 1000
        semantic_seconds = (result.stage_timings.get("semantic_assets_ms") or 0) / 1000
        lines.append(
            f"- 耗时：总计 {total_seconds:.2f}s，语义资产 {semantic_seconds:.2f}s，"
            f"SQL生成 {generation_seconds:.2f}s，SQL执行 {sql_seconds:.2f}s"
        )
    if execution.result_id:
        lines.extend(
            [
                f"- result_id：{execution.result_id}",
                f"- 持久化：{execution.result_store.get('artifact_path')}（过期时间：{execution.result_store.get('expires_at')}）",
            ]
        )
    if execution.llm_guardrail:
        lines.append(f"- Guardrail：{execution.llm_guardrail}")
    lines.extend(format_profile(execution.profile))
    lines.extend(format_actions(execution.actions))
    lines.extend(
        [
            "",
            "- SQL：",
            "```sql",
            result.sql,
            "```",
            "",
            *markdown_table(execution.rows, execution.columns, max_rows=len(execution.rows) or 20),
        ]
    )
    return "\n".join(lines)
