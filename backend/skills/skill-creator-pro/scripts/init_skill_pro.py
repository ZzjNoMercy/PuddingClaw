#!/usr/bin/env python3
"""Create a review-ready skill scaffold with paradigm-specific templates."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ALLOWED_RESOURCES = {"scripts", "references", "assets"}
VALID_PARADIGMS = {"operator", "navigator", "architect", "partner", "orchestrator", "scout", "philosopher"}

# Paradigm-specific default resources
PARADIGM_DEFAULTS = {
    "operator":      ["scripts", "references"],
    "navigator":     ["references"],
    "architect":     ["references", "assets"],
    "partner":       ["references"],
    "orchestrator":  ["references"],
    "scout":         ["references"],
    "philosopher":   ["references"],
}

# Paradigm-specific workflow hints
PARADIGM_WORKFLOWS = {
    "operator": """\
1. Confirm input file/data exists and is valid.
2. Detect the input type or format.
3. Run the appropriate processing script.
4. Validate the output (exit code, file existence, format).
5. Report results and any errors.""",
    "navigator": """\
1. Classify the user's query or lookup request.
2. Route to the matching reference or knowledge branch.
3. Load only the relevant reference file.
4. Present the answer with source attribution.
5. If no match, suggest the closest alternatives.""",
    "architect": """\
1. Confirm the design scope and constraints.
2. Choose the system structure or template.
3. Generate the scaffold or specification.
4. Validate the output against design gates.
5. Report the deliverable and next steps.""",
    "partner": """\
1. Open with a clear collaboration protocol.
2. Collect the user's intent and constraints.
3. Present a draft or proposal for confirmation.
4. Iterate based on user feedback.
5. Finalize only after explicit user approval.""",
    "orchestrator": """\
1. Classify the request and select the operating mode.
2. Route to the appropriate tool, agent, or phase.
3. Execute with explicit handoff rules between stages.
4. Monitor for failures and apply recovery logic.
5. Synthesize results and report the final outcome.""",
    "scout": """\
1. Inspect the environment before any action (recon-first).
2. Collect evidence: file state, system state, data shape.
3. Analyze findings against known patterns.
4. Decide whether to act or stop.
5. If acting, validate the result against recon baseline.""",
    "philosopher": """\
1. Load the governing principles (constitutional rules).
2. Classify the request against principle boundaries.
3. Execute within principle constraints.
4. Validate that the output does not violate any rule.
5. Report compliance status and any principle tensions.""",
}

# Paradigm-specific constraint hints
PARADIGM_CONSTRAINTS = {
    "operator":      "- Every operation must have a success/failure check.\n- Do not proceed if input validation fails.\n- Script all repeated or fragile actions.",
    "navigator":     "- Do not embed domain knowledge in SKILL.md; route to references/.\n- Every branch in the decision tree must point to a concrete resource.\n- Do not guess; if no match is found, say so.",
    "architect":     "- Every phase must have a design gate (pass criteria).\n- Output must be reusable, not one-off.\n- Do not skip validation before declaring a phase complete.",
    "partner":       "- Never assume user agreement; always confirm at checkpoints.\n- Do not proceed past a stage exit without explicit approval.\n- Keep the opening protocol consistent across sessions.",
    "orchestrator":  "- Define explicit handoff rules between each tool/agent.\n- Provide fallback or recovery for every handoff failure.\n- Do not carry all agent details inline in SKILL.md.",
    "scout":         "- Never act before inspecting the environment.\n- Do not guess file state, API state, or data shape.\n- Define clear stop conditions for investigation.",
    "philosopher":   "- Separate constitutional rules from execution workflow.\n- Principles are non-negotiable; execution details are adjustable.\n- Every rule must be concrete enough to check, not just aspirational.",
}

SKILL_TEMPLATE = """---
name: {skill_name}
description: [State what this skill does and when to use it. Include "Use when..." and representative request patterns.]
---

# {title}

## Goal
[State the repeated problem this skill solves and the outcome it should stabilize.]

## Workflow
{workflow}

## Decision Tree
{decision_tree}

## Constraints
{constraints}

## Validation
- Required checks:
- Success criteria:
- First failure checks:

## Resources
{resources}
"""

OPENAI_YAML_TEMPLATE = """interface:
  display_name: \"{display_name}\"
  short_description: \"{short_description}\"
  default_prompt: \"Use ${skill_name} to handle this repeated workflow as a reusable skill.\"
"""

REFERENCE_TEMPLATE = """# Design Notes

## Trigger Examples
- [Add realistic requests that should trigger this skill]

## Non-Trigger Neighbors
- [Add adjacent requests that should not trigger this skill]

## Branch Details
- [Move optional or branch-specific detail here]
"""

SCRIPT_TEMPLATE = """#!/usr/bin/env python3
\"\"\"Starter script for {skill_name}. Replace or remove it.\"\"\"


def main() -> None:
    print(\"Replace this script with real automation.\")


if __name__ == \"__main__\":
    main()
"""

ASSET_TEMPLATE = """# Output Template

Replace this file with a real asset, template, sample, or starter material.
"""


def normalize(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return re.sub(r"-{2,}", "-", normalized)


def title_case(skill_name: str) -> str:
    return " ".join(part.capitalize() for part in skill_name.split("-"))


def ensure_short_description(display_name: str) -> str:
    base = f"Create and review {display_name} skills"
    if len(base) <= 64:
        return base
    compact = f"Design and review {display_name}"
    if len(compact) <= 64:
        return compact
    return compact[:64].rstrip()


def parse_resources(raw: str) -> list[str]:
    if not raw:
        return []
    items = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if item not in ALLOWED_RESOURCES:
            raise ValueError(f"Unknown resource '{item}'. Allowed: {', '.join(sorted(ALLOWED_RESOURCES))}")
        if item not in items:
            items.append(item)
    return items


def write_file(path: Path, content: str, executable: bool = False) -> None:
    path.write_text(content)
    if executable:
        path.chmod(0o755)


def build_decision_tree(paradigm: str) -> str:
    """Generate paradigm-appropriate decision tree placeholder."""
    hints = {
        "operator":      '- If input is [format A], run `scripts/process_a.py`\n- If input is [format B], run `scripts/process_b.py`\n- If tool is missing, read `references/installation.md`',
        "navigator":     '- If query matches [topic A], read `references/topic-a.md`\n- If query matches [topic B], read `references/topic-b.md`\n- If no match, suggest closest alternatives',
        "architect":     '- If scope is [small], use `assets/template-small/`\n- If scope is [large], follow phase structure in `references/phases.md`\n- If design gate fails, iterate before proceeding',
        "partner":       '- If user intent is unclear, ask clarifying questions\n- If user confirms draft, proceed to next stage\n- If user rejects, iterate on current stage',
        "orchestrator":  '- If mode is [A], delegate to [tool/agent A]\n- If mode is [B], delegate to [tool/agent B]\n- If handoff fails, apply recovery from `references/recovery.md`',
        "scout":         '- If environment is unknown, run recon commands first\n- If recon reveals [pattern A], proceed with [action A]\n- If recon reveals risk, stop and report',
        "philosopher":   '- If request touches [principle 1], enforce rule before executing\n- If conflict between principles, escalate to user\n- If new situation, check `references/principles.md` first',
    }
    return hints.get(paradigm, '- If [condition A], run `scripts/...`\n- If [condition B], read `references/...`')


def build_resources_section(resources: list[str]) -> str:
    """Generate Resources section based on included directories."""
    lines = []
    if "scripts" in resources:
        lines.append("- `scripts/...`: when to run")
    if "references" in resources:
        lines.append("- `references/...`: when to read")
    if "assets" in resources:
        lines.append("- `assets/...`: when to use")
    return "\n".join(lines) if lines else "- (no resources yet)"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a review-ready skill scaffold with paradigm-specific templates.",
        epilog="Example: %(prog)s my-tool --path ~/.claude/skills --paradigm operator",
    )
    parser.add_argument("skill_name", help="New skill name (will be normalized to hyphen-case)")
    parser.add_argument("--path", required=True, help="Parent directory for the new skill")
    parser.add_argument("--paradigm", default="", help=f"Primary paradigm: {', '.join(sorted(VALID_PARADIGMS))}")
    parser.add_argument("--resources", default="", help="Override resource dirs (comma-separated). If omitted, uses paradigm defaults.")
    args = parser.parse_args()

    skill_name = normalize(args.skill_name)
    if not skill_name:
        print("[ERROR] Skill name is empty after normalization.")
        return 1
    if len(skill_name) > 64:
        print("[ERROR] Skill name exceeds 64 characters.")
        return 1

    paradigm = args.paradigm.lower().strip() if args.paradigm else ""
    if paradigm and paradigm not in VALID_PARADIGMS:
        print(f"[ERROR] Unknown paradigm '{paradigm}'. Valid: {', '.join(sorted(VALID_PARADIGMS))}")
        return 1

    # Determine resources: explicit override > paradigm defaults > fallback
    if args.resources:
        try:
            resources = parse_resources(args.resources)
        except ValueError as exc:
            print(f"[ERROR] {exc}")
            return 1
    elif paradigm:
        resources = PARADIGM_DEFAULTS[paradigm]
    else:
        resources = ["references"]

    parent = Path(args.path).resolve()
    skill_dir = parent / skill_name
    if skill_dir.exists():
        print(f"[ERROR] Target already exists: {skill_dir}")
        return 1

    skill_dir.mkdir(parents=True)
    (skill_dir / "agents").mkdir()

    display_name = title_case(skill_name)
    short_description = ensure_short_description(display_name)

    # Build paradigm-aware SKILL.md
    workflow = PARADIGM_WORKFLOWS.get(paradigm, PARADIGM_WORKFLOWS["operator"])
    constraints = PARADIGM_CONSTRAINTS.get(paradigm, "- List non-negotiable rules.\n- List preservation requirements.")
    decision_tree = build_decision_tree(paradigm) if paradigm else build_decision_tree("operator")
    resources_section = build_resources_section(resources)

    write_file(skill_dir / "SKILL.md", SKILL_TEMPLATE.format(
        skill_name=skill_name,
        title=display_name,
        workflow=workflow,
        decision_tree=decision_tree,
        constraints=constraints,
        resources=resources_section,
    ))
    write_file(
        skill_dir / "agents" / "openai.yaml",
        OPENAI_YAML_TEMPLATE.format(
            display_name=display_name,
            short_description=short_description,
            skill_name=skill_name,
        ),
    )

    if "references" in resources:
        (skill_dir / "references").mkdir()
        write_file(skill_dir / "references" / "design-notes.md", REFERENCE_TEMPLATE)
    if "scripts" in resources:
        (skill_dir / "scripts").mkdir()
        write_file(skill_dir / "scripts" / "example.py", SCRIPT_TEMPLATE.format(skill_name=skill_name), executable=True)
    if "assets" in resources:
        (skill_dir / "assets").mkdir()
        write_file(skill_dir / "assets" / "template.md", ASSET_TEMPLATE)

    paradigm_label = f" (paradigm: {paradigm})" if paradigm else ""
    print(f"[OK] Created {skill_dir}{paradigm_label}")
    print(f"[DIRS] {', '.join(resources) if resources else 'none'}")
    print("[NEXT] Replace template wording before using the skill.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
