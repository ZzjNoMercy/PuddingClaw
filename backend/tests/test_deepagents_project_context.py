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

    assert "Current Capability Manifest lists `database_sql_generate`" in prompt
    assert "`/skills/database-analysis/SKILL.md`" in prompt
    assert "配置率, 搭载率, 配备率" in prompt
    assert "business sub-question" in prompt
    assert "Never add a physical choice the user did not state" in prompt
    assert "describe only the observed error" in prompt
    assert "Never start a fresh generation with an Agent-invented physical workaround" in prompt
    assert "injected automatically from trusted runtime state" in prompt
    assert "validation_receipt_id" in prompt
    assert "Execute rejects a missing or hash-mismatched receipt" in prompt
    assert "never launch multiple revision requests in parallel" in prompt
    assert "Do not first search the knowledge base, inspect schema" in prompt
    assert "Business metric questions such as sales volume" not in prompt


def test_default_tool_guides_require_execution_self_check_before_completion() -> None:
    prompt = (Path(__file__).resolve().parent.parent / "prompts" / "deepagents" / "TOOL_GUIDES.md").read_text(
        encoding="utf-8"
    )

    assert "finish or" in prompt
    assert "explicitly cancel every Todo" in prompt
    assert "read back each declared deliverable" in prompt
    assert "lidar/HUD" in prompt
    assert "continue the Model/Tools loop and repair it" in prompt
    assert "treat it as part of" in prompt
    assert "the same Run" in prompt


def test_default_tool_guides_define_managed_browser_authorization_lifecycle() -> None:
    prompt = (Path(__file__).resolve().parent.parent / "prompts" / "deepagents" / "TOOL_GUIDES.md").read_text(
        encoding="utf-8"
    )

    assert "status: awaiting_user_browser" in prompt
    assert "Natural-language replies" in prompt
    assert "`lark-cli auth resume`" in prompt
    assert "Never call `lark-cli auth login --device-code ...`" in prompt
    assert "Only `authorization_completed: true`" in prompt


def test_default_agent_prompt_defines_async_lifecycle_invariant() -> None:
    prompt = (Path(__file__).resolve().parent.parent / "prompts" / "deepagents" / "AGENTS.md").read_text(
        encoding="utf-8"
    )

    assert "异步与交互式任务生命周期" in prompt
    assert "严格区分“命令或后台任务成功启动”和“用户目标已经完成”" in prompt
    assert "`awaiting_*`、`pending`、`action_required`" in prompt
    assert "结束当前轮，把控制权交还用户" in prompt
    assert "先用对应的 status/show/verify 能力验证" in prompt
    assert "结构化 `authorization_request`" in prompt
    assert "用户用自然语言表示完成即可续跑" in prompt


def test_default_tool_guides_use_virtual_filesystem_for_semantic_assets() -> None:
    prompt = (Path(__file__).resolve().parent.parent / "prompts" / "deepagents" / "TOOL_GUIDES.md").read_text(
        encoding="utf-8"
    )

    assert 'read_file("/semantic-assets/...", limit=1000)' in prompt
    assert "never pass it to `read_resource`" in prompt
    assert "Do not use `read_resource` for `/skills/`, `/semantic-assets/`" in prompt
    assert "convert it to the equivalent `/workspace/<relative-path>`" in prompt
    assert "transparently routed through the HostFileBroker" in prompt
    assert "do not call deprecated Stage/lease tools for a new Run" in prompt
    assert "`patch_file` with unique replacement anchors" in prompt
    assert "The default runner is the kernel sandbox" in prompt
    assert "stage_external_artifact" not in prompt
    assert "stage_external_directory" not in prompt


def test_default_agent_prompt_requires_chinese_user_visible_output() -> None:
    prompt = (Path(__file__).resolve().parent.parent / "prompts" / "deepagents" / "AGENTS.md").read_text(
        encoding="utf-8"
    )

    assert "所有对用户可见的输出必须使用中文" in prompt
    assert "工具调用前后的过渡语" in prompt
    assert "内部隐藏推理可以使用英文" in prompt
