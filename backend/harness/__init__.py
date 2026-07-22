"""Product-level Harness control plane for PuddingClaw Agent runs.

The package deliberately exposes its compatibility API lazily. Core storage
modules import lightweight Harness submodules during application cold start;
eagerly importing coordinators here would import ``graph.session_manager``
again while it is still being initialized.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "CompletionVerificationCoordinator": ("harness.coordinators", "CompletionVerificationCoordinator"),
    "evaluate_deterministic_criteria": ("harness.deterministic_checks", "evaluate_deterministic_criteria"),
    "EvidenceScope": ("harness.models", "EvidenceScope"),
    "GoalCoordinator": ("harness.coordinators", "GoalCoordinator"),
    "GoalRecord": ("harness.models", "GoalRecord"),
    "GoalStatus": ("harness.models", "GoalStatus"),
    "HarnessRunCoordinator": ("harness.coordinators", "HarnessRunCoordinator"),
    "RubricEvaluationReport": ("harness.models", "RubricEvaluationReport"),
    "RunOutcome": ("harness.models", "RunOutcome"),
    "RunRecord": ("harness.models", "RunRecord"),
    "RunStatus": ("harness.models", "RunStatus"),
    "RunVerificationContract": ("harness.models", "RunVerificationContract"),
    "VerificationStatus": ("harness.models", "VerificationStatus"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
