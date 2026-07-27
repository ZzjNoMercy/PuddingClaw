"""CLI wrapper bundled beside the shared deterministic guardrail runtime."""

from __future__ import annotations

PORTABLE_SQL_VALIDATOR = r'''#!/usr/bin/env python3
"""Validate SQL using the exact deterministic runtime used by PuddingClaw."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from guardrail_runtime import validate_rules


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sql_file", type=Path)
    parser.add_argument("--rules", type=Path, default=Path(__file__).parents[1] / "compiled" / "rules.json")
    parser.add_argument("--context", type=Path, help="JSON with available_tables, semantic_asset_ids and question")
    parser.add_argument("--non-strict", action="store_true", help="Do not fail on rules that cannot be evaluated")
    args = parser.parse_args()
    context = json.loads(args.context.read_text(encoding="utf-8")) if args.context else None
    result = validate_rules(
        args.sql_file.read_text(encoding="utf-8"),
        json.loads(args.rules.read_text(encoding="utf-8")),
        context,
        strict=not args.non_strict,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
'''
