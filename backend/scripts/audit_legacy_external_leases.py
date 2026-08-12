"""Audit/migrate Agent-facing external lease compatibility state.

Run once per release candidate. Source schemas remain present until the report
shows no active leases and two distinct release observations with zero new
compatibility calls.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from graph.session_manager import SessionManager  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", type=Path, default=None, help="Optional explicit test/runtime root")
    parser.add_argument(
        "--release-id",
        required=True,
        help="Stable release/candidate identifier; repeated ids are idempotent",
    )
    parser.add_argument(
        "--no-migrate",
        action="store_true",
        help="Audit only; do not abandon expired or known-terminal leases",
    )
    args = parser.parse_args()

    manager = SessionManager()
    if args.base_dir is None:
        from runtime_identity.paths import PuddingClawPaths

        base_dir = PuddingClawPaths.from_environment().root
    else:
        base_dir = args.base_dir.expanduser().resolve()
    manager.initialize(base_dir)
    reports: list[dict[str, object]] = []
    for path in sorted((base_dir / "sessions").glob("*.json")):
        if path.parent.name == "traces":
            continue
        session_id = path.stem
        try:
            audit = manager.audit_legacy_external_leases(
                session_id,
                migrate=not args.no_migrate,
                release_id=args.release_id,
            )
        except (FileNotFoundError, ValueError) as exc:
            reports.append({"session_id": session_id, "error": str(exc)})
            continue
        reports.append({"session_id": session_id, **audit})

    payload = {
        "release_id": args.release_id,
        "session_count": len(reports),
        "retirement_eligible": bool(reports)
        and all(bool(item.get("retirement_eligible")) for item in reports),
        "sessions": reports,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
