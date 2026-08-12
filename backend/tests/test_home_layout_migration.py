from __future__ import annotations

import json
from pathlib import Path

from runtime_identity.migration import migrate_home_layout
from runtime_identity.paths import PuddingClawPaths


def test_home_layout_migration_copies_early_layout_without_runtime_fallback(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / ".puddingclaw"
    paths = PuddingClawPaths(home)
    legacy_usage = paths.data() / "stats"
    legacy_results = paths.data() / "database-query-results"
    legacy_usage.mkdir(parents=True)
    legacy_results.mkdir(parents=True)
    (legacy_usage / "token_usage.db").write_bytes(b"usage")
    (legacy_results / "qr-1.jsonl").write_text('{"id":1}\n', encoding="utf-8")
    access = paths.data() / "worker-access-keys.json"
    access.write_text(json.dumps({"key": {"secret_hash": "sha256:test"}}), encoding="utf-8")
    (paths.data() / "evaluation-settings.json").write_text(
        json.dumps({"enabled": True}), encoding="utf-8"
    )
    (paths.data() / "evaluation.db").write_bytes(b"evaluation")
    (paths.data() / "puddingclaw.db").write_bytes(b"catalog")
    (paths.data() / "projects.json").write_text(
        json.dumps({"project": {"path": "/tmp/project"}}), encoding="utf-8"
    )
    legacy_memory = paths.memory() / "MEMORY.md"
    legacy_memory.parent.mkdir(parents=True)
    legacy_memory.write_text("# 长期记忆\n\n用户偏好：严谨。\n", encoding="utf-8")
    generated_memory = paths.memory() / "global" / "MEMORY.md"
    generated_memory.parent.mkdir(parents=True)
    generated_memory.write_text(
        "# Global Memory\n\n<!--\n"
        "This file is injected into the Agent's system prompt via DeepAgents MemoryMiddleware.\n"
        "-->\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "owner")

    report = migrate_home_layout(paths)

    assert report["conflicts"] == []
    assert (paths.usage() / "token_usage.db").read_bytes() == b"usage"
    assert (paths.query_results() / "qr-1.jsonl").is_file()
    assert json.loads(paths.evaluation_settings().read_text(encoding="utf-8"))["enabled"] is True
    assert (paths.databases() / "evaluation.sqlite3").read_bytes() == b"evaluation"
    assert (paths.databases() / "catalog.sqlite3").read_bytes() == b"catalog"
    assert json.loads(paths.project_registry().read_text(encoding="utf-8"))["project"]["path"] == "/tmp/project"
    assert generated_memory.read_text(encoding="utf-8") == legacy_memory.read_text(encoding="utf-8")
    owner_access = paths.owner_access("owner") / "worker-access-keys.json"
    assert owner_access.is_file()
    assert owner_access.stat().st_mode & 0o777 == 0o600
