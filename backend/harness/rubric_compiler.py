"""Compile a deterministic first-pass Run rubric from task context."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from harness.models import (
    CriterionSource,
    RunVerificationContract,
    VerificationCriterion,
    VerifierKind,
)

_ANALYTICS_TERMS = (
    "分析",
    "原因",
    "趋势",
    "销量",
    "收入",
    "毛利",
    "指标",
    "数据",
    "同比",
    "环比",
    "占比",
    "贡献",
)
_ARTIFACT_TERMS = (
    "创建",
    "报告",
    "模板",
    "文档",
    "表格",
    "图表",
    "文件",
    "刷新",
    "更新",
    "生成",
)
_TIME_PATTERN = re.compile(
    r"(?:20\d{2}[-/.年]\s*\d{1,2}(?:月)?|"
    r"\d{1,2}\s*月|"
    r"最近\s*[一二三四五六七八九十\d]+\s*(?:天|周|月|季|年)|"
    r"(?:本|上|下)(?:周|月|季度|年)|"
    r"(?:第一|第二|第三|第四|一|二|三|四)季度)"
)


@dataclass(frozen=True)
class RubricBuildContext:
    user_message: str
    analytics_model_id: str | None = None
    project_id: str | None = None
    custom_rules: tuple[dict, ...] = ()
    force_required: bool = False


class RunRubricCompiler:
    """Build a typed, versioned Run contract without requiring user upkeep."""

    VERSION = "analytics-run-v1"

    @classmethod
    def should_verify(cls, context: RubricBuildContext) -> bool:
        message = context.user_message.strip()
        if not message:
            return False
        if context.force_required:
            return True
        if context.analytics_model_id:
            return True
        return any(term in message for term in (*_ANALYTICS_TERMS, *_ARTIFACT_TERMS))

    @classmethod
    def compile(cls, context: RubricBuildContext) -> RunVerificationContract | None:
        message = context.user_message.strip()
        if not cls.should_verify(context):
            return None

        criteria: list[VerificationCriterion] = [
            VerificationCriterion(
                id="task_fulfillment",
                statement="最终结果必须完成用户本次 Run 明确提出的任务，不能只给计划或声称已完成。",
                source=CriterionSource.TASK,
                verifier=VerifierKind.LLM_GRADER,
            ),
            VerificationCriterion(
                id="todo_reconciliation",
                statement="若本 Run 使用了 Todo，结束时所有 Todo 必须已完成或明确取消。",
                source=CriterionSource.MANAGED,
                verifier=VerifierKind.DETERMINISTIC,
            ),
        ]
        analytics_task = bool(
            context.analytics_model_id
            or any(term in message for term in _ANALYTICS_TERMS)
        )
        artifact_task = any(term in message for term in _ARTIFACT_TERMS)

        if analytics_task:
            criteria.extend(
                [
                    VerificationCriterion(
                        id="metric_consistency",
                        statement="指标名称、计算口径、维度和结论必须前后一致；未知口径必须明确说明。",
                        source=CriterionSource.SYSTEM,
                        verifier=VerifierKind.ANALYTICS,
                    ),
                    VerificationCriterion(
                        id="evidence_traceability",
                        statement="关键数据与结论必须能追溯到本 Run 的工具结果、数据源或产物证据。",
                        source=CriterionSource.SYSTEM,
                        verifier=VerifierKind.ANALYTICS,
                    ),
                ]
            )
        if _TIME_PATTERN.search(message):
            criteria.append(
                VerificationCriterion(
                    id="time_scope",
                    statement="必须明确并遵守用户要求的数据时间范围，不能用其他期间替代。",
                    source=CriterionSource.USER,
                    verifier=VerifierKind.ANALYTICS,
                )
            )
        if artifact_task:
            criteria.append(
                VerificationCriterion(
                    id="artifact_delivery",
                    statement="要求生成或更新的产物必须真实存在，并在最终回答中给出可定位的路径或引用。",
                    source=CriterionSource.TASK,
                    verifier=VerifierKind.DETERMINISTIC,
                )
            )
        if "报告" in message or "模板" in message:
            criteria.append(
                VerificationCriterion(
                    id="report_integrity",
                    statement="报告的既定结构、标题、图表与正文必须保持完整，更新内容不能破坏模板。",
                    source=CriterionSource.TASK,
                    verifier=VerifierKind.LLM_GRADER,
                )
            )
        existing_ids = {criterion.id for criterion in criteria}
        for index, rule in enumerate(context.custom_rules):
            if not isinstance(rule, dict) or not rule.get("enabled", True):
                continue
            rule_id = str(rule.get("id") or f"custom_{index + 1}")
            if rule_id in existing_ids:
                continue
            statement = str(rule.get("statement") or "").strip()
            if not statement:
                continue
            verifier_value = str(rule.get("verifier") or "llm_grader")
            try:
                verifier = VerifierKind(verifier_value)
            except ValueError:
                continue
            criteria.append(
                VerificationCriterion(
                    id=rule_id,
                    statement=statement,
                    source=CriterionSource.SETTINGS,
                    verifier=verifier,
                    required=bool(rule.get("required", True)),
                )
            )
            existing_ids.add(rule_id)

        rubric_lines = [
            "逐项审查以下标准；只有全部 required 标准有正面证据时才能判定 satisfied："
        ]
        rubric_lines.extend(
            f"- [{criterion.id}] {criterion.statement}" for criterion in criteria
        )
        digest = hashlib.sha256(
            (
                cls.VERSION
                + "\n"
                + message
                + "\n"
                + str(context.analytics_model_id or "")
                + "\n"
                + str(context.force_required)
                + "\n"
                + "\n".join(item.id for item in criteria)
            ).encode("utf-8")
        ).hexdigest()[:20]
        return RunVerificationContract(
            contract_id=f"run-contract-{digest}",
            version=cls.VERSION,
            task_type=cls._task_type(
                analytics_task=analytics_task,
                artifact_task=artifact_task,
            ),
            criteria=criteria,
            rubric="\n".join(rubric_lines),
        )

    @staticmethod
    def _task_type(*, analytics_task: bool, artifact_task: bool) -> str:
        if analytics_task and artifact_task:
            return "analytics_artifact"
        if analytics_task:
            return "analytics"
        if artifact_task:
            return "artifact"
        return "general"
