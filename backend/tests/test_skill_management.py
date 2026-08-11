from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.testclient import TestClient
from langchain.agents.middleware.types import ToolCallRequest
from starlette.requests import Request

from api import skill_plans as skill_plans_api
from api import skills_api as skills_api_module
from api.permissions import ToolActionGrantRequest, grant_tool_action_permission, list_permissions
from api.skill_plans import SkillPlanDecision, cancel_skill_plan, commit_skill_plan, get_skill_plan
from api.skills_api import UploadPlanDecision, commit_uploaded_skill, import_skill
from graph.permission_resume import permission_resume_registry
from graph.session_manager import session_manager
from harness.tool_execution import PolicyDecision, ToolExecutionPipeline
from services import skill_management as skill_management_module
from services.skill_management import SkillManagementError, SkillManagementService
from tools.skill_management_tool import PrepareSkillInstallTool


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


def test_uploaded_skill_file_uses_managed_plan_and_commits_direct_install(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)
    monkeypatch.setattr(service, "_refresh_skill_snapshot", lambda: None)

    plan = service.prepare_upload(
        filename="local-demo.skill",
        content=b"# Local demo\n\nImported directly by the user.\n",
    )

    assert plan["action"] == "install"
    assert plan["skill_name"] == "local-demo"
    assert plan["source"] == "upload:local-demo.skill"
    assert not (tmp_path / "skills" / "local-demo").exists()

    installed = service.commit(
        action="install",
        plan_id=plan["plan_id"],
        plan_sha256=plan["plan_sha256"],
    )

    assert installed["provenance_recorded"] is False
    assert (tmp_path / "skills" / "local-demo" / "SKILL.md").read_bytes().startswith(b"# Local demo")


def test_uploaded_folder_update_requires_plan_commit_and_can_be_cancelled(tmp_path, monkeypatch):
    installed = tmp_path / "skills" / "demo-skill"
    _write_skill(installed, version="1.0.0")
    (installed / "old.txt").write_text("keep me", encoding="utf-8")
    service = SkillManagementService(tmp_path)
    monkeypatch.setattr(service, "_refresh_skill_snapshot", lambda: None)

    plan = service.prepare_upload(
        filename="demo-skill.folder",
        skill_name="demo-skill",
        uploaded_files=[
            (
                "demo-skill/SKILL.md",
                b"---\nname: demo-skill\nversion: 2.0.0\ndescription: demo\n---\n# Demo\n",
            ),
            ("demo-skill/new.txt", b"new"),
        ],
    )

    assert plan["action"] == "update"
    assert plan["diff"]["added"] == ["new.txt"]
    assert plan["diff"]["removed"] == ["old.txt"]
    assert "version: 1.0.0" in (installed / "SKILL.md").read_text(encoding="utf-8")
    assert (installed / "old.txt").read_text(encoding="utf-8") == "keep me"

    cancelled = service.cancel(plan_id=plan["plan_id"], plan_sha256=plan["plan_sha256"])
    assert cancelled["status"] == "cancelled"
    assert "version: 1.0.0" in (installed / "SKILL.md").read_text(encoding="utf-8")
    assert (installed / "old.txt").is_file()


def test_uploaded_folder_rejects_unsafe_names_paths_and_oversized_archives(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)

    with pytest.raises(SkillManagementError) as unsafe_name:
        service.prepare_upload(
            filename="escape.folder",
            skill_name="../../escape",
            uploaded_files=[("escape/SKILL.md", b"# Escape")],
        )
    assert unsafe_name.value.code == "invalid_skill_name"
    assert not (tmp_path.parent / "escape").exists()

    with pytest.raises(SkillManagementError) as unsafe_path:
        service.prepare_upload(
            filename="demo.folder",
            skill_name="demo-skill",
            uploaded_files=[("demo/../SKILL.md", b"# Escape")],
        )
    assert unsafe_path.value.code == "invalid_relative_path"

    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("large-skill/SKILL.md", "---\nname: large-skill\n---\n" + ("x" * 512))
    monkeypatch.setattr(skill_management_module, "_MAX_TOTAL_BYTES", 128)
    with pytest.raises(SkillManagementError) as oversized:
        service.prepare_upload(filename="large-skill.zip", content=archive_bytes.getvalue())
    assert oversized.value.code == "skill_size_limit_exceeded"
    assert not service.plans_dir.exists() or not any(service.plans_dir.iterdir())


def test_upload_api_installs_new_skill_and_requires_confirmation_for_update(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)
    monkeypatch.setattr(service, "_refresh_skill_snapshot", lambda: None)
    monkeypatch.setattr(skills_api_module, "_upload_service", lambda: service)

    created = asyncio.run(
        import_skill(
            files=[
                UploadFile(
                    filename="api-demo.skill",
                    file=io.BytesIO(b"---\nname: api-demo\nversion: 1.0.0\n---\n# API demo\n"),
                )
            ],
            skill_name=None,
        )
    )
    assert created["requires_confirmation"] is False
    assert "version: 1.0.0" in (tmp_path / "skills" / "api-demo" / "SKILL.md").read_text(encoding="utf-8")

    prepared = asyncio.run(
        import_skill(
            files=[
                UploadFile(
                    filename="api-demo.skill",
                    file=io.BytesIO(b"---\nname: api-demo\nversion: 2.0.0\n---\n# API demo\n"),
                )
            ],
            skill_name=None,
        )
    )
    assert prepared["requires_confirmation"] is True
    assert prepared["plan"]["action"] == "update"
    assert "version: 1.0.0" in (tmp_path / "skills" / "api-demo" / "SKILL.md").read_text(encoding="utf-8")

    committed = asyncio.run(
        commit_uploaded_skill(
            prepared["plan"]["plan_id"],
            UploadPlanDecision(plan_sha256=prepared["plan"]["plan_sha256"]),
        )
    )
    assert committed["requires_confirmation"] is False
    assert committed["plan"]["snapshot_id"]
    assert "version: 2.0.0" in (tmp_path / "skills" / "api-demo" / "SKILL.md").read_text(encoding="utf-8")


def test_upload_api_rejects_untrusted_browser_origins():
    trusted = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/skills/import",
            "headers": [(b"origin", b"http://localhost:3000")],
        }
    )
    skills_api_module._require_trusted_upload_origin(trusted)

    untrusted = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/skills/import",
            "headers": [(b"origin", b"https://evil.example")],
        }
    )
    with pytest.raises(HTTPException) as blocked:
        skills_api_module._require_trusted_upload_origin(untrusted)
    assert blocked.value.status_code == 403


def test_upload_http_contract_accepts_files_multipart_field(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)
    monkeypatch.setattr(service, "_refresh_skill_snapshot", lambda: None)
    monkeypatch.setattr(skills_api_module, "_upload_service", lambda: service)
    api = FastAPI()
    api.include_router(skills_api_module.router, prefix="/api")

    with TestClient(api) as client:
        response = client.post(
            "/api/skills/import",
            files={"files": ("multipart-demo.skill", b"# Multipart demo\n", "text/markdown")},
            headers={"Origin": "http://localhost:3000"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["skill_name"] == "multipart-demo"
    assert (tmp_path / "skills" / "multipart-demo" / "SKILL.md").is_file()


def test_upload_http_contract_accepts_legacy_file_field_with_anthropic_style_zip(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)
    monkeypatch.setattr(service, "_refresh_skill_snapshot", lambda: None)
    monkeypatch.setattr(skills_api_module, "_upload_service", lambda: service)
    api = FastAPI()
    api.include_router(skills_api_module.router, prefix="/api")
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "pdf/SKILL.md",
            "---\nname: pdf\ndescription: Read and process PDF files.\n"
            "license: Proprietary. LICENSE.txt has complete terms\n---\n# PDF\n",
        )
        archive.writestr("pdf/LICENSE.txt", "License terms\n")
        archive.writestr("pdf/scripts/convert_pdf_to_images.py", "print('ok')\n")

    with TestClient(api) as client:
        response = client.post(
            "/api/skills/import",
            files={"file": ("anthropic-pdf-skill.zip", archive_bytes.getvalue(), "application/zip")},
            headers={"Origin": "http://localhost:3000"},
        )

    assert response.status_code == 200
    assert response.json()["skill_name"] == "pdf"
    assert (tmp_path / "skills" / "pdf" / "SKILL.md").is_file()
    assert (tmp_path / "skills" / "pdf" / "scripts" / "convert_pdf_to_images.py").is_file()


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


def test_npx_add_well_known_source_creates_managed_plans_without_installing(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)
    index = {
        "skills": [
            {
                "name": "alpha-skill",
                "description": "Alpha test skill",
                "files": ["SKILL.md", "references/alpha.md"],
            },
            {
                "name": "beta-skill",
                "description": "Beta test skill",
                "files": ["SKILL.md"],
            },
        ]
    }
    payloads = {
        "https://skills.example/.well-known/agent-skills/index.json": json.dumps(index).encode(),
        "https://skills.example/.well-known/agent-skills/alpha-skill/SKILL.md": (
            b"---\nname: alpha-skill\ndescription: Alpha test skill\n---\n# Alpha\n"
        ),
        "https://skills.example/.well-known/agent-skills/alpha-skill/references/alpha.md": b"alpha\n",
        "https://skills.example/.well-known/agent-skills/beta-skill/SKILL.md": (
            b"---\nname: beta-skill\ndescription: Beta test skill\n---\n# Beta\n"
        ),
    }

    def download(url: str) -> bytes:
        if url.endswith("/README.md"):
            raise SkillManagementError("http_error_404")
        if url not in payloads:
            raise SkillManagementError("http_error_404")
        return payloads[url]

    monkeypatch.setattr(service, "_download", download)
    result = service.prepare_npx_skills_add(
        source="https://skills.example",
        yes=True,
        request_context={"session_id": "session-npx", "query_id": "query-npx"},
    )

    assert result["ok"] is True
    assert result["managed_by"] == "skill_management"
    assert result["intercepted"] is True
    assert [plan["skill_name"] for plan in result["plans"]] == ["alpha-skill", "beta-skill"]
    assert all(plan["ui_commit_supported"] is True for plan in result["plans"])
    assert not (tmp_path / "skills" / "alpha-skill").exists()
    assert not (tmp_path / "skills" / "beta-skill").exists()
    alpha_plan = result["plans"][0]
    staged_reference = service.plans_dir / alpha_plan["plan_id"] / "payload" / "references" / "alpha.md"
    assert staged_reference.read_text(encoding="utf-8") == "alpha\n"


def test_npx_add_well_known_source_requires_selection_without_yes(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)
    index = {
        "skills": [
            {"name": "alpha-skill", "description": "Alpha", "files": ["SKILL.md"]},
            {"name": "beta-skill", "description": "Beta", "files": ["SKILL.md"]},
        ]
    }
    monkeypatch.setattr(
        service,
        "_download",
        lambda url: (
            json.dumps(index).encode()
            if url == "https://skills.example/.well-known/agent-skills/index.json"
            else (_ for _ in ()).throw(SkillManagementError("http_error_404"))
        ),
    )

    result = service.prepare_npx_skills_add(source="https://skills.example")

    assert result["selection_required"] is True
    assert result["available_skills"] == ["alpha-skill", "beta-skill"]
    assert result["plans"] == []


def test_well_known_v2_skill_md_digest_is_verified(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)
    content = b"---\nname: digest-skill\ndescription: Digest\n---\n# Digest\n"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    index = {
        "$schema": "https://schemas.agentskills.io/discovery/0.2.0/schema.json",
        "skills": [
            {
                "name": "digest-skill",
                "type": "skill-md",
                "description": "Digest test skill",
                "url": "/artifacts/digest/SKILL.md",
                "digest": digest,
            }
        ],
    }
    payloads = {
        "https://skills.example/.well-known/agent-skills/index.json": json.dumps(index).encode(),
        "https://skills.example/artifacts/digest/SKILL.md": content,
    }
    monkeypatch.setattr(
        service,
        "_download",
        lambda url: payloads[url] if url in payloads else (_ for _ in ()).throw(SkillManagementError("http_error_404")),
    )

    result = service.prepare_npx_skills_add(
        source="https://skills.example",
        yes=True,
        request_context={"session_id": "digest-session"},
    )

    assert result["ok"] is True
    assert result["plans"][0]["source_digest"] == digest
    assert result["plans"][0]["skill_name"] == "digest-skill"

    with pytest.raises(SkillManagementError, match="source_digest_mismatch"):
        service._stage_source(
            source="https://skills.example/artifacts/digest/SKILL.md",
            target=tmp_path / "bad-digest",
            ref="main",
            subpath="",
            files=[],
            source_digest="sha256:" + "0" * 64,
        )


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
        backend_mode="spawn",
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


def test_session_bound_prepare_is_idempotent_and_persists_expiry(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)
    stage_calls = 0

    def stage(**kwargs) -> None:
        nonlocal stage_calls
        stage_calls += 1
        _stage_version("1.0.0")(**kwargs)

    monkeypatch.setattr(service, "_stage_source", stage)
    context = {"session_id": "session-a", "query_id": "query-a", "run_id": "run-a"}
    first = service.prepare(
        action="install",
        source="https://example.com/demo/",
        request_context=context,
    )
    second = service.prepare(
        action="install",
        source="https://example.com/demo/",
        request_context=context,
    )

    assert second["plan_id"] == first["plan_id"]
    assert stage_calls == 1
    assert first["phase"] == "awaiting_confirmation"
    assert first["requires_confirmation"] is True

    monkeypatch.setattr(skill_management_module.time, "time", lambda: first["expires_at"] + 1)
    expired = service.preview_for_session(first["plan_id"], "session-a")
    assert expired["status"] == "expired"
    assert expired["phase"] == "expired"
    assert not (service.plans_dir / first["plan_id"] / "payload").exists()


def test_prepare_tool_binds_plan_to_agent_request_context(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)
    monkeypatch.setattr(service, "_stage_source", _stage_version("1.0.0"))
    tool = PrepareSkillInstallTool(
        service=service,
        session_id="session-bound",
        query_id="query-bound",
        run_id="run-bound",
    )

    result = json.loads(tool._run(source="https://example.com/demo/"))

    assert result["phase"] == "awaiting_confirmation"
    assert result["ui_commit_supported"] is True
    assert service.preview_for_session(result["plan_id"], "session-bound")["plan_id"] == result["plan_id"]
    with pytest.raises(SkillManagementError, match="plan_session_mismatch"):
        service.preview_for_session(result["plan_id"], "another-session")
    with pytest.raises(SkillManagementError) as structured_only:
        service.commit(
            action="install",
            plan_id=result["plan_id"],
            plan_sha256=result["plan_sha256"],
        )
    assert structured_only.value.code == "plan_requires_structured_commit"


def test_legacy_prepare_stays_on_approval_gated_tool_path(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)
    monkeypatch.setattr(service, "_stage_source", _stage_version("1.0.0"))

    plan = service.prepare(action="install", source="https://example.com/demo/")

    assert plan["ui_commit_supported"] is False
    result = service.commit(
        action="install",
        plan_id=plan["plan_id"],
        plan_sha256=plan["plan_sha256"],
    )
    assert result["status"] == "committed"


def test_structured_skill_plan_commit_is_session_bound_idempotent_and_audited(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)
    monkeypatch.setattr(service, "_stage_source", _stage_version("1.0.0"))
    monkeypatch.setattr(service, "_refresh_skill_snapshot", lambda: None)
    monkeypatch.setattr(skill_plans_api, "_service", lambda: service)
    session_manager.initialize(tmp_path)
    session_manager.create_session("owner-session")
    session_manager.create_session("other-session")
    plan = service.prepare(
        action="install",
        source="https://example.com/demo/",
        request_context={"session_id": "owner-session", "query_id": "query-1", "run_id": "run-1"},
    )
    decision = SkillPlanDecision(plan_sha256=plan["plan_sha256"])

    async def scenario() -> None:
        with pytest.raises(HTTPException) as foreign:
            await get_skill_plan("other-session", plan["plan_id"])
        assert foreign.value.status_code == 404

        first = await commit_skill_plan("owner-session", plan["plan_id"], decision)
        assert first["plan"]["status"] == "committed"
        assert first["permission_recorded"] is True
        assert first["idempotent"] is False
        assert (tmp_path / "skills" / "demo-skill" / "SKILL.md").is_file()

        second = await commit_skill_plan("owner-session", plan["plan_id"], decision)
        assert second["plan"]["status"] == "committed"
        assert second["permission_recorded"] is False
        assert second["idempotent"] is True

        permissions = await list_permissions("owner-session")
        assert permissions["grants"] == []
        assert len(permissions["history"]) == 1
        assert permissions["history"][0]["metadata"]["policy_source"] == "structured_skill_plan"
        assert permissions["history"][0]["metadata"]["change_preview"]["skill_name"] == "demo-skill"

    asyncio.run(scenario())

    replay = service.prepare(
        action="install",
        source="https://example.com/demo/",
        request_context={"session_id": "owner-session", "query_id": "query-1", "run_id": "run-1"},
    )
    assert replay["plan_id"] == plan["plan_id"]
    assert replay["status"] == "committed"


def test_structured_skill_plan_cancel_is_durable_and_never_installs(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)
    monkeypatch.setattr(service, "_stage_source", _stage_version("1.0.0"))
    monkeypatch.setattr(skill_plans_api, "_service", lambda: service)
    session_manager.initialize(tmp_path)
    session_manager.create_session("cancel-session")
    plan = service.prepare(
        action="install",
        source="https://example.com/demo/",
        request_context={"session_id": "cancel-session", "query_id": "query-1"},
    )
    decision = SkillPlanDecision(plan_sha256=plan["plan_sha256"])

    async def scenario() -> None:
        first = await cancel_skill_plan("cancel-session", plan["plan_id"], decision)
        second = await cancel_skill_plan("cancel-session", plan["plan_id"], decision)
        assert first["plan"]["status"] == "cancelled"
        assert second["plan"]["status"] == "cancelled"
        assert not (tmp_path / "skills" / "demo-skill").exists()
        with pytest.raises(HTTPException) as conflict:
            await commit_skill_plan("cancel-session", plan["plan_id"], decision)
        assert conflict.value.status_code == 409

    asyncio.run(scenario())


def test_failed_structured_commit_leaves_no_active_permission(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)
    monkeypatch.setattr(service, "_stage_source", _stage_version("1.0.0"))
    monkeypatch.setattr(skill_plans_api, "_service", lambda: service)
    session_manager.initialize(tmp_path)
    session_manager.create_session("failed-session")
    plan = service.prepare(
        action="install",
        source="https://example.com/demo/",
        request_context={"session_id": "failed-session", "query_id": "query-1"},
    )
    payload = service.plans_dir / plan["plan_id"] / "payload" / "SKILL.md"
    payload.write_text(payload.read_text(encoding="utf-8") + "tampered", encoding="utf-8")

    async def scenario() -> None:
        with pytest.raises(HTTPException) as failure:
            await commit_skill_plan(
                "failed-session",
                plan["plan_id"],
                SkillPlanDecision(plan_sha256=plan["plan_sha256"]),
            )
        assert failure.value.status_code == 409
        permissions = await list_permissions("failed-session")
        assert permissions["grants"] == []
        assert len(permissions["history"]) == 1
        assert service.preview_for_session(plan["plan_id"], "failed-session")["status"] == "prepared"

    asyncio.run(scenario())


def test_session_plan_cleanup_removes_payload_and_audit_record(tmp_path, monkeypatch):
    service = SkillManagementService(tmp_path)
    monkeypatch.setattr(service, "_stage_source", _stage_version("1.0.0"))
    plan = service.prepare(
        action="install",
        source="https://example.com/demo/",
        request_context={"session_id": "deleted-session", "query_id": "query-1"},
    )

    assert service.delete_session_plans("deleted-session") == 1
    assert service.preview(plan["plan_id"]) is None
    assert not (service.plans_dir / plan["plan_id"]).exists()
