import json
from pathlib import Path


def _write_prompt_templates(base_dir: Path) -> None:
    prompt_dir = base_dir / "prompts"
    prompt_dir.mkdir(parents=True)
    (prompt_dir / "SOUL.md").write_text("SOUL PROMPT", encoding="utf-8")
    (prompt_dir / "IDENTITY.md").write_text("IDENTITY PROMPT", encoding="utf-8")
    (prompt_dir / "AGENTS.md").write_text("AGENTS PROMPT", encoding="utf-8")
    tool_guide_dir = prompt_dir / "tool_guides"
    tool_guide_dir.mkdir()
    (tool_guide_dir / "core.md").write_text("CORE TOOL GUIDES", encoding="utf-8")


def test_register_project_does_not_create_project_agents(tmp_path):
    from projects.project_agents import PROJECT_AGENTS_RELATIVE_PATH
    from projects.registry import ProjectRegistry

    _write_prompt_templates(tmp_path)
    project = tmp_path / "sample-project"
    project.mkdir()

    registry = ProjectRegistry()
    registry.initialize(tmp_path)
    record = registry.register(str(project))

    context_path = project / PROJECT_AGENTS_RELATIVE_PATH
    assert record.path == str(project.resolve())
    assert not context_path.exists()


def test_local_project_selection_registers_current_identity_as_trusted(tmp_path):
    from projects.registry import ProjectRegistry

    project = tmp_path / "selected-project"
    project.mkdir()
    registry = ProjectRegistry()
    registry.initialize(tmp_path / "home")

    record = registry.register(str(project), trusted=True)

    assert record.trust_state == "trusted"
    assert record.identity_digest
    assert registry.is_trusted(record.project_id) is True


def test_explicit_trust_upgrades_migrated_project_without_identity_digest(tmp_path):
    from projects.registry import ProjectRegistry

    project = tmp_path / "migrated-project"
    project.mkdir()
    home = tmp_path / "home"
    registry = ProjectRegistry()
    registry.initialize(home)
    record = registry.register(str(project))

    registry_path = home / "projects" / "registry.json"
    persisted = json.loads(registry_path.read_text(encoding="utf-8"))
    persisted[record.project_id].pop("identity_digest", None)
    registry_path.write_text(json.dumps(persisted), encoding="utf-8")

    trusted = registry.set_trust(record.project_id, "trusted")

    assert trusted.trust_state == "trusted"
    assert trusted.identity_digest
    assert registry.is_trusted(record.project_id) is True


def test_trust_is_invalidated_when_project_identity_changes(tmp_path):
    from projects.registry import ProjectRegistry

    project = tmp_path / "replaceable-project"
    project.mkdir()
    registry = ProjectRegistry()
    registry.initialize(tmp_path / "home")
    record = registry.register(str(project))
    registry.set_trust(record.project_id, "trusted")

    original = project.rename(tmp_path / "original-project")
    project.mkdir()

    assert original.is_dir()
    assert registry.is_trusted(record.project_id) is False


def test_project_agents_read_is_empty_until_user_creates_file(tmp_path):
    from projects.project_agents import read_project_agents

    project = tmp_path / "sample-project"
    project.mkdir()

    content, path, exists = read_project_agents(project)
    assert content == ""
    assert path == project / "AGENTS.md"
    assert exists is False

    user_created = project / "AGENTS.md"
    user_created.write_text("# Project Rules\n", encoding="utf-8")
    assert read_project_agents(project) == ("# Project Rules\n", user_created, True)


def test_project_agents_rejects_symlink_escape(tmp_path):
    import pytest

    from projects.project_agents import read_project_agents

    project = tmp_path / "sample-project"
    project.mkdir()
    external = tmp_path / "external-agents.md"
    external.write_text("EXTERNAL", encoding="utf-8")
    (project / "AGENTS.md").symlink_to(external)

    with pytest.raises(PermissionError, match="outside the trusted project"):
        read_project_agents(project)
    assert external.read_text(encoding="utf-8") == "EXTERNAL"


def test_project_agents_api_is_read_only():
    from api.projects import router

    agents_routes = [
        route
        for route in router.routes
        if route.path == "/projects/{project_id}/agents"
    ]

    assert any("GET" in (route.methods or set()) for route in agents_routes)
    assert not any("PUT" in (route.methods or set()) for route in agents_routes)


def test_deepagents_prompt_builder_mounts_trusted_project_agents(tmp_path):
    from graph.deepagents_prompt_builder import build_deepagents_system_prompt
    from projects.project_agents import PROJECT_AGENTS_RELATIVE_PATH
    from projects.registry import project_registry

    _write_prompt_templates(tmp_path)
    project = tmp_path / "sample-project"
    project.mkdir()
    context_path = project / PROJECT_AGENTS_RELATIVE_PATH
    context_path.write_text("LOCAL PROJECT AGENTS", encoding="utf-8")
    project_registry.initialize(tmp_path / "home")
    record = project_registry.register(str(project))
    project_registry.set_trust(record.project_id, "trusted")

    prompt = build_deepagents_system_prompt(tmp_path, project, project_id=record.project_id)

    assert prompt.index("SOUL PROMPT") < prompt.index("IDENTITY PROMPT")
    assert prompt.index("IDENTITY PROMPT") < prompt.index("AGENTS PROMPT")
    assert "## Project AGENTS" in prompt
    assert "LOCAL PROJECT AGENTS" in prompt
    assert "CORE TOOL GUIDES" in prompt


def test_deepagents_prompt_builder_ignores_untrusted_project_agents(tmp_path):
    from graph.deepagents_prompt_builder import build_deepagents_system_prompt
    from projects.registry import project_registry

    _write_prompt_templates(tmp_path)
    project = tmp_path / "sample-project"
    project.mkdir()
    (project / "AGENTS.md").write_text("UNTRUSTED PROJECT AGENTS", encoding="utf-8")
    project_registry.initialize(tmp_path / "home")
    record = project_registry.register(str(project))

    prompt = build_deepagents_system_prompt(tmp_path, project, project_id=record.project_id)

    assert "UNTRUSTED PROJECT AGENTS" not in prompt
    assert "## Project AGENTS" not in prompt


def test_deepagents_prompt_builder_rejects_trusted_id_for_another_workspace(tmp_path):
    from graph.deepagents_prompt_builder import build_deepagents_system_prompt
    from projects.registry import project_registry

    _write_prompt_templates(tmp_path)
    trusted_project = tmp_path / "trusted-project"
    other_project = tmp_path / "other-project"
    trusted_project.mkdir()
    other_project.mkdir()
    (other_project / "AGENTS.md").write_text("OTHER PROJECT AGENTS", encoding="utf-8")
    project_registry.initialize(tmp_path / "home")
    record = project_registry.register(str(trusted_project))
    project_registry.set_trust(record.project_id, "trusted")

    prompt = build_deepagents_system_prompt(
        tmp_path,
        other_project,
        project_id=record.project_id,
    )

    assert "OTHER PROJECT AGENTS" not in prompt
    assert "## Project AGENTS" not in prompt


def test_deepagents_base_prompt_does_not_merge_user_profile(tmp_path):
    from graph.deepagents_prompt_builder import build_deepagents_system_prompt
    from projects.project_agents import PROJECT_AGENTS_RELATIVE_PATH
    from projects.registry import project_registry

    _write_prompt_templates(tmp_path)
    profile = tmp_path / "home" / "profile"
    profile.mkdir(parents=True)
    (profile / "AGENTS.md").write_text("USER AGENT ADDITION", encoding="utf-8")
    (profile / "SOUL.md").write_text("IGNORED USER SOUL", encoding="utf-8")
    (profile / "USER.md").write_text("IGNORED USER PROFILE", encoding="utf-8")
    project = tmp_path / "sample-project"
    project.mkdir()
    context_path = project / PROJECT_AGENTS_RELATIVE_PATH
    context_path.write_text("LOCAL PROJECT AGENTS", encoding="utf-8")
    project_registry.initialize(tmp_path / "registry-home")
    record = project_registry.register(str(project))
    project_registry.set_trust(record.project_id, "trusted")

    prompt = build_deepagents_system_prompt(tmp_path, project, project_id=record.project_id)

    assert prompt.index("AGENTS PROMPT") < prompt.index("LOCAL PROJECT AGENTS")
    assert "## User AGENTS Additions" not in prompt
    assert "USER AGENT ADDITION" not in prompt
    assert "IGNORED USER SOUL" not in prompt
    assert "IGNORED USER PROFILE" not in prompt


def test_default_tool_guides_route_product_config_metrics_to_database() -> None:
    prompt = (
        Path(__file__).resolve().parent.parent
        / "prompts"
        / "tool_guides"
        / "database-analysis.md"
    ).read_text(encoding="utf-8")

    assert "Current Capability Manifest lists `database_evidence_search`" in prompt
    assert "`/skills/database-analysis/SKILL.md`" in prompt
    assert "配置率, 搭载率, 配备率" in prompt
    assert "business sub-question" in prompt
    assert "selected_semantic_asset_ids" in prompt
    assert "The Agent writes the SQL" in prompt
    assert "`database_sql_validate`" in prompt
    assert "returned `sql_submission_id`" in prompt
    assert "paired validation Receipt" in prompt
    assert "never bypass the Receipt chain" in prompt
    assert "retrieved evidence is insufficient for a physical mapping" in prompt
    assert "`database_sql_generate`" not in prompt


def test_default_tool_guides_require_execution_self_check_before_completion() -> None:
    prompt = (Path(__file__).resolve().parent.parent / "prompts" / "tool_guides" / "core.md").read_text(
        encoding="utf-8"
    )

    assert "finish or" in prompt
    assert "explicitly cancel every Todo" in prompt
    assert "read back each declared deliverable" in prompt
    assert "lidar/HUD" in prompt
    assert "continue the Model/Tools loop and repair it" in prompt
    assert "treat it as part of" in prompt
    assert "the same Run" in prompt


def test_default_tool_guides_route_disposable_html_to_the_message() -> None:
    prompt = (Path(__file__).resolve().parent.parent / "prompts" / "tool_guides" / "core.md").read_text(
        encoding="utf-8"
    )

    assert "## Lightweight HTML placement" in prompt
    assert "one complete, standalone HTML document" in prompt
    assert "chart libraries" in prompt
    assert "not write that lightweight HTML" in prompt
    assert "`/workspace`" in prompt
    assert "formal reports" in prompt
    assert "reusable pages" in prompt


def test_default_tool_guides_define_managed_browser_authorization_lifecycle() -> None:
    prompt = (Path(__file__).resolve().parent.parent / "prompts" / "tool_guides" / "core.md").read_text(
        encoding="utf-8"
    )

    assert "status: awaiting_user_browser" in prompt
    assert "Natural-language replies" in prompt
    assert "`lark-cli auth resume`" in prompt
    assert "Never call `lark-cli auth login --device-code ...`" in prompt
    assert "Only `authorization_completed: true`" in prompt


def test_default_agent_prompt_defines_async_lifecycle_invariant() -> None:
    prompt = (Path(__file__).resolve().parent.parent / "prompts" / "AGENTS.md").read_text(
        encoding="utf-8"
    )

    assert "异步与交互式任务生命周期" in prompt
    assert "严格区分“命令或后台任务成功启动”和“用户目标已经完成”" in prompt
    assert "`awaiting_*`、`pending`、`action_required`" in prompt
    assert "结束当前轮，把控制权交还用户" in prompt
    assert "先用对应的 status/show/verify 能力验证" in prompt
    assert "结构化 `authorization_request`" in prompt
    assert "用户用自然语言表示完成即可续跑" in prompt


def test_default_agent_prompt_defines_scope_bound_memory_updates() -> None:
    prompt = (Path(__file__).resolve().parent.parent / "prompts" / "AGENTS.md").read_text(
        encoding="utf-8"
    )

    assert "`update_memory` is the only supported Memory write path" in prompt
    assert "The Backend binds it to the current Run scope" in prompt
    assert "Never use `write_file`, `execute`, or a guessed physical Home path" in prompt
    assert "Do not store transient task state" in prompt
    assert "Do not create user-layer `SOUL.md`, `IDENTITY.md`, or `USER.md`" in prompt
    assert "unless `update_memory` returned `ok: true`" in prompt


def test_default_tool_guides_use_virtual_filesystem_for_semantic_assets() -> None:
    prompt = (Path(__file__).resolve().parent.parent / "prompts" / "tool_guides" / "core.md").read_text(
        encoding="utf-8"
    )

    assert 'read_file("/semantic-assets/...", limit=1000)' in prompt
    assert "never pass it to `read_resource`" in prompt
    assert "Do not use `read_resource` for `/skills/`, `/semantic-assets/`" in prompt
    assert "convert it to the equivalent `/workspace/<relative-path>`" in prompt
    assert "Submit the original operation exactly once" in prompt
    assert "Smart trusted-local Spawn/Kernel runs execute ordinary non-sensitive reads and mutations" in prompt
    assert "without project, directory, or exact-file HITL" in prompt
    assert "an explicit `/tmp/...` path always means the operating system's real temporary directory" in prompt
    assert "do not call deprecated Stage/lease tools for a new Run" in prompt
    assert "`patch_file` with unique replacement anchors" in prompt
    assert "Smart trusted-local Spawn and Kernel share unrestricted ordinary host-path semantics" in prompt
    assert "stage_external_artifact" not in prompt
    assert "stage_external_directory" not in prompt


def test_base_prompt_excludes_request_scoped_tool_guides() -> None:
    prompt = (Path(__file__).resolve().parent.parent / "prompts" / "tool_guides" / "core.md").read_text(
        encoding="utf-8"
    )

    assert "## Resource Access" in prompt
    assert "## Completion discipline" in prompt
    assert "## Managed Browser Authorization" in prompt
    assert "## Managed CLI Toolchain Installation" in prompt
    assert "Never use `install_packages`" in prompt
    assert "Do not inspect the host with `which`" in prompt
    assert "Do not request external-directory access" in prompt
    assert "preserve the existing Bot/App configuration" in prompt
    assert "never restart App configuration merely because the user says" in prompt
    assert "Run `lark-cli config init --new` only for first-time setup" in prompt
    assert "## Source Citation Rules" in prompt
    assert "## Database Analysis" not in prompt
    assert "## Semantic Dimension Builds" not in prompt


def test_default_agent_prompt_requires_chinese_user_visible_output() -> None:
    prompt = (Path(__file__).resolve().parent.parent / "prompts" / "AGENTS.md").read_text(
        encoding="utf-8"
    )

    assert "所有对用户可见的输出必须使用中文" in prompt
    assert "工具调用前后的过渡语" in prompt
    assert "内部隐藏推理可以使用英文" in prompt


def test_default_agent_prompt_reuses_summary_before_database_skill_activation() -> None:
    prompt = (Path(__file__).resolve().parent.parent / "prompts" / "AGENTS.md").read_text(
        encoding="utf-8"
    )

    assert "## 压缩摘要、近期历史与重复查询" in prompt
    assert "在激活任何 Skill 或调用任何查询工具之前" in prompt
    assert "基于本会话已有结果" in prompt
    assert "不要先激活 `/skills/database-analysis/SKILL.md`" in prompt
    assert "询问用户是否需要按当前数据库重新查询" in prompt
    assert "近期消息中的更新事实优先于较早摘要" in prompt
    assert "用户确认后的新一轮" in prompt


def test_default_agent_prompt_routes_markdown_wiki_first_and_documents_conditionally() -> None:
    prompt = (Path(__file__).resolve().parent.parent / "prompts" / "AGENTS.md").read_text(
        encoding="utf-8"
    )

    assert "## Knowledge Source Routing" in prompt
    assert "内部知识优先是普通知识任务的默认来源策略" in prompt
    assert "不要求用户先说“知识库”" in prompt
    assert "项目/产品清单、技术比较、选型建议" in prompt
    assert "出现“开源”“有哪些项目”“推荐”" in prompt
    assert "先读取 `/skills/llm-wiki/SKILL.md`" in prompt
    assert "读取 Skill 只完成能力激活，不等于已经检索" in prompt
    assert '`llm_wiki_context(operation="query")`' in prompt
    assert "不默认并行调用 `llamaindex_knowledge_query`" in prompt
    assert "Wiki 无直接命中、覆盖不完整" in prompt
    assert "用户要求原始证据或具体 PDF/Markdown/图片/图表" in prompt
    assert "才读取 `/skills/knowledge-search/SKILL.md`" in prompt
    assert "仅在实体关系、图谱遍历或结构化筛选有价值时" in prompt
    assert "不得替代或跳过本轮 `llm_wiki_query`" in prompt
    assert "本轮按上述条件需要的补充路径" in prompt
    assert "才把 Web 作为补充来源" in prompt


def test_knowledge_search_skill_requires_actual_document_index_query() -> None:
    skill = (Path(__file__).resolve().parent.parent / "skills" / "knowledge-search" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(skill.split())

    assert "call `llamaindex_knowledge_query` after activating this Skill" in normalized
    assert "Reading this file only activates the toolset" in normalized
    assert "Markdown LLM Wiki and GBrain are separate knowledge paths" in normalized
