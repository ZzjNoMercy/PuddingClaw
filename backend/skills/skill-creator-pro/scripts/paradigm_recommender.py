#!/usr/bin/env python3
"""
Paradigm Recommender - Skill paradigm recommendation from natural language or SKILL.md analysis.

Usage:
    python paradigm_recommender.py --description "build a batch processing pipeline"
    python paradigm_recommender.py --skill-path /path/to/skill/
    python paradigm_recommender.py --description "..." --json
    python paradigm_recommender.py --help
"""

import argparse
import json
import os
import sys
import re
from pathlib import Path

# ── Paradigm keyword mappings ──────────────────────────────────────────────

PARADIGM_KEYWORDS = {
    "Operator": [
        "process", "transform", "batch", "pipeline", "execute", "automate",
        "format", "extract", "generate", "script", "tool", "run",
        "处理", "转换", "批量", "流水线", "执行", "自动化", "脚本", "生成",
    ],
    "Navigator": [
        "find", "search", "route", "guide", "navigate", "which", "select",
        "choose", "discover", "lookup",
        "查找", "搜索", "路由", "引导", "选择", "发现", "哪个", "导航",
    ],
    "Architect": [
        "design", "system", "framework", "template", "standard",
        "specification", "structure", "blueprint", "scaffold", "reusable",
        "设计", "系统", "框架", "模板", "标准", "规范", "结构", "可复用",
    ],
    "Partner": [
        "collaborate", "review", "draft", "co-design", "iterate", "confirm",
        "approve", "feedback", "discuss", "brainstorm", "refine", "co-author",
        "interactive", "conversation", "dialogue", "clarify", "consensus",
        "协作", "审查", "起草", "确认", "迭代", "反馈", "讨论", "头脑风暴",
        "打磨", "共创", "交互", "对话", "澄清", "共识",
    ],
    "Orchestrator": [
        "coordinate", "orchestrate", "multi-agent", "handoff", "pipeline",
        "mode", "switch", "parallel", "delegate",
        "协调", "编排", "多agent", "交接", "模式切换", "并行", "委派",
    ],
    "Scout": [
        "inspect", "investigate", "analyze", "debug", "diagnose", "verify",
        "check", "recon", "unknown", "uncertain",
        "检查", "调查", "分析", "调试", "诊断", "验证", "侦察", "不确定",
    ],
    "Philosopher": [
        "principle", "constitution", "rule", "governance", "meta", "doctrine",
        "policy", "guideline",
        "原则", "宪法", "规则", "治理", "准则", "策略",
    ],
}

# ── Paradigm brief descriptions ───────────────────────────────────────────

PARADIGM_DESCRIPTIONS = {
    "Operator":      "Execute deterministic tasks with clear inputs/outputs",
    "Navigator":     "Guide decisions by searching and routing among options",
    "Architect":     "Define reusable structures, templates, and standards",
    "Partner":       "Collaborate iteratively with human review and feedback",
    "Orchestrator":  "Coordinate multi-step or multi-agent workflows",
    "Scout":         "Investigate unknowns, diagnose issues, gather intel",
    "Philosopher":   "Establish principles, rules, and governance policies",
}


# ── Core scoring algorithm ─────────────────────────────────────────────────

def score_paradigms(text: str) -> dict[str, dict]:
    """
    Score each paradigm against the input text.

    Algorithm:
      1. Lowercase the input text.
      2. For each paradigm, count how many of its keywords appear in the text.
      3. Each matched keyword = +1 point.
      4. The first paradigm to score gets +0.5 bonus (tie-breaker by iteration order,
         but we apply +0.5 to the paradigm with earliest first-match position).
      5. Confidence = top1_score / total_score, normalised to [0, 1].

    Returns dict keyed by paradigm name with score, matched_keywords, confidence.
    """
    text_lower = text.lower()

    raw_scores: dict[str, float] = {}
    matched_kw: dict[str, list[str]] = {}

    # Track which paradigm has the earliest keyword match position (for tie-break bonus)
    earliest_pos: dict[str, int] = {}

    for paradigm, keywords in PARADIGM_KEYWORDS.items():
        matches = []
        first_pos = len(text_lower) + 1
        for kw in keywords:
            if kw in text_lower:
                matches.append(kw)
                pos = text_lower.index(kw)
                if pos < first_pos:
                    first_pos = pos
        raw_scores[paradigm] = float(len(matches))
        matched_kw[paradigm] = matches
        if matches:
            earliest_pos[paradigm] = first_pos

    # Apply +0.5 bonus to the paradigm whose first keyword appears earliest
    if earliest_pos:
        first_paradigm = min(earliest_pos, key=earliest_pos.get)
        raw_scores[first_paradigm] += 0.5

    total_score = sum(raw_scores.values())

    results = {}
    for paradigm in PARADIGM_KEYWORDS:
        score = raw_scores[paradigm]
        confidence = score / total_score if total_score > 0 else 0.0
        results[paradigm] = {
            "score": score,
            "confidence": round(confidence, 3),
            "matched_keywords": matched_kw[paradigm],
        }

    return results


# ── Skill-path analysis helpers ────────────────────────────────────────────

def read_skill_md(skill_path: str) -> str:
    """Read SKILL.md from the given directory."""
    md_path = os.path.join(skill_path, "SKILL.md")
    if not os.path.isfile(md_path):
        print(f"Error: SKILL.md not found at {md_path}", file=sys.stderr)
        sys.exit(1)
    with open(md_path, "r", encoding="utf-8") as f:
        return f.read()


def directory_bonus(skill_path: str) -> dict[str, float]:
    """
    Inspect directory structure and return bonus weights per paradigm.

    Heuristics:
      - Has scripts/         -> Operator +1.0
      - Has many references/ -> Navigator +1.0
      - Has templates/       -> Architect +0.5
      - Has tests/           -> Scout +0.5
    """
    bonus: dict[str, float] = {p: 0.0 for p in PARADIGM_KEYWORDS}
    base = Path(skill_path)

    if (base / "scripts").is_dir():
        bonus["Operator"] += 1.0

    refs_dir = base / "references"
    if refs_dir.is_dir():
        ref_count = sum(1 for _ in refs_dir.iterdir())
        if ref_count >= 3:
            bonus["Navigator"] += 1.0
        elif ref_count >= 1:
            bonus["Navigator"] += 0.5

    if (base / "templates").is_dir():
        bonus["Architect"] += 0.5

    if (base / "tests").is_dir():
        bonus["Scout"] += 0.5

    return bonus


# ── Result formatting ──────────────────────────────────────────────────────

def build_result(scores: dict[str, dict]) -> dict:
    """Build the final recommendation result from scored paradigms."""
    sorted_paradigms = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)

    primary_name, primary_data = sorted_paradigms[0]
    secondary = []
    for name, data in sorted_paradigms[1:]:
        if data["score"] > 0:
            secondary.append({
                "paradigm": name,
                "score": data["score"],
                "confidence": data["confidence"],
                "matched_keywords": data["matched_keywords"],
            })

    # Build reasoning
    if primary_data["score"] == 0:
        reasoning = "No paradigm keywords matched. Consider providing a more detailed description."
    else:
        kw_list = ", ".join(primary_data["matched_keywords"][:5])
        reasoning = (
            f"Matched {len(primary_data['matched_keywords'])} keyword(s) for {primary_name}: "
            f"[{kw_list}]. "
            f"Description: {PARADIGM_DESCRIPTIONS[primary_name]}."
        )

    return {
        "primary_paradigm": primary_name,
        "confidence": primary_data["confidence"],
        "score": primary_data["score"],
        "matched_keywords": primary_data["matched_keywords"],
        "secondary_paradigms": secondary,
        "reasoning": reasoning,
    }


def print_human(result: dict) -> None:
    """Print result in human-readable format."""
    print("=" * 60)
    print("  Paradigm Recommender Result")
    print("=" * 60)
    print()
    print(f"  Primary Paradigm : {result['primary_paradigm']}")
    print(f"  Confidence       : {result['confidence']:.1%}")
    print(f"  Score            : {result['score']}")
    if result["matched_keywords"]:
        print(f"  Matched Keywords : {', '.join(result['matched_keywords'])}")
    print()
    print(f"  Reasoning: {result['reasoning']}")
    print()

    if result["secondary_paradigms"]:
        print("  Secondary Paradigms:")
        print("  " + "-" * 50)
        for sp in result["secondary_paradigms"]:
            kws = ", ".join(sp["matched_keywords"][:3])
            print(f"    {sp['paradigm']:14s}  score={sp['score']:<5.1f}  "
                  f"conf={sp['confidence']:.1%}  [{kws}]")
        print()
    else:
        print("  No secondary paradigms matched.")
        print()

    print("=" * 60)


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Recommend the best-matching skill paradigm from a natural language "
                    "description or an existing SKILL.md file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --description "build a batch processing pipeline for CSV files"
  %(prog)s --description "帮我查找最合适的部署方案" --json
  %(prog)s --skill-path ./my-skill/
  %(prog)s --skill-path ./my-skill/ --json

Supported paradigms:
  Operator      Execute deterministic tasks (process, transform, batch...)
  Navigator     Guide decisions by searching/routing (find, search, which...)
  Architect     Define reusable structures/templates (design, framework...)
  Partner       Collaborate with human feedback (review, draft, iterate...)
  Orchestrator  Coordinate multi-step workflows (coordinate, parallel...)
  Scout         Investigate unknowns/diagnose (inspect, debug, verify...)
  Philosopher   Establish principles/governance (principle, rule, policy...)
        """,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--description", "-d",
        type=str,
        help="Natural language description of the desired skill behavior",
    )
    group.add_argument(
        "--skill-path", "-s",
        type=str,
        help="Path to an existing skill directory containing SKILL.md",
    )
    parser.add_argument(
        "--json", "-j",
        action="store_true",
        dest="json_output",
        help="Output result in JSON format",
    )

    args = parser.parse_args()

    # ── Determine input text and optional directory bonus ──
    dir_bonus: dict[str, float] | None = None

    if args.description:
        input_text = args.description
    else:
        skill_path = os.path.expanduser(args.skill_path)
        if not os.path.isdir(skill_path):
            print(f"Error: directory not found: {skill_path}", file=sys.stderr)
            sys.exit(1)
        input_text = read_skill_md(skill_path)
        dir_bonus = directory_bonus(skill_path)

    # ── Score ──
    scores = score_paradigms(input_text)

    # Apply directory structure bonus if in skill-path mode
    if dir_bonus:
        for paradigm, bonus in dir_bonus.items():
            if bonus > 0:
                scores[paradigm]["score"] += bonus
        # Recalculate confidence after bonus
        total = sum(s["score"] for s in scores.values())
        if total > 0:
            for s in scores.values():
                s["confidence"] = round(s["score"] / total, 3)

    # ── Build and output result ──
    result = build_result(scores)

    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_human(result)


if __name__ == "__main__":
    main()
