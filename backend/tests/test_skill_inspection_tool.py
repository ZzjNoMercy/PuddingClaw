from __future__ import annotations

import hashlib
import json

from tools.skill_inspection_tool import create_skill_inspection_tool


def test_inspect_skill_returns_stable_read_only_manifest(tmp_path):
    skill_dir = tmp_path / "skills" / "demo-skill"
    script_dir = skill_dir / "scripts"
    script_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\nversion: 1.2.3\ndescription: demo\n---\n# Demo\n",
        encoding="utf-8",
    )
    script = script_dir / "run.py"
    script.write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
    cache = script_dir / "__pycache__"
    cache.mkdir()
    (cache / "run.pyc").write_bytes(b"unstable cache")
    tool = create_skill_inspection_tool(tmp_path)

    first = json.loads(tool._run("demo-skill"))
    second = json.loads(tool._run("demo-skill"))

    assert first == second
    assert first["ok"] is True
    assert first["metadata"]["version"] == "1.2.3"
    assert first["executed"] is False
    assert first["file_count"] == 2
    script_entry = next(item for item in first["files"] if item["path"] == "scripts/run.py")
    assert script_entry["sha256"] == hashlib.sha256(script.read_bytes()).hexdigest()


def test_inspect_skill_rejects_path_traversal_and_symlinks(tmp_path):
    (tmp_path / "skills").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "skills" / "linked").symlink_to(outside, target_is_directory=True)
    tool = create_skill_inspection_tool(tmp_path)

    assert json.loads(tool._run("../outside"))["error"] == "invalid_skill_name"
    assert json.loads(tool._run("linked"))["error"] == "skill_outside_managed_root"
