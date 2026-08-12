from __future__ import annotations

import json
from pathlib import Path

from runtime_identity.migration import migrate_project_trust_registry
from runtime_identity.paths import PuddingClawPaths


def test_legacy_registry_projects_preserve_trust_once(tmp_path: Path) -> None:
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    paths.ensure_layout()
    project = tmp_path / "legacy-project"
    project.mkdir()
    registry_path = paths.project_registry()
    registry_path.write_text(
        json.dumps({
            "proj_legacy": {
                "name": "Legacy",
                "path": str(project),
                "created_at": 1,
                "updated_at": 1,
            }
        }),
        encoding="utf-8",
    )

    report = migrate_project_trust_registry(paths)
    migrated = json.loads(registry_path.read_text(encoding="utf-8"))["proj_legacy"]

    assert report["upgraded"] == 1
    assert migrated["trust_state"] == "trusted"
    assert migrated["identity_digest"]
    assert migrated["trust_source"] == "legacy_registry_migration"

    # A later external import without trust fields must remain untouched: the
    # one-shot legacy upgrade cannot become an implicit trust fallback.
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    payload["proj_imported"] = {"name": "Imported", "path": str(project)}
    registry_path.write_text(json.dumps(payload), encoding="utf-8")
    repeated = migrate_project_trust_registry(paths)
    imported = json.loads(registry_path.read_text(encoding="utf-8"))["proj_imported"]

    assert repeated == report
    assert "trust_state" not in imported


def test_legacy_unavailable_project_remains_pending(tmp_path: Path) -> None:
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    paths.ensure_layout()
    registry_path = paths.project_registry()
    registry_path.write_text(
        json.dumps({
            "proj_missing": {
                "name": "Missing",
                "path": str(tmp_path / "missing-project"),
            }
        }),
        encoding="utf-8",
    )

    report = migrate_project_trust_registry(paths)
    migrated = json.loads(registry_path.read_text(encoding="utf-8"))["proj_missing"]

    assert report["unavailable"] == 1
    assert migrated["trust_state"] == "pending"
    assert migrated["identity_digest"] == ""
