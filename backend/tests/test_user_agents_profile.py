from pathlib import Path

import pytest
from fastapi import HTTPException


def test_profile_file_api_only_allows_user_agents(monkeypatch, tmp_path: Path) -> None:
    from api.files import _validate_path

    monkeypatch.setenv("PUDDINGCLAW_HOME", str(tmp_path))

    assert _validate_path("profile/AGENTS.md") == tmp_path / "profile" / "AGENTS.md"
    for unsupported in ("profile/SOUL.md", "profile/IDENTITY.md", "profile/USER.md"):
        with pytest.raises(HTTPException) as exc_info:
            _validate_path(unsupported)
        assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_user_agents_file_is_created_in_home(monkeypatch, tmp_path: Path) -> None:
    from api.files import FileSaveRequest, read_file, save_file

    monkeypatch.setenv("PUDDINGCLAW_HOME", str(tmp_path))

    missing = await read_file("profile/AGENTS.md")
    assert missing == {"path": "profile/AGENTS.md", "content": ""}

    await save_file(FileSaveRequest(path="profile/AGENTS.md", content="用户追加规则"))

    target = tmp_path / "profile" / "AGENTS.md"
    assert target.read_text(encoding="utf-8") == "用户追加规则"


def test_deepagents_memory_has_no_backend_fallback(tmp_path: Path) -> None:
    from graph.deepagents_manager import DeepAgentsAgentManager

    manager = DeepAgentsAgentManager()
    manager._base_dir = tmp_path / "backend"

    with pytest.raises(RuntimeError, match="user Home"):
        manager._memory_dir_for(None)

    assert not (tmp_path / "backend" / "memory").exists()


def test_user_agents_middleware_places_home_layer_before_project_and_runtime() -> None:
    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain_core.messages import SystemMessage

    from graph.middlewares.user_agents_prompt import UserAgentsPromptMiddleware

    request = ModelRequest(
        model=None,
        messages=[],
        system_message=SystemMessage(
            content=(
                "## Stable Core\n\nSYSTEM\n\n"
                "## Project AGENTS\n\nPROJECT\n\n"
                "<agent_memory>MEMORY</agent_memory>\n\n"
                "## Current Run Delta\n\nRUNTIME\n\n"
                "## Agent Core\n\nDEEPAGENTS"
            )
        ),
        tools=[],
        state={"messages": []},
        runtime=None,
    )
    captured: dict[str, str] = {}

    def handler(updated: ModelRequest) -> ModelResponse:
        captured["text"] = updated.system_message.text
        return ModelResponse(result=[])

    UserAgentsPromptMiddleware("## User AGENTS Additions\n\nUSER FINAL").wrap_model_call(
        request,
        handler,
    )

    text = captured["text"]
    assert text.index("## Agent Core") < text.index("## User AGENTS Additions")
    assert text.index("## User AGENTS Additions") < text.index("## Project AGENTS")
    assert text.index("## Project AGENTS") < text.index("<agent_memory>")
    assert text.index("<agent_memory>") < text.index("## Current Run Delta")


def test_profile_todo_prompt_stays_in_runtime_after_stable_user_agents() -> None:
    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain_core.messages import SystemMessage

    from graph.middlewares.harness_todos import HarnessTodoMiddleware
    from graph.middlewares.user_agents_prompt import UserAgentsPromptMiddleware

    request = ModelRequest(
        model=None,
        messages=[],
        system_message=SystemMessage(
            content="## Stable Core\n\nSYSTEM\n\n## Project AGENTS\n\nPROJECT"
        ),
        tools=[],
        state={"messages": []},
        runtime=None,
    )
    captured: dict[str, str] = {}

    def final_handler(updated: ModelRequest) -> ModelResponse:
        captured["text"] = updated.system_message.text
        return ModelResponse(result=[])

    def profile_tail(updated: ModelRequest) -> ModelResponse:
        return HarnessTodoMiddleware().wrap_model_call(updated, final_handler)

    UserAgentsPromptMiddleware("## User AGENTS Additions\n\nUSER FINAL").wrap_model_call(
        request,
        profile_tail,
    )

    assert captured["text"].index("## User AGENTS Additions") < captured["text"].index(
        "## Project AGENTS"
    )
    assert captured["text"].index("## Project AGENTS") < captured["text"].index(
        "## `update_todos`"
    )
    assert "For complex work, maintain the Todo ledger" in captured["text"]
    assert "{'type': 'text'" not in captured["text"]
