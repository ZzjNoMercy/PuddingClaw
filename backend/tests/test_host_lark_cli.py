from __future__ import annotations

import io
import tarfile
from pathlib import Path

from runtime_identity.adapters import LarkManagedCliAdapter
from runtime_identity.host_lark_cli import HostLarkCliRuntime
from runtime_identity.paths import PuddingClawPaths


def _fake_lark(tmp_path: Path, monkeypatch, version: str = "1.2.3") -> Path:
    executable = tmp_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        """#!/bin/sh
if [ "$1" = "--version" ]; then
  echo "lark-cli version 1.2.3"
  exit 0
fi
mkdir -p "$LARKSUITE_CLI_CONFIG_DIR"
printf 'configured' > "$LARKSUITE_CLI_CONFIG_DIR/config.json"
printf '%s\n' "$LARKSUITE_CLI_CONFIG_DIR"
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PUDDINGCLAW_LARK_CLI_PATH", str(executable))
    monkeypatch.setenv(
        "PUDDINGCLAW_LARK_NATIVE_CREDENTIAL_DIR",
        str(tmp_path / "native-lark-credentials"),
    )
    return executable


def _legacy_archive() -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in (
            (".lark-cli/config.json", b"legacy-config"),
            (".lark-cli/.credential-data/lark-cli/master.key", b"k" * 32),
            (".lark-cli/.credential-data/lark-cli/token.enc", b"legacy-token"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def test_global_resolution_and_profile_environment_are_explicit(tmp_path, monkeypatch):
    executable = _fake_lark(tmp_path, monkeypatch)
    runtime = HostLarkCliRuntime(PuddingClawPaths(tmp_path / "home"))

    resolution = runtime.resolve()
    environment = runtime.environment("owner-a", "profile-a")

    assert resolution.executable == executable.resolve()
    assert resolution.version == "1.2.3"
    assert environment["LARKSUITE_CLI_CONFIG_DIR"].endswith(
        "/users/owner-a/integrations/lark-cli/profiles/profile-a/config"
    )
    assert "LARKSUITE_CLI_DATA_DIR" not in environment
    assert environment["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] == "1"
    assert environment["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] == "1"


def test_direct_execution_uses_native_dirs_without_creating_vault(tmp_path, monkeypatch):
    _fake_lark(tmp_path, monkeypatch)
    paths = PuddingClawPaths(tmp_path / "home")
    runtime = HostLarkCliRuntime(paths)
    resolution = runtime.resolve()
    assert resolution.executable is not None

    result = runtime.execute(
        executable=resolution.executable,
        workspace=tmp_path,
        argv=["lark-cli", "auth", "status", "--json"],
        environment={},
        owner_user_id="owner-a",
        profile_id="profile-a",
    )

    assert result.exit_code == 0
    assert str(paths.lark_cli_config_dir("owner-a", "profile-a")) in result.output
    assert paths.lark_cli_config_dir("owner-a", "profile-a").joinpath("config.json").is_file()
    assert not paths.provider_profile("owner-a", "lark", "profile-a").joinpath("vault.enc").exists()


def test_legacy_archive_is_migrated_once_and_retired_after_success(tmp_path, monkeypatch):
    _fake_lark(tmp_path, monkeypatch)
    paths = PuddingClawPaths(tmp_path / "home")
    runtime = HostLarkCliRuntime(paths)
    resolution = runtime.resolve()
    assert resolution.executable is not None
    legacy = paths.provider_profile("owner-a", "lark", "profile-a")
    legacy.mkdir(parents=True)
    legacy.joinpath("vault.enc").write_bytes(b"obsolete-encrypted-copy")
    legacy.joinpath("profile.json").write_text("{}", encoding="utf-8")
    spec = LarkManagedCliAdapter().credential_state

    result = runtime.execute(
        executable=resolution.executable,
        workspace=tmp_path,
        argv=["lark-cli", "auth", "status", "--json"],
        environment={},
        owner_user_id="owner-a",
        profile_id="profile-a",
        credential_state=_legacy_archive(),
        credential_state_spec=spec,
    )

    assert result.exit_code == 0
    native_credentials = tmp_path / "native-lark-credentials"
    assert native_credentials.joinpath("master.key.file").read_bytes() == b"k" * 32
    assert native_credentials.joinpath("token.enc").read_bytes() == b"legacy-token"
    assert not paths.lark_cli_profile_root("owner-a", "profile-a").joinpath("data").exists()
    assert not legacy.joinpath("vault.enc").exists()
    assert not legacy.joinpath("profile.json").exists()
    assert runtime.profile_state_present("owner-a", "profile-a")


def test_explicit_missing_binary_does_not_fall_back_to_another_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_LARK_CLI_PATH", str(tmp_path / "missing" / "lark-cli"))

    resolution = HostLarkCliRuntime(PuddingClawPaths(tmp_path / "home")).resolve()

    assert resolution.executable is None
    assert resolution.version is None


def test_linux_migration_accepts_the_old_private_home_file_store(tmp_path, monkeypatch):
    _fake_lark(tmp_path, monkeypatch)
    monkeypatch.setattr("runtime_identity.host_lark_cli.sys.platform", "linux")
    paths = PuddingClawPaths(tmp_path / "home")
    runtime = HostLarkCliRuntime(paths)

    migrated = runtime.migrate_legacy_archive_once(
        "owner-a",
        "profile-a",
        _legacy_archive(),
        LarkManagedCliAdapter().credential_state,
    )

    native_credentials = tmp_path / "native-lark-credentials"
    assert migrated is True
    assert native_credentials.joinpath("master.key").read_bytes() == b"k" * 32
    assert native_credentials.joinpath("token.enc").read_bytes() == b"legacy-token"
    assert not paths.lark_cli_profile_root("owner-a", "profile-a").joinpath("data").exists()


def test_windows_migration_never_discards_an_incompatible_legacy_archive(tmp_path, monkeypatch):
    _fake_lark(tmp_path, monkeypatch)
    monkeypatch.setattr("runtime_identity.host_lark_cli.sys.platform", "win32")
    paths = PuddingClawPaths(tmp_path / "home")
    runtime = HostLarkCliRuntime(paths)

    try:
        runtime.migrate_legacy_archive_once(
            "owner-a",
            "profile-a",
            _legacy_archive(),
            LarkManagedCliAdapter().credential_state,
        )
    except ValueError as exc:
        assert "authorize it again" in str(exc)
    else:
        raise AssertionError("incompatible Windows migration must fail closed")

    profile_root = paths.lark_cli_profile_root("owner-a", "profile-a")
    assert not profile_root.joinpath(".legacy-vault-migrated").exists()
    assert not (tmp_path / "native-lark-credentials").exists()


def test_managed_service_uses_global_cli_without_profile_vault(tmp_path, monkeypatch):
    _fake_lark(tmp_path, monkeypatch)
    paths = PuddingClawPaths(tmp_path / "home")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    from harness.host_skill_runtime import HostSkillRuntimeBackend
    from runtime_identity.composition import ManagedIntegrationBackend
    from runtime_identity.service import ManagedCliService

    backend = ManagedIntegrationBackend(HostSkillRuntimeBackend(paths), workspace)
    service = ManagedCliService(backend, paths=paths)
    plan = service.plan_command("lark-cli auth status --json", {})
    assert plan is not None
    preview = __import__("json").loads(plan.approval_preview())
    assert preview["runtime"] == "host"
    assert preview["version"] == "1.2.3"
    assert "toolchain_revision" not in preview
    assert "runtime_image_digest" not in preview

    result = service.execute(plan, {})

    assert result.exit_code == 0
    assert plan.toolchain_revision == "host-global:1.2.3"
    assert plan.executable_path is not None
    assert not paths.provider_profile("local", "lark", "lark_default").joinpath("vault.enc").exists()
    assert not (paths.root / ".vault-keys" / "local.key").exists()


def test_profileless_local_inspection_uses_a_non_secret_default_runtime_dir(tmp_path, monkeypatch):
    _fake_lark(tmp_path, monkeypatch)
    paths = PuddingClawPaths(tmp_path / "home")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    from harness.host_skill_runtime import HostSkillRuntimeBackend
    from runtime_identity.composition import ManagedIntegrationBackend
    from runtime_identity.service import ManagedCliService

    backend = ManagedIntegrationBackend(HostSkillRuntimeBackend(paths), workspace)
    service = ManagedCliService(backend, paths=paths)
    plan = service.plan_command("lark-cli --version", {})
    assert plan is not None
    assert plan.profile_id is None

    result = service.execute(plan, {})

    assert result.exit_code == 0
    assert result.payload["output"]
    assert paths.lark_cli_config_dir("local", "lark_default").is_dir()
    assert not list(paths.root.rglob("vault.enc"))


def test_host_global_install_plan_has_no_toolchain_or_runtime_image_contract(tmp_path, monkeypatch):
    _fake_lark(tmp_path, monkeypatch)
    npm = tmp_path / "bin" / "npm"
    npm.write_text(
        "#!/bin/sh\nprintf '%s\\n' '{\"name\":\"@larksuite/cli\",\"version\":\"1.2.4\","
        "\"dist.integrity\":\"sha512-YWJj\"}'\n",
        encoding="utf-8",
    )
    npm.chmod(0o755)
    original_which = __import__("shutil").which
    monkeypatch.setattr(
        "runtime_identity.host_lark_cli.shutil.which",
        lambda name: str(npm) if name == "npm" else original_which(name),
    )
    paths = PuddingClawPaths(tmp_path / "home")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    from harness.host_skill_runtime import HostSkillRuntimeBackend
    from runtime_identity.composition import ManagedIntegrationBackend
    from runtime_identity.service import ManagedCliService

    service = ManagedCliService(
        ManagedIntegrationBackend(HostSkillRuntimeBackend(paths), workspace),
        paths=paths,
    )
    plan = service.plan_command("npm install --global @larksuite/cli", {})
    assert plan is not None
    preview = __import__("json").loads(plan.approval_preview())

    assert plan.runtime_image_digest == ""
    assert preview["installation_scope"] == "host_user_global"
    assert preview["current_version"] == "1.2.3"
    assert preview["resolved_version"] == "1.2.4"
    assert "toolchain_revision" not in preview
    assert "runtime_image_digest" not in preview
