from pathlib import Path


def _write_prompt_templates(base_dir: Path) -> None:
    prompt_dir = base_dir / "prompts" / "deepagents"
    prompt_dir.mkdir(parents=True)
    (prompt_dir / "AGENTS.md").write_text("AGENTS PROMPT", encoding="utf-8")
    (prompt_dir / "PROJECT_CONTEXT.md").write_text("DEFAULT PROJECT CONTEXT", encoding="utf-8")
    (prompt_dir / "TOOL_GUIDES.md").write_text("TOOL GUIDES", encoding="utf-8")


def test_register_project_creates_project_context(tmp_path):
    from projects.project_context import PROJECT_CONTEXT_RELATIVE_PATH
    from projects.registry import ProjectRegistry

    _write_prompt_templates(tmp_path)
    project = tmp_path / "sample-project"
    project.mkdir()

    registry = ProjectRegistry()
    registry.initialize(tmp_path)
    record = registry.register(str(project))

    context_path = project / PROJECT_CONTEXT_RELATIVE_PATH
    assert record.path == str(project.resolve())
    assert context_path.exists()
    assert context_path.read_text(encoding="utf-8") == "DEFAULT PROJECT CONTEXT"


def test_deepagents_prompt_builder_uses_project_local_context(tmp_path):
    from graph.deepagents_prompt_builder import build_deepagents_system_prompt
    from projects.project_context import PROJECT_CONTEXT_RELATIVE_PATH

    _write_prompt_templates(tmp_path)
    project = tmp_path / "sample-project"
    project.mkdir()
    context_path = project / PROJECT_CONTEXT_RELATIVE_PATH
    context_path.parent.mkdir(parents=True)
    context_path.write_text("LOCAL PROJECT CONTEXT", encoding="utf-8")

    prompt = build_deepagents_system_prompt(tmp_path, project)

    assert "AGENTS PROMPT" in prompt
    assert "LOCAL PROJECT CONTEXT" in prompt
    assert "DEFAULT PROJECT CONTEXT" not in prompt
    assert "TOOL GUIDES" in prompt


def test_default_tool_guides_route_product_config_metrics_to_database() -> None:
    prompt = (Path(__file__).resolve().parent.parent / "prompts" / "deepagents" / "TOOL_GUIDES.md").read_text(
        encoding="utf-8"
    )

    assert "Use `database_sql_generate` first" in prompt
    assert "配置率, 搭载率, 配备率" in prompt
    assert "Do not first search the knowledge base, inspect schema" in prompt
    assert "Business metric questions such as sales volume" not in prompt
