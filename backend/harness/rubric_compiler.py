"""Compile declared and effective Run verification contracts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from harness.analytics_invariants import load_model_invariants
from harness.models import (
    CriterionSource,
    EvidenceScope,
    RunTaskProfile,
    RunVerificationContract,
    VerificationActivation,
    VerificationCriterion,
    VerifierKind,
)
from harness.task_profiles import INTENT_REGISTRY, TaskProfileClassifier

_TIME_PATTERN = re.compile(
    r"(?:20\d{2}(?:\s*年(?:\s*\d{1,2}月)?|[-/.]\s*\d{1,2}(?:月)?)|"
    r"\d{1,2}\s*月|"
    r"最近\s*[一二三四五六七八九十\d]+\s*(?:天|周|月|季|年)|"
    r"(?:本|上|下)(?:周|月|季度|年)|"
    r"(?:第一|第二|第三|第四|一|二|三|四)季度)"
)
_BROWSER_E2E_PATTERN = re.compile(
    r"(?is)(?:"
    r"(?:e2e|end[\s-]*to[\s-]*end|端到端|浏览器(?:运行)?|playwright)"
    r".{0,24}?(?:测试|验证|验收|检查|跑|运行|执行|开展|进行|开启|要求|必须|需要)"
    r"|"
    r"(?:测试|验证|验收|检查|跑|运行|执行|开展|进行|开启|要求|必须|需要)"
    r".{0,24}?(?:e2e|end[\s-]*to[\s-]*end|端到端|浏览器(?:运行)?|playwright)"
    r")"
)
_BROWSER_E2E_NEGATION = re.compile(
    r"(?is)(?:不要|无需|不需要|不必|跳过|关闭|禁止|请勿).{0,12}"
    r"(?:e2e|end[\s-]*to[\s-]*end|端到端|浏览器(?:运行)?|playwright)"
)


def browser_e2e_requested(message: str) -> bool:
    """Return true only for an explicit positive browser/E2E requirement."""

    return bool(
        message
        and _BROWSER_E2E_PATTERN.search(message)
        and not _BROWSER_E2E_NEGATION.search(message)
    )


@dataclass(frozen=True)
class RubricBuildContext:
    user_message: str
    analytics_model_id: str | None = None
    project_id: str | None = None
    custom_rules: tuple[dict, ...] = ()
    force_required: bool = False
    task_profile: RunTaskProfile | None = None


def _criterion(
    criterion_id: str,
    statement: str,
    *,
    source: CriterionSource,
    verifier: VerifierKind,
    evidence_scope: EvidenceScope = EvidenceScope.RUN_ONLY,
) -> VerificationCriterion:
    return VerificationCriterion(
        id=criterion_id,
        statement=statement,
        source=source,
        verifier=verifier,
        evidence_scope=evidence_scope,
    )


_PACK_CRITERIA: dict[str, tuple[VerificationCriterion, ...]] = {
    "core": (
        _criterion(
            "task_fulfillment",
            "最终结果必须完成用户本次 Run 明确提出的任务，不能只给计划或声称已完成。",
            source=CriterionSource.TASK,
            verifier=VerifierKind.LLM_GRADER,
            evidence_scope=EvidenceScope.RUN_ONLY,
        ),
        _criterion(
            "todo_reconciliation",
            "若本 Run 使用了 Todo，结束时所有 Todo 必须已完成或明确取消。",
            source=CriterionSource.MANAGED,
            verifier=VerifierKind.DETERMINISTIC,
            evidence_scope=EvidenceScope.GOAL_INHERITABLE,
        ),
        _criterion(
            "tool_protocol_integrity",
            "所有工具调用必须拥有对应 ToolMessage，不能以缺失工具结果的协议状态结束。",
            source=CriterionSource.SYSTEM,
            verifier=VerifierKind.DETERMINISTIC,
            evidence_scope=EvidenceScope.RUN_ONLY,
        ),
    ),
    "web_research": (
        _criterion(
            "web_evidence_traceability",
            "关键事实与结论必须能追溯到本 Run 成功完成的检索工具或知识来源。",
            source=CriterionSource.SYSTEM,
            verifier=VerifierKind.DETERMINISTIC,
            evidence_scope=EvidenceScope.GOAL_INHERITABLE,
        ),
    ),
    "analytics": (
        _criterion(
            "metric_consistency",
            "指标名称、计算口径、维度和结论必须前后一致；未知口径必须明确说明。",
            source=CriterionSource.SYSTEM,
            verifier=VerifierKind.LLM_GRADER,
            evidence_scope=EvidenceScope.RUN_ONLY,
        ),
        _criterion(
            "analytics_evidence_traceability",
            "关键数据与结论必须能追溯到本 Run 成功完成的查询、数据源或产物证据。",
            source=CriterionSource.SYSTEM,
            verifier=VerifierKind.DETERMINISTIC,
            evidence_scope=EvidenceScope.GOAL_INHERITABLE,
        ),
    ),
    "artifact": (
        _criterion(
            "artifact_delivery",
            "要求生成或更新的产物必须真实存在，并在最终回答中给出可定位的路径或引用。",
            source=CriterionSource.TASK,
            verifier=VerifierKind.DETERMINISTIC,
            evidence_scope=EvidenceScope.ARTIFACT_BOUND,
        ),
    ),
    "code": (
        _criterion(
            "code_validation",
            "代码修改任务必须给出并通过与改动相称的测试、构建或静态检查。",
            source=CriterionSource.SYSTEM,
            verifier=VerifierKind.DETERMINISTIC,
            evidence_scope=EvidenceScope.ARTIFACT_BOUND,
        ),
    ),
}
_PACK_ORDER = ("core", "web_research", "analytics", "artifact", "code")


class RunRubricCompiler:
    """Build immutable declared contracts and material effective contracts."""

    VERSION = "run-task-profile-v4"

    @classmethod
    def rebuild_declared_contract(
        cls,
        *,
        message: str,
        legacy_contract: RunVerificationContract,
    ) -> RunVerificationContract:
        """Migrate an older Goal contract from objective truth, not prior packs.

        Older Goal records may contain an effective Run contract that was
        monotonically widened by incidental Tool work.  Reclassification of
        the immutable Goal objective removes those historical packs while
        preserving genuine custom criteria.
        """

        managed_ids = {
            criterion.id
            for pack in _PACK_CRITERIA.values()
            for criterion in pack
        } | {"time_scope", "report_integrity"}
        custom_rules = tuple(
            criterion.model_dump(mode="json")
            for criterion in legacy_contract.criteria
            if criterion.id not in managed_ids
        )
        rebuilt = cls.compile(
            RubricBuildContext(
                user_message=message,
                custom_rules=custom_rules,
                force_required=True,
            )
        )
        if rebuilt is None:  # force_required makes this unreachable; fail closed.
            raise ValueError("Could not rebuild declared Goal contract")
        return rebuilt

    @classmethod
    def classify(cls, context: RubricBuildContext) -> RunTaskProfile:
        return context.task_profile or TaskProfileClassifier.classify(
            message=context.user_message,
            analytics_model_id=context.analytics_model_id,
        )

    @classmethod
    def should_verify(cls, context: RubricBuildContext) -> bool:
        if not context.user_message.strip():
            return False
        profile = cls.classify(context)
        return bool(
            context.force_required
            or profile.initial_packs
            or any(
                isinstance(rule, dict)
                and rule.get("enabled", True)
                and str(rule.get("statement") or "").strip()
                for rule in context.custom_rules
            )
        )

    @classmethod
    def compile(cls, context: RubricBuildContext) -> RunVerificationContract | None:
        profile = cls.classify(context)
        if not cls.should_verify(context):
            return None
        packs = list(profile.initial_packs)
        if "core" not in packs:
            packs.insert(0, "core")
        packs = cls._normalize_packs(packs)
        criteria = cls._criteria_for(
            packs=packs,
            message=context.user_message,
            custom_rules=context.custom_rules,
        )
        # 模型声明 acceptance.invariants 时并入验收 criteria。不加入 managed_ids，
        # expand_for_activations 会经 custom 规则回填保留这条 criterion。
        if context.analytics_model_id and load_model_invariants(
            context.analytics_model_id
        ):
            criteria.append(
                _criterion(
                    "analytics_model_invariants",
                    "分析模型声明的验收不变量（acceptance.invariants）必须全部满足。",
                    source=CriterionSource.SYSTEM,
                    verifier=VerifierKind.DETERMINISTIC,
                    evidence_scope=EvidenceScope.RUN_ONLY,
                )
            )
        reasons = {
            pack: [
                reason
                for reason in profile.reasons
                if cls._reason_matches_pack(reason, pack)
            ]
            or ["goal_mode" if context.force_required else "task_profile"]
            for pack in packs
        }
        return cls._build_contract(
            message=context.user_message,
            profile=profile,
            packs=packs,
            criteria=criteria,
            activation_reasons=reasons,
            browser_e2e_required=browser_e2e_requested(
                context.user_message
            ),
        )

    @classmethod
    def expand_for_activations(
        cls,
        *,
        contract: RunVerificationContract | None,
        profile: RunTaskProfile,
        message: str,
        activations: list[VerificationActivation | dict[str, Any]],
    ) -> RunVerificationContract | None:
        normalized = [
            item
            if isinstance(item, VerificationActivation)
            else VerificationActivation.model_validate(item)
            for item in activations
        ]
        # A successful call is only an activation candidate.  Widen the
        # effective contract only when the result became completion evidence,
        # a cited source, or a delivered artifact.  Objective-derived packs are
        # already present in ``profile.initial_packs`` and do not need a Tool
        # call to become mandatory.
        successful = [
            item
            for item in normalized
            if item.status == "succeeded" and cls._is_material_activation(item)
        ]
        packs = list(contract.verification_packs if contract is not None else ())
        for pack in profile.initial_packs:
            if pack not in packs:
                packs.append(pack)
        for activation in successful:
            if activation.pack not in packs:
                packs.append(activation.pack)
        if not packs:
            return contract
        if "core" not in packs:
            packs.insert(0, "core")
        packs = cls._normalize_packs(packs)

        existing_custom = []
        if contract is not None:
            registered_ids = {
                criterion.id
                for pack in _PACK_CRITERIA.values()
                for criterion in pack
            } | {"time_scope", "report_integrity"}
            existing_custom = [
                criterion.model_dump(mode="json")
                for criterion in contract.criteria
                if criterion.id not in registered_ids
            ]
        criteria = cls._criteria_for(
            packs=packs,
            message=message,
            custom_rules=tuple(existing_custom),
        )
        reasons = {
            key: list(values)
            for key, values in (contract.activation_reasons if contract else {}).items()
        }
        for activation in successful:
            reason = f"tool:{activation.tool_name}:{activation.tool_call_id}"
            reasons.setdefault(activation.pack, [])
            if reason not in reasons[activation.pack]:
                reasons[activation.pack].append(reason)
        for pack in packs:
            reasons.setdefault(pack, ["task_profile"])
        reasons = {
            pack: sorted(dict.fromkeys(reasons.get(pack, [])))
            for pack in packs
        }

        effective = cls._build_contract(
            message=message,
            profile=profile,
            packs=packs,
            criteria=criteria,
            activation_reasons=reasons,
            browser_e2e_required=bool(
                (contract.browser_e2e_required if contract else False)
                or browser_e2e_requested(message)
            ),
            base_contract_id=(
                contract.base_contract_id or contract.contract_id
                if contract is not None
                else None
            ),
        )
        if (
            contract is not None
            and effective.verification_packs == contract.verification_packs
            and effective.criteria == contract.criteria
            and effective.activation_reasons == contract.activation_reasons
            and effective.browser_e2e_required
            == contract.browser_e2e_required
        ):
            return contract
        return effective

    @classmethod
    def _criteria_for(
        cls,
        *,
        packs: list[str],
        message: str,
        custom_rules: tuple[dict, ...],
    ) -> list[VerificationCriterion]:
        criteria: list[VerificationCriterion] = []
        seen: set[str] = set()
        for pack in packs:
            for configured in _PACK_CRITERIA.get(pack, ()):
                if configured.id in seen:
                    continue
                criteria.append(configured.model_copy(deep=True))
                seen.add(configured.id)
        if _TIME_PATTERN.search(message) and (
            "analytics" in packs or "web_research" in packs
        ):
            criteria.append(
                _criterion(
                    "time_scope",
                    "必须明确并遵守用户要求的数据或信息时间范围，不能用其他期间替代。",
                    source=CriterionSource.USER,
                    verifier=VerifierKind.LLM_GRADER,
                )
            )
            seen.add("time_scope")
        if "artifact" in packs and ("报告" in message or "模板" in message):
            criteria.append(
                _criterion(
                    "report_integrity",
                    "报告的既定结构、标题、图表与正文必须保持完整，更新内容不能破坏模板。",
                    source=CriterionSource.TASK,
                    verifier=VerifierKind.LLM_GRADER,
                )
            )
            seen.add("report_integrity")
        for index, rule in enumerate(custom_rules):
            if not isinstance(rule, dict) or not rule.get("enabled", True):
                continue
            rule_id = str(rule.get("id") or f"custom_{index + 1}")
            if rule_id in seen:
                continue
            statement = str(rule.get("statement") or "").strip()
            if not statement:
                continue
            try:
                verifier = VerifierKind(str(rule.get("verifier") or "llm_grader"))
                source = CriterionSource(str(rule.get("source") or "settings"))
                evidence_scope = EvidenceScope(
                    str(rule.get("evidence_scope") or EvidenceScope.RUN_ONLY.value)
                )
            except ValueError:
                continue
            criteria.append(
                VerificationCriterion(
                    id=rule_id,
                    statement=statement,
                    source=source,
                    verifier=verifier,
                    required=bool(rule.get("required", True)),
                    evidence_scope=evidence_scope,
                )
            )
            seen.add(rule_id)
        return criteria

    @classmethod
    def _build_contract(
        cls,
        *,
        message: str,
        profile: RunTaskProfile,
        packs: list[str],
        criteria: list[VerificationCriterion],
        activation_reasons: dict[str, list[str]],
        browser_e2e_required: bool = False,
        base_contract_id: str | None = None,
    ) -> RunVerificationContract:
        semantic_criteria = [
            criterion
            for criterion in criteria
            if criterion.verifier == VerifierKind.LLM_GRADER
        ]
        rubric_lines = (
            ["逐项审查以下标准；每个 required 标准都必须明确返回，缺失视为未通过："]
            + [f"- [{criterion.id}] {criterion.statement}" for criterion in semantic_criteria]
            if semantic_criteria
            else []
        )
        canonical_criteria = "\n".join(
            "|".join(
                (
                    criterion.id,
                    criterion.statement,
                    criterion.source.value,
                    criterion.verifier.value,
                    str(criterion.required),
                    criterion.evidence_scope.value,
                )
            )
            for criterion in criteria
        )
        digest = hashlib.sha256(
            (
                cls.VERSION
                + "\n"
                + message.strip()
                + "\n"
                + "\n".join(packs)
                + "\n"
                + canonical_criteria
                + "\n"
                + f"browser_e2e_required={browser_e2e_required}"
            ).encode("utf-8")
        ).hexdigest()[:20]
        return RunVerificationContract(
            contract_id=f"run-contract-{digest}",
            version=cls.VERSION,
            task_type=cls._task_type(packs, profile.primary_intent),
            criteria=criteria,
            rubric="\n".join(rubric_lines),
            verification_packs=packs,
            activation_reasons=activation_reasons,
            browser_e2e_required=browser_e2e_required,
            base_contract_id=base_contract_id,
        )

    @staticmethod
    def _task_type(packs: list[str], primary_intent: str) -> str:
        task_packs = [pack for pack in packs if pack != "core"]
        if not task_packs:
            return primary_intent
        return "_".join(task_packs)

    @staticmethod
    def _reason_matches_pack(reason: str, pack: str) -> bool:
        intent_id = reason.removeprefix("intent:")
        definition = INTENT_REGISTRY.get(intent_id)
        return bool(definition and pack in definition.get("packs", []))

    @staticmethod
    def _normalize_packs(packs: list[str]) -> list[str]:
        unique = set(packs)
        ordered = [pack for pack in _PACK_ORDER if pack in unique]
        ordered.extend(sorted(unique - set(_PACK_ORDER)))
        return ordered

    @staticmethod
    def packs_for_criteria(criterion_ids: set[str]) -> list[str]:
        return [
            pack
            for pack in _PACK_ORDER
            if any(criterion.id in criterion_ids for criterion in _PACK_CRITERIA.get(pack, ()))
        ]

    @staticmethod
    def _is_material_activation(activation: VerificationActivation) -> bool:
        return any(
            isinstance(ref, dict)
            and ref.get("material") is True
            and ref.get("kind") != "tool_execution"
            for ref in activation.evidence_refs
        )


__all__ = ["RubricBuildContext", "RunRubricCompiler"]
