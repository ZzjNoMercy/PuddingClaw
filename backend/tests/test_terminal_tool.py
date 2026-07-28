"""Tests for PuddingClaw terminal tool path handling."""

from tools.terminal_tool import SafeTerminalTool


def test_terminal_maps_deepagents_virtual_skills_path(tmp_path):
    """Skill scripts can be launched with the same `/skills/` path DeepAgents sees."""

    workspace = tmp_path / "workspace"
    skills_dir = tmp_path / "skills"
    workspace.mkdir()
    skills_dir.mkdir()
    script = skills_dir / "demo.py"
    script.write_text("print('skill script ok')\n", encoding="utf-8")

    tool = SafeTerminalTool(
        root_dir=str(workspace),
        path_aliases={"/skills": str(skills_dir)},
    )

    output = tool._run("python3 /skills/demo.py")

    assert "skill script ok" in output


def test_terminal_alias_does_not_replace_substring_path(tmp_path):
    """Aliases such as /knowledge/ must not rewrite substrings inside other paths."""

    workspace = tmp_path / "workspace"
    kb_dir = tmp_path / "knowledge"
    skills_dir = tmp_path / "skills"
    workspace.mkdir()
    kb_dir.mkdir()
    skills_dir.mkdir()
    (skills_dir / "backend").mkdir()
    (skills_dir / "backend" / "knowledge").mkdir()
    (skills_dir / "backend" / "knowledge" / "note.md").write_text("ok")

    tool = SafeTerminalTool(
        root_dir=str(workspace),
        path_aliases={
            "/workspace": str(workspace),
            "/knowledge": str(kb_dir),
            "/skills": str(skills_dir),
        },
    )

    # /knowledge/ inside /skills/backend/knowledge/ should stay untouched.
    output = tool._run("cat /skills/backend/knowledge/note.md")
    assert "ok" in output
    # The real /knowledge/ alias still works.
    (kb_dir / "real.md").write_text("real kb")
    output = tool._run("cat /knowledge/real.md")
    assert "real kb" in output
