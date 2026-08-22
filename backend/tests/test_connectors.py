from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from runtime_identity.adapters import (
    CredentialStateSpec,
    ManagedCliRegistry,
    ManagedConnectorSpec,
    ToolchainPackageSpec,
)
from runtime_identity.authorization import (
    LARK_APP_CONFIGURATION_PHASE,
    LARK_USER_REAUTHORIZATION_PHASE,
    AuthorizationFlowStore,
)
from runtime_identity.authorization_drivers import AuthorizationDriverRegistry
from runtime_identity.connectors import ConnectorRegistry
from runtime_identity.paths import PuddingClawPaths
from runtime_identity.profiles import CredentialProfileStore
from runtime_identity.toolchains import ToolchainManager

_TEST_IMAGE_DIGEST = "sha256:" + "1" * 64
_TEST_INTEGRITY = "sha512-YWJj"


class _FakeInstaller:
    @staticmethod
    def managed_runtime_image_digest():
        return _TEST_IMAGE_DIGEST

    @staticmethod
    def resolve_shared_node_runtime(
        *,
        dependencies,
        expected_runtime_image_digest,
        resolution_path,
    ):
        assert expected_runtime_image_digest == _TEST_IMAGE_DIGEST
        dependencies = dict(sorted(dependencies.items()))
        (resolution_path / "package.json").write_text(
            json.dumps(
                {
                    "name": "puddingclaw-managed-runtime",
                    "private": True,
                    "version": "0.0.0",
                    "dependencies": dependencies,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (resolution_path / "package-lock.json").write_text(
            json.dumps(
                {
                    "lockfileVersion": 3,
                    "packages": {
                        "": {"dependencies": dependencies},
                        **{
                            f"node_modules/{package}": {
                                "name": package,
                                "version": version,
                                "integrity": _TEST_INTEGRITY,
                                "resolved": f"https://registry.npmjs.org/{package}/-/{version}.tgz",
                            }
                            for package, version in dependencies.items()
                        },
                    },
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return SimpleNamespace(output="resolved", exit_code=0)

    @staticmethod
    def build_shared_node_runtime(
        *,
        expected_runtime_image_digest,
        runtime_path,
        container_path,
    ):
        assert expected_runtime_image_digest == _TEST_IMAGE_DIGEST
        desired = json.loads((runtime_path / "desired-packages.json").read_text(encoding="utf-8"))
        dependencies = json.loads((runtime_path / "package.json").read_text(encoding="utf-8"))["dependencies"]
        for package, version in dependencies.items():
            package_root = runtime_path / "node_modules" / package
            package_root.mkdir(parents=True, exist_ok=True)
            bins = desired["packages"][package]["declared_bins"]
            package_root.joinpath("package.json").write_text(
                json.dumps(
                    {
                        "name": package,
                        "version": version,
                        "bin": {executable: "cli.js" for executable in bins},
                    }
                ),
                encoding="utf-8",
            )
            cli = package_root / "cli.js"
            cli.write_text(f"binary-{version}", encoding="utf-8")
            cli.chmod(0o755)
        return SimpleNamespace(output="built", exit_code=0, truncated=False)

    def install_managed_node_cli(
        self,
        *,
        distribution,
        package,
        executable,
        toolchain_path,
        **_kwargs,
    ):
        version = distribution.rsplit("@", 1)[-1]
        installed = toolchain_path / "bin" / executable
        installed.write_text(f"binary-{version}", encoding="utf-8")
        installed.chmod(0o755)
        package_root = toolchain_path / "lib" / "node_modules" / package
        package_root.mkdir(parents=True, exist_ok=True)
        (package_root / "package.json").write_text(
            json.dumps({"name": package, "version": version}),
            encoding="utf-8",
        )
        (toolchain_path / "lib" / "node_modules" / ".package-lock.json").write_text(
            json.dumps(
                {
                    "packages": {
                        f"node_modules/{package}": {
                            "name": package,
                            "version": version,
                            "integrity": _TEST_INTEGRITY,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(output=version, exit_code=0, truncated=False)


def _install_adapter(
    paths: PuddingClawPaths,
    registry: ManagedCliRegistry,
    adapter_id: str,
    version: str,
    runtime_contract: str = "test",
) -> None:
    adapter = registry.adapter(adapter_id)
    fingerprint = registry.adapter_contract_fingerprint(adapter_id)
    driver = AuthorizationDriverRegistry().for_adapter(adapter_id, required=False)
    if driver is not None:
        fingerprint = hashlib.sha256(f"{fingerprint}\0{driver.contract_fingerprint}".encode()).hexdigest()
    manager = ToolchainManager(paths, runtime_contract)
    current = manager.resolve_node(adapter_id).host_path.name
    result = manager.install_package(
        _FakeInstaller(),
        adapter_id=adapter_id,
        spec=adapter.toolchain_package,
        distribution=f"{adapter.toolchain_package.package}@{version}",
        expected_integrity=_TEST_INTEGRITY,
        runtime_image_digest=_TEST_IMAGE_DIGEST,
        adapter_contract_fingerprint=fingerprint,
        credential_state_fingerprint=adapter.credential_state.fingerprint,
        expected_revision=current,
    )
    assert result.exit_code == 0


def _install_fake_lark(paths: PuddingClawPaths, runtime_contract: str = "test") -> None:
    _install_adapter(paths, ManagedCliRegistry(), "lark-cli", "1.0.78", runtime_contract)


def _global_lark(tmp_path, monkeypatch, version: str = "1.0.78"):
    executable = tmp_path / "global-bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text(f"#!/bin/sh\necho 'lark-cli version {version}'\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PUDDINGCLAW_LARK_CLI_PATH", str(executable))
    return executable


def test_application_registers_connector_routes():
    from app import app

    paths = app.openapi()["paths"]
    assert "/api/connectors" in paths
    assert "/api/connectors/{connector_id}" in paths
    assert "/api/connectors/{connector_id}/authorize" in paths
    assert "/api/connectors/{connector_id}/resume" in paths
    assert "/api/connectors/{connector_id}/revoke" in paths
    assert "/api/toolchains/{adapter_id}/revisions" in paths
    assert "/api/toolchains/{adapter_id}/rollback/preview" in paths
    assert "/api/toolchains/{adapter_id}/rollback/commit" in paths


def test_connector_catalog_does_not_create_profile_when_only_viewed(tmp_path, monkeypatch):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    executable = _global_lark(tmp_path, monkeypatch)

    connector = ConnectorRegistry(
        paths,
        "test",
        owner_user_id="local",
        runtime_image_digest=_TEST_IMAGE_DIGEST,
    ).get("lark")

    assert connector["status"] == "unconfigured"
    assert connector["environment"]["health"] == "available"
    assert connector["environment"]["version"] == "1.0.78"
    assert connector["environment"]["availability_scope"] == "all_projects"
    assert connector["environment"]["executable"] == str(executable.resolve())
    assert connector["environment"]["state_model"] == "provider_native_profile_dirs"
    assert connector["driver_kind"] == "managed_cli"
    assert connector["profile"] is None
    assert CredentialProfileStore(paths, "local").resolve("lark", create_default=False) is None


def test_installed_adapter_is_automatically_projected_into_connector_catalog(tmp_path):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")

    class FixtureAdapter:
        adapter_id = "fixture-cli"
        provider = "fixture"
        executables = frozenset({"fixture-cli"})
        toolchain_package = ToolchainPackageSpec(
            ecosystem="node",
            package="@fixture/cli",
            executable="fixture-cli",
        )
        credential_state = CredentialStateSpec(paths=(".fixture-cli",))
        connector = ManagedConnectorSpec(
            connector_id="fixture",
            display_name="Fixture Cloud",
            description="Fixture managed CLI connector",
            capabilities=("记录", "查询"),
            skill_prefix="fixture-",
        )

        def claims(self, command):
            return "fixture-cli" in command

        def parse(self, _tokens, _env):
            return None

    registry = ManagedCliRegistry((FixtureAdapter(),))
    _install_adapter(paths, registry, "fixture-cli", "2.4.0")

    catalog = ConnectorRegistry(
        paths,
        "test",
        owner_user_id="local",
        managed_registry=registry,
        runtime_image_digest=_TEST_IMAGE_DIGEST,
    ).list()

    assert [item["connector_id"] for item in catalog] == ["fixture", "kimi-webbridge"]
    fixture = catalog[0]
    assert fixture["status"] == "unconfigured"
    assert fixture["environment"]["health"] == "available"
    assert fixture["environment"]["version"] == "2.4.0"
    assert fixture["capabilities"] == ["记录", "查询"]


def test_connector_counts_effective_home_skills(tmp_path):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    skill = paths.user_skills() / "fixture-records"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: fixture-records\ndescription: fixture\n---\n",
        encoding="utf-8",
    )

    class FixtureAdapter:
        adapter_id = "fixture-cli"
        provider = "fixture"
        executables = frozenset({"fixture-cli"})
        toolchain_package = ToolchainPackageSpec(
            ecosystem="node",
            package="@fixture/cli",
            executable="fixture-cli",
        )
        credential_state = CredentialStateSpec(paths=(".fixture-cli",))
        connector = ManagedConnectorSpec(
            connector_id="fixture",
            display_name="Fixture Cloud",
            description="Fixture managed CLI connector",
            capabilities=("记录",),
            skill_prefix="fixture-",
        )

        def claims(self, _command):
            return False

        def parse(self, _tokens, _env):
            return None

    registry = ManagedCliRegistry((FixtureAdapter(),))
    connector = ConnectorRegistry(paths, "test", managed_registry=registry)

    assert connector._installed_skill_count(connector.definitions["fixture"]) == 1


def test_connector_catalog_ignores_obsolete_per_adapter_release(tmp_path, monkeypatch):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    _global_lark(tmp_path, monkeypatch)
    root = paths.root / "runtime" / "toolchains" / "node" / "obsolete" / "adapters" / "lark-cli"
    release = root / "releases" / "release-forged"
    executable = release / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True)
    executable.write_text("not-a-verified-release", encoding="utf-8")
    executable.chmod(0o755)
    (release / "toolchain-manifest.json").write_text(
        json.dumps({"version": 3, "adapter_id": "lark-cli"}),
        encoding="utf-8",
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / "current").symlink_to(release)

    connector = ConnectorRegistry(paths, "test", owner_user_id="local").get("lark")

    assert connector["status"] == "unconfigured"
    assert connector["environment"]["health"] == "available"
    assert connector["environment"]["version"] == "1.0.78"


def test_connector_catalog_treats_empty_toolchain_as_not_installed(tmp_path, monkeypatch):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    missing = tmp_path / "missing" / "lark-cli"
    monkeypatch.setenv("PUDDINGCLAW_LARK_CLI_PATH", str(missing))
    ToolchainManager(paths, "test").resolve_node("lark-cli")

    connector = ConnectorRegistry(
        paths,
        "test",
        owner_user_id="local",
        runtime_image_digest=_TEST_IMAGE_DIGEST,
    ).get("lark")

    assert connector["status"] == "environment_unavailable"
    assert connector["environment"]["health"] == "unavailable"


def test_connector_catalog_ignores_managed_runtime_image_for_host_cli(tmp_path, monkeypatch):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    _global_lark(tmp_path, monkeypatch)

    connector = ConnectorRegistry(
        paths,
        "test",
        owner_user_id="local",
        runtime_image_digest="sha256:" + "2" * 64,
    ).get("lark")

    assert connector["status"] == "unconfigured"
    assert connector["environment"]["health"] == "available"


def test_connector_catalog_rejects_cross_driver_connector_id_collision(tmp_path):
    class ConflictingAdapter:
        adapter_id = "conflicting-cli"
        provider = "conflicting"
        executables = frozenset({"conflicting-cli"})
        toolchain_package = ToolchainPackageSpec(
            ecosystem="node",
            package="@fixture/conflicting",
            executable="conflicting-cli",
        )
        credential_state = CredentialStateSpec(paths=(".conflicting",))
        connector = ManagedConnectorSpec(
            connector_id="kimi-webbridge",
            display_name="Collision",
            description="Must fail closed",
        )

        def claims(self, _command):
            return False

        def parse(self, _tokens, _env):
            return None

    with pytest.raises(ValueError, match="conflicts"):
        ConnectorRegistry(
            PuddingClawPaths(tmp_path / ".puddingclaw"),
            "test",
            managed_registry=ManagedCliRegistry((ConflictingAdapter(),)),
        )


def test_connector_projects_profile_and_authorization_flow_without_secrets(tmp_path):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    _install_fake_lark(paths)
    store = CredentialProfileStore(paths, "local")
    profile = store.resolve("lark")
    store.update_status(profile["profile_id"], "active")
    store.update_identity_status(profile["profile_id"], "bot", "ready")
    store.update_identity_status(profile["profile_id"], "user", "active")
    profile = store.resolve("lark")
    flow_store = AuthorizationFlowStore(paths, "local", vault=store.vault)
    flow = flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=profile["profile_id"],
        purpose="lark_user_reauthorization",
        phase=LARK_APP_CONFIGURATION_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision="old-vault",
        adapter_contract_fingerprint="a" * 64,
        public={},
        secret=None,
        expires_at=None,
    )
    flow_store.write_staged_state(flow, b"verified-app-staging")
    flow_store.mark_phase_verified("lark", profile["profile_id"], LARK_APP_CONFIGURATION_PHASE.phase_id)
    flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=profile["profile_id"],
        purpose="lark_user_reauthorization",
        phase=LARK_USER_REAUTHORIZATION_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision="old-vault",
        adapter_contract_fingerprint="a" * 64,
        public={
            "verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify",
            "user_code": "SAFE-CODE",
        },
        secret={"device_code": "DO-NOT-EXPOSE"},
        expires_at=9999999999,
    )

    connector = ConnectorRegistry(
        paths,
        "test",
        owner_user_id="local",
        runtime_image_digest=_TEST_IMAGE_DIGEST,
    ).get("lark")

    assert connector["status"] == "authorizing"
    assert connector["profile"]["health"] == "active"
    assert connector["profile"]["app_identity"]["status"] == "ready"
    assert connector["profile"]["user_identity"]["status"] == "active"
    assert connector["active_flow"]["flow_id"] == flow["flow_id"]
    assert connector["active_flow"]["phase"]["total"] == 1
    assert connector["active_flow"]["completed_phase_ids"] == ["app_configuration"]
    serialized = json.dumps(connector)
    assert "DO-NOT-EXPOSE" not in serialized
    assert "/home/puddingclaw" not in serialized


def test_connector_status_does_not_treat_orphaned_flow_as_authorizing(tmp_path):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    _install_fake_lark(paths)
    store = CredentialProfileStore(paths, "local")
    profile = store.resolve("lark")
    store.update_status(profile["profile_id"], "active")
    store.update_identity_status(profile["profile_id"], "bot", "ready", verified=True)
    store.update_identity_status(
        profile["profile_id"],
        "user",
        "active",
        verified=True,
        token_status="valid",
    )
    profile = store.resolve("lark")
    flow_store = AuthorizationFlowStore(paths, "local", vault=store.vault)
    flow = flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=profile["profile_id"],
        purpose="lark_full_authorization",
        phase=LARK_APP_CONFIGURATION_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision="old-vault",
        adapter_contract_fingerprint="a" * 64,
        public={"verification_url": "https://open.feishu.cn/page/cli?user_code=ORPHAN"},
        secret=None,
        expires_at=None,
    )

    connector = ConnectorRegistry(
        paths,
        "test",
        owner_user_id="local",
        runtime_image_digest=_TEST_IMAGE_DIGEST,
    ).get("lark")

    assert connector["status"] == "connected"
    assert connector["profile"]["app_identity"]["verified"] is True
    assert connector["profile"]["user_identity"]["token_status"] == "valid"
    assert connector["active_flow"] is None
    record = next(item for item in flow_store._read_registry()["flows"] if item["flow_id"] == flow["flow_id"])
    assert record["status"] == "expired"
    assert record["error"] == "browser_job_missing"
