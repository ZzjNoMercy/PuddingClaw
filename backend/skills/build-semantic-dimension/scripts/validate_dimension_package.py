"""Validate a completed semantic dimension package without executing its builder."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a semantic dimension package")
    parser.add_argument("--dimension-dir", required=True, help="Path containing dimension.md")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dimension_dir = Path(args.dimension_dir).expanduser().resolve()
    document = dimension_dir / "dimension.md"
    if not document.is_file():
        raise SystemExit(f"dimension.md not found: {document}")
    if any(path.suffix == ".py" for path in dimension_dir.rglob("*.py")):
        raise SystemExit("semantic asset directory must not contain scripts; place them in build-semantic-dimension")

    text = document.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise SystemExit("dimension.md must start with YAML frontmatter")
    _prefix, frontmatter, _body = text.split("---", 2)
    metadata = yaml.safe_load(frontmatter) or {}
    if metadata.get("type") != "dimension":
        raise SystemExit("frontmatter type must be dimension")
    mode = metadata.get("resolution_mode")
    resolution = metadata.get("resolution") or {}
    if mode not in {"source_field", "derived", "entity_lookup", "calendar_lookup"}:
        raise SystemExit("resolution_mode is invalid")
    if not isinstance(resolution, dict) or resolution.get("mode") != mode:
        raise SystemExit("resolution.mode must match resolution_mode")
    print(f"OK {metadata.get('name') or dimension_dir.name}: {mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
