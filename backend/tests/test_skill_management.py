from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from langchain.agents.middleware.types import ToolCallRequest

from api.permissions import ToolActionGrantRequest, grant_tool_action_permission, list_permissions
from graph.permission_resume import permission_resume_registry
from graph.session_manager import session_manager
from harness.tool_execution import PolicyDecision, ToolExecutionPipeline
from services import skill_management as skill_management_module
from services.skill_management import SkillManagementError, SkillManagementService


def _write_skill(root: Path, *, version: str, body: str = "") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        f"---\nname: demo-skill\nversion: {version}\ndescription: demo\n---\n# Demo\n{body}",
        encoding="utf-8",
    )


def _stage_version(version: str, *, extra: dict[str, str] | None = None):
    def stage(*, target: Path, **_kwargs) -> None:
        _write_skill(target, version=version)
        for name, content in (extra or {}).items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    return stage


def test_prepare_and_commit_install_is_immutable_and_non_overwriting(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)
    monkeypatch.setattr(service, "_stage_source", _stage_version("1.0.0", extra={"scripts/run.py": "print(1)\n"}))
    monkeypatch.setattr(service, "_refresh_skill_snapshot", lambda: None)

    plan = service.prepare(action="install", source="https://example.com/demo/")

    assert plan["skill_name"] == "demo-skill"
    assert plan["diff"]["summary"] == "+2 ~0 -0 =0"
    assert not (tmp_path / "skills" / "demo-skill").exists()

    result = service.commit(
        action="install",
        plan_id=plan["plan_id"],
        plan_sha256=plan["plan_sha256"],
    )

    assert result["ok"] is True
    assert (tmp_path / "skills" / "demo-skill" / "scripts" / "run.py").is_file()
    with pytest.raises(SkillManagementError, match="plan_already_consumed"):
        service.commit(
            action="install",
            plan_id=plan["plan_id"],
            plan_sha256=plan["plan_sha256"],
        )


def test_update_creates_snapshot_and_rejects_changed_baseline(tmp_path, monkeypatch):
    installed = tmp_path / "skills" / "demo-skill"
    _write_skill(installed, version="1.0.0")
    (installed / "old.txt").write_text("old", encoding="utf-8")
    service = SkillManagementService(tmp_path)
    monkeypatch.setattr(service, "_stage_source", _stage_version("2.0.0", extra={"new.txt": "new"}))
    monkeypatch.setattr(service, "_refresh_skill_snapshot", lambda: None)

    stale = service.prepare(action="update", source="https://example.com/demo/")
    (installed / "old.txt").write_text("changed after prepare", encoding="utf-8")
    with pytest.raises(SkillManagementError) as conflict:
        service.commit(action="update", plan_id=stale["plan_id"], plan_sha256=stale["plan_sha256"])
    assert conflict.value.code == "installed_skill_changed"

    # A fresh plan is bound to the now-current baseline and may be committed.
    fresh = service.prepare(action="update", source="https://example.com/demo/")
    result = service.commit(action="update", plan_id=fresh["plan_id"], plan_sha256=fresh["plan_sha256"])

    assert result["snapshot_id"]
    assert "version: 2.0.0" in (installed / "SKILL.md").read_text(encoding="utf-8")
    assert not (installed / "old.txt").exists()
    snapshot = tmp_path / "data" / "skill-management" / "snapshots" / "demo-skill" / result["snapshot_id"]
    assert (snapshot / "old.txt").read_text(encoding="utf-8") == "changed after prepare"


def test_managed_install_records_source_for_future_update(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)
    monkeypatch.setattr(service, "_stage_source", _stage_version("1.0.0"))
    monkeypatch.setattr(service, "_refresh_skill_snapshot", lambda: None)
    install = service.prepare(
        action="install",
        source="https://github.com/example/skills/tree/stable/demo",
        ref="stable",
        subpath="demo",
        files=["references/schema.md"],
    )
    service.commit(action="install", plan_id=install["plan_id"], plan_sha256=install["plan_sha256"])

    observed: dict[str, object] = {}

    def stage_update(**kwargs) -> None:
        observed.update(kwargs)
        _stage_version("2.0.0")(**kwargs)

    monkeypatch.setattr(service, "_stage_source", stage_update)
    update = service.prepare(action="update", skill_name="demo-skill")

    assert observed["source"] == "https://github.com/example/skills/tree/stable/demo"
    assert observed["ref"] == "stable"
    assert observed["subpath"] == "demo"
    assert observed["files"] == ["references/schema.md"]
    assert update["source"] == observed["source"]


def test_plan_digest_and_staged_payload_are_verified(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)
    monkeypatch.setattr(service, "_stage_source", _stage_version("1.0.0"))
    monkeypatch.setattr(service, "_refresh_skill_snapshot", lambda: None)
    plan = service.prepare(action="install", source="https://example.com/demo/")

    with pytest.raises(SkillManagementError, match="plan_digest_mismatch"):
        service.commit(action="install", plan_id=plan["plan_id"], plan_sha256="0" * 64)
    payload = service.plans_dir / plan["plan_id"] / "payload" / "SKILL.md"
    payload.write_text(payload.read_text(encoding="utf-8") + "tampered", encoding="utf-8")
    with pytest.raises(SkillManagementError, match="staged_payload_changed"):
        service.commit(action="install", plan_id=plan["plan_id"], plan_sha256=plan["plan_sha256"])


def test_archive_rejects_path_traversal_and_symlink(tmp_path):
    service = SkillManagementService(tmp_path)
    traversal = io.BytesIO()
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../SKILL.md", "bad")
    with pytest.raises(SkillManagementError, match="archive_path_traversal"):
        service._extract_archive(traversal.getvalue(), tmp_path / "payload-a", subpath="", github_archive=False)

    linked = io.BytesIO()
    with zipfile.ZipFile(linked, "w") as archive:
        info = zipfile.ZipInfo("demo/link")
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "SKILL.md")
    with pytest.raises(SkillManagementError, match="skill_symlink_not_supported"):
        service._extract_archive(linked.getvalue(), tmp_path / "payload-b", subpath="", github_archive=False)


def test_web_directory_discovers_bounded_referenced_files(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)
    payloads = {
        "https://skills.example/demo/SKILL.md": b"---\nname: demo-skill\n---\nRun scripts/query.py\n",
        "https://skills.example/demo/README.md": b"Logo: assets/logo.svg\n",
        "https://skills.example/demo/scripts/query.py": b"print('ok')\n",
        "https://skills.example/demo/assets/logo.svg": b"<svg />",
    }
    monkeypatch.setattr(service, "_download", lambda url: payloads[url])

    target = tmp_path / "staged"
    service._stage_web_directory("https://skills.example/demo/", target, [])

    assert sorted(path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()) == [
        "README.md",
        "SKILL.md",
        "assets/logo.svg",
        "scripts/query.py",
    ]


def test_private_source_addresses_are_rejected_without_network_access():
    for url in ("http://127.0.0.1/SKILL.md", "http://[::1]/SKILL.md", "http://localhost/SKILL.md"):
        with pytest.raises(SkillManagementError) as error:
            SkillManagementService._validate_public_url(url)
        assert error.value.code == "private_source_address"


def test_skill_source_allows_https_fake_ip_hostname_only(monkeypatch):
    monkeypatch.setattr(
        skill_management_module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                skill_management_module.socket.AF_INET,
                skill_management_module.socket.SOCK_STREAM,
                6,
                "",
                ("198.18.0.118", 443),
            ),
        ],
    )

    SkillManagementService._validate_public_url("https://aihot.example/SKILL.md")
    with pytest.raises(SkillManagementError) as insecure:
        SkillManagementService._validate_public_url("http://aihot.example/SKILL.md")
    assert insecure.value.code == "private_source_address"
    with pytest.raises(SkillManagementError) as literal:
        SkillManagementService._validate_public_url("https://198.18.0.118/SKILL.md")
    assert literal.value.code == "private_source_address"


def test_skill_source_without_vpn_keeps_public_dns_behavior(monkeypatch):
    monkeypatch.setattr(
        skill_management_module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                skill_management_module.socket.AF_INET,
                skill_management_module.socket.SOCK_STREAM,
                6,
                "",
                ("93.184.216.34", 443),
            ),
        ],
    )

    SkillManagementService._validate_public_url("https://public.example/SKILL.md")


def test_failed_atomic_update_restores_installed_skill(tmp_path, monkeypatch):
    installed = tmp_path / "skills" / "demo-skill"
    _write_skill(installed, version="1.0.0")
    service = SkillManagementService(tmp_path)
    monkeypatch.setattr(service, "_stage_source", _stage_version("2.0.0"))
    monkeypatch.setattr(service, "_refresh_skill_snapshot", lambda: None)
    plan = service.prepare(action="update", source="https://example.com/demo/")
    real_replace = skill_management_module.os.replace
    calls = 0

    def fail_new_target(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated atomic swap failure")
        return real_replace(source, target)

    monkeypatch.setattr(skill_management_module.os, "replace", fail_new_target)
    with pytest.raises(OSError, match="simulated"):
        service.commit(action="update", plan_id=plan["plan_id"], plan_sha256=plan["plan_sha256"])

    assert "version: 1.0.0" in (installed / "SKILL.md").read_text(encoding="utf-8")
    assert service.preview(plan["plan_id"])["status"] == "prepared"


def test_skill_management_policy_requires_network_then_one_time_managed_write(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)
    monkeypatch.setattr(service, "_stage_source", _stage_version("1.0.0"))
    plan = service.prepare(action="install", source="https://example.com/demo/")
    pipeline = ToolExecutionPipeline(
        known_tools={"prepare_skill_install", "install_skill"},
        backend_mode="restricted_host",
        base_dir=tmp_path,
    )
    prepare = ToolCallRequest(
        tool_call={"id": "prepare", "name": "prepare_skill_install", "args": {"source": "https://example.com/demo/"}},
        tool=None,
        state={},
        runtime=None,
    )
    commit = ToolCallRequest(
        tool_call={
            "id": "commit",
            "name": "install_skill",
            "args": {"plan_id": plan["plan_id"], "plan_sha256": plan["plan_sha256"]},
        },
        tool=None,
        state={},
        runtime=None,
    )

    assert pipeline._preflight(prepare).decision == PolicyDecision.ASK
    assert pipeline._required_capabilities(prepare) == ["execute", "temporary_network"]
    prepare_preview = pipeline._skill_change_preview(prepare)
    assert prepare_preview == {
        "action": "prepare_install",
        "source": "https://example.com/demo/",
    }
    assert pipeline._preflight(commit).risk == "managed_skill_write"
    assert pipeline._required_capabilities(commit) == ["execute", "managed_skill_write"]
    assert pipeline._session_grant_scope(commit) is None
    preview = json.loads(pipeline._action_preview(commit))
    assert preview["verified_plan"]["diff"] == plan["diff"]
    assert preview["verified_plan"]["plan_sha256"] == plan["plan_sha256"]
    card_preview = pipeline._skill_change_preview(commit)
    assert card_preview is not None
    assert card_preview["skill_name"] == "demo-skill"
    assert card_preview["changes"] == plan["diff"]["summary"]
    assert card_preview["plan_sha256"] == plan["plan_sha256"]


def test_skill_management_permission_api_rejects_session_scope(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("skill-permission-session")

    async def scenario() -> None:
        pending = permission_resume_registry.create_tool_action_request(
            session_id="skill-permission-session",
            query_id="query-1",
            run_id="run-1",
            tool_call_id="call-1",
            tool_name="update_skill",
            command="{}",
            reason="managed_skill_write:update_skill",
            risk="managed_skill_write",
            required_capabilities=["execute", "managed_skill_write"],
        )
        try:
            with pytest.raises(HTTPException) as error:
                await grant_tool_action_permission(
                    "skill-permission-session",
                    ToolActionGrantRequest(permission_request_id=pending["id"], scope="session"),
                )
            assert error.value.status_code == 400
            assert "one-time" in str(error.value.detail)
        finally:
            permission_resume_registry.resolve(pending["id"], {"type": "reject"})

    asyncio.run(scenario())


def test_skill_permission_history_remains_visible_after_one_time_consumption(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("skill-history-session")
    fingerprint = "skill-plan-fingerprint"
    session_manager.add_permission_grant(
        "skill-history-session",
        grant_type="tool_action",
        target_kind="fingerprint",
        target=fingerprint,
        capabilities=["execute", "managed_skill_write"],
        scope="once",
        metadata={
            "tool_name": "update_skill",
            "command": "{}",
            "change_preview": {
                "skill_name": "aihot",
                "source": "https://github.com/example/skills",
                "changes": "+0 ~2 -1 =0",
            },
        },
    )

    assert session_manager.consume_tool_action_permission(
        "skill-history-session",
        fingerprint,
        required_capabilities=["execute", "managed_skill_write"],
    )
    response = asyncio.run(list_permissions("skill-history-session"))
    assert response["grants"] == []
    assert len(response["history"]) == 1
    assert response["history"][0]["consumed_at"]
    assert response["history"][0]["metadata"]["change_preview"]["skill_name"] == "aihot"
