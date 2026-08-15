"""Project and local-file API policy tests."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.session_manager import session_manager


def _client(tmp_path: Path, monkeypatch, *, approval_mode: str) -> tuple[TestClient, Path, list[Path]]:
    from api import projects as projects_api

    session_store = tmp_path / "sessions"
    workspace = tmp_path / "project-a"
    workspace.mkdir()
    session_manager.initialize(session_store)
    session_manager.create_session(
        "session-1",
        metadata={"runtime_mode": "agent", "workspace_path": str(workspace)},
        approval_mode=approval_mode,
    )

    opened: list[Path] = []
    monkeypatch.setattr(projects_api, "_open_in_file_manager", opened.append)
    app = FastAPI()
    app.include_router(projects_api.router, prefix="/api")
    return TestClient(app), workspace, opened


def test_smart_session_opens_ordinary_subagent_file_outside_parent_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, _workspace, opened = _client(tmp_path, monkeypatch, approval_mode="smart")
    subagent_file = tmp_path / "project-b" / "subagent-source.txt"
    subagent_file.parent.mkdir()
    subagent_file.write_text("subagent evidence", encoding="utf-8")

    response = client.post(
        "/api/local-files/open",
        json={"path": subagent_file.as_uri(), "session_id": "session-1"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "path": str(subagent_file.resolve())}
    assert opened == [subagent_file.resolve()]


def test_strict_session_keeps_local_file_open_confined_to_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, _workspace, opened = _client(tmp_path, monkeypatch, approval_mode="strict")
    external_file = tmp_path / "project-b" / "external.txt"
    external_file.parent.mkdir()
    external_file.write_text("external", encoding="utf-8")

    response = client.post(
        "/api/local-files/open",
        json={"path": external_file.as_uri(), "session_id": "session-1"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "File is outside the session workspace"
    assert opened == []


def test_strict_session_can_open_file_inside_workspace(tmp_path: Path, monkeypatch) -> None:
    client, workspace, opened = _client(tmp_path, monkeypatch, approval_mode="strict")
    local_file = workspace / "artifact.txt"
    local_file.write_text("artifact", encoding="utf-8")

    response = client.post(
        "/api/local-files/open",
        json={"path": local_file.as_uri(), "session_id": "session-1"},
    )

    assert response.status_code == 200
    assert opened == [local_file.resolve()]


def test_smart_session_does_not_open_sensitive_external_file(tmp_path: Path, monkeypatch) -> None:
    client, _workspace, opened = _client(tmp_path, monkeypatch, approval_mode="smart")
    sensitive_file = tmp_path / ".ssh" / "id_rsa"
    sensitive_file.parent.mkdir()
    sensitive_file.write_text("secret", encoding="utf-8")

    response = client.post(
        "/api/local-files/open",
        json={"path": sensitive_file.as_uri(), "session_id": "session-1"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Sensitive host files cannot be opened from a generated file link"
    )
    assert opened == []
