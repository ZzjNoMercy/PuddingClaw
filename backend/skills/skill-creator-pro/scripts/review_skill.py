#!/usr/bin/env python3
"""Review a skill for structural and content-quality issues."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
RECOMMENDED_SECTIONS = ["Goal", "Workflow", "Decision Tree", "Constraints", "Validation", "Resources"]
WHEN_TO_USE_CUES = (
    "use when",
    "when asked",
    "when asked to",
    "for requests",
    "for tasks",
)
BASIC_KNOWLEDGE_PATTERNS = [
    r"\bwhat is\b",
    r"\bdefinition of\b",
    r"\bbasics of\b",
    r"\bintroduction to\b",
]
REPEATED_WORK_PATTERNS = [
    r"\brepeated\b",
    r"\brepeatable\b",
    r"\bdeterministic\b",
    r"\bbatch\b",
    r"\bautomate\b",
    r"\bworkflow\b",
]


@dataclass
class Finding:
    severity: str
    code: str
    message: str
    remediation: str


def add(findings: list[Finding], severity: str, code: str, message: str, remediation: str) -> None:
    findings.append(Finding(severity, code, message, remediation))


def load_frontmatter(skill_md: Path) -> tuple[dict | None, str]:
    text = skill_md.read_text()
    match = re.match(r"^---\n(.*?)\n---\n?", text, re.DOTALL)
    if not match:
        return None, text
    frontmatter_text = match.group(1)
    try:
        data = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        return None, text
    body = text[match.end() :]
    return data, body


def headings(body: str) -> list[str]:
    return re.findall(r"^##\s+(.+)$", body, re.MULTILINE)


def has_any_pattern(text: str, patterns: Iterable[str]) -> bool:
    lowered = text.lower()
    for pattern in patterns:
        if re.search(pattern, lowered):
            return True
    return False


def review_skill(path: Path) -> tuple[list[Finding], dict[str, str]]:
    findings: list[Finding] = []
    stats: dict[str, str] = {}

    skill_md = path / "SKILL.md"
    if not skill_md.exists():
        add(findings, "high", "missing-skill-md", "`SKILL.md` is missing.", "Create `SKILL.md` before any other skill review.")
        return findings, stats

    raw_text = skill_md.read_text()
    line_count = raw_text.count("\n") + 1
    stats["skill_md_lines"] = str(line_count)

    frontmatter, body = load_frontmatter(skill_md)
    if frontmatter is None:
        add(findings, "high", "invalid-frontmatter", "`SKILL.md` frontmatter is missing or invalid YAML.", "Add valid YAML frontmatter with `name` and `description`.")
        return findings, stats

    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9-]+", name or ""):
        add(findings, "high", "invalid-name", "Skill `name` is missing or not valid hyphen-case.", "Use lowercase letters, digits, and hyphens only.")
    if not isinstance(description, str) or not description.strip():
        add(findings, "high", "missing-description", "Skill `description` is missing.", "Write a description that states what the skill does and when to use it.")
    else:
        desc = description.strip()
        stats["description_chars"] = str(len(desc))
        if len(desc) < 120:
            add(findings, "medium", "description-thin", "Description is short and may underspecify trigger context.", "Add typical request patterns, task objects, or file types.")
        if not any(cue in desc.lower() for cue in WHEN_TO_USE_CUES):
            add(findings, "high", "description-no-trigger-cue", "Description states capability but not clear invocation context.", "Include wording like `Use when...` or `when asked to...`.")
        broad_terms = ["anything", "general", "various tasks", "all tasks", "any task"]
        if any(term in desc.lower() for term in broad_terms):
            add(findings, "medium", "description-too-broad", "Description uses broad terms that may cause accidental triggering.", "Narrow the description to repeated workflows and concrete task types.")

    section_names = headings(body)
    stats["top_level_sections"] = str(len(section_names))
    missing_sections = [section for section in RECOMMENDED_SECTIONS if section not in section_names]
    if missing_sections:
        add(findings, "medium", "missing-sections", f"Recommended sections are missing: {', '.join(missing_sections)}.", "Add the missing routing sections or justify a different structure.")

    if line_count > 260:
        add(findings, "medium", "skill-md-long", "`SKILL.md` is long enough to risk context bloat.", "Move optional details and deep examples into `references/`.")
    if line_count > 420:
        add(findings, "high", "skill-md-very-long", "`SKILL.md` is far too long for a primary routing file.", "Aggressively split detail into `references/` and keep only workflow-critical guidance.")

    if "when to use" in body.lower():
        add(findings, "low", "body-when-to-use", "Body contains a `When to use` style section or phrase.", "Keep trigger logic in frontmatter unless the body needs special caveats.")

    if has_any_pattern(body, BASIC_KNOWLEDGE_PATTERNS):
        add(findings, "medium", "teaching-basics", "Body appears to explain general basics instead of focusing on execution guidance.", "Remove textbook-style explanations and keep only non-obvious procedural knowledge.")

    if "## Decision Tree" not in body and "if " not in body.lower():
        add(findings, "medium", "weak-routing", "Routing logic is weak or absent.", "Add a decision tree or explicit branching rules for different request types.")

    references_dir = path / "references"
    scripts_dir = path / "scripts"
    assets_dir = path / "assets"
    openai_yaml = path / "agents" / "openai.yaml"

    if not openai_yaml.exists():
        add(findings, "medium", "missing-openai-yaml", "`agents/openai.yaml` is missing.", "Create it so the skill has stable UI-facing metadata.")
    else:
        yaml_text = openai_yaml.read_text()
        stats["openai_yaml_chars"] = str(len(yaml_text))
        if "$" not in yaml_text:
            add(findings, "low", "default-prompt-missing-skill-ref", "`openai.yaml` does not appear to reference the skill in a default prompt.", "Add a short default prompt that explicitly mentions the skill name.")

    if references_dir.exists():
        ref_files = sorted(p for p in references_dir.rglob("*.md") if p.is_file())
        stats["reference_files"] = str(len(ref_files))
        if line_count > 220 and not ref_files:
            add(findings, "medium", "missing-references", "`SKILL.md` is long but there are no reference files.", "Split optional or detailed material into `references/`.")
    else:
        if line_count > 220:
            add(findings, "medium", "no-references-dir", "Skill is large but has no `references/` directory.", "Create `references/` for optional or branch-specific detail.")

    body_and_desc = f"{description or ''}\n{body}"
    if has_any_pattern(body_and_desc, REPEATED_WORK_PATTERNS) and not scripts_dir.exists():
        add(findings, "low", "maybe-needs-scripts", "Skill discusses repeatable or deterministic work but has no `scripts/` directory.", "Consider whether repeated fragile actions should be scripted.")

    if assets_dir.exists() and not any(assets_dir.rglob("*")):
        add(findings, "low", "empty-assets", "`assets/` exists but is empty.", "Remove decorative directories or add real output assets.")

    duplicate_resource_mentions = len(re.findall(r"references/", body)) > 8
    if duplicate_resource_mentions:
        add(findings, "low", "resource-overloaded", "`SKILL.md` may be overloading resource listings instead of routing clearly.", "Compress resource listings and move detail into the referenced files.")

    findings.sort(key=lambda item: (SEVERITY_ORDER[item.severity], item.code))
    return findings, stats


def print_report(path: Path, findings: list[Finding], stats: dict[str, str]) -> None:
    print(f"Skill Review: {path}")
    print()
    if stats:
        print("Stats")
        for key, value in stats.items():
            print(f"- {key}: {value}")
        print()

    if not findings:
        print("Findings")
        print("- No major structural or content-quality issues detected.")
        return

    print("Findings")
    for finding in findings:
        print(f"- [{finding.severity}] {finding.code}: {finding.message}")
        print(f"  Fix: {finding.remediation}")

    high_count = sum(1 for item in findings if item.severity == "high")
    medium_count = sum(1 for item in findings if item.severity == "medium")
    low_count = sum(1 for item in findings if item.severity == "low")
    print()
    print("Summary")
    print(f"- high: {high_count}")
    print(f"- medium: {medium_count}")
    print(f"- low: {low_count}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Review a skill for structure and content quality.")
    parser.add_argument("skill_path", help="Path to the skill directory")
    args = parser.parse_args()

    path = Path(args.skill_path).resolve()
    findings, stats = review_skill(path)
    print_report(path, findings, stats)
    return 1 if any(item.severity == "high" for item in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
