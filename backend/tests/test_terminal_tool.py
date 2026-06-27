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
