# Skill Review Scorecard

Score each dimension from 1 to 5.

## 1. Trigger Quality

1: Trigger is vague or missing invocation context.
2: Capability is named, but route quality is weak.
3: Trigger works for the main path but overlaps adjacent tasks.
4: Trigger is precise and includes context.
5: Trigger is precise, bounded, and highly discoverable.

## 2. Routing Clarity

1: No clear flow.
2: Some steps exist, but next action is ambiguous.
3: Main path is usable, branches are weak.
4: Main path and branches are explicit.
5: Routing is obvious, compact, and robust.

## 3. Context Efficiency

1: Main file is bloated.
2: Some detail should be externalized.
3: Mixed discipline.
4: Most detail is correctly layered.
5: Main file is lean and the references are cleanly routed.

## 4. Reuse And Determinism

1: Repeated fragile work is entirely manual.
2: Reuse opportunities are mostly ignored.
3: Some reusable parts exist.
4: Repeated work is mostly stabilized.
5: Scripts, references, and assets are used with strong intent.

## 5. Validation Strength

1: No clear validation.
2: Validation is implied, not defined.
3: Basic checks exist.
4: Clear checks and success criteria exist.
5: Validation is concrete, testable, and hard to fake.

## Interpretation

22-25:
Production-grade. Expand only if real usage demands it.

17-21:
Strong foundation. Fix medium weaknesses before expanding.

12-16:
Usable but unstable. Tighten routing and validation first.

Below 12:
Rework the boundary and architecture before adding more detail.
