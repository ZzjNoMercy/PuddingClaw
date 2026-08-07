from __future__ import annotations

import json

from runtime_identity.authorization import (
    LARK_APP_CONFIGURATION_PHASE,
    LARK_USER_REAUTHORIZATION_PHASE,
    AuthorizationFlowStore,
)
from runtime_identity.connectors import ConnectorRegistry
from runtime_identity.paths import PuddingClawPaths
from runtime_identity.profiles import CredentialProfileStore


def _install_fake_lark(paths: PuddingClawPaths, runtime_contract: str = "test") -> None:
    root = paths.node_toolchain(runtime_contract)
    release = root / "releases" / "release-test"
    executable = release / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True)
    executable.write_text("test", encoding="utf-8")
    package = release / "lib" / "node_modules" / "@larksuite" / "cli" / "package.json"
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({"version": "1.0.78"}), encoding="utf-8")
    current = root / "current"
    current.symlink_to(release)


def test_application_registers_connector_routes():
    from app import app

    paths = app.openapi()["paths"]
    assert "/api/connectors" in paths
    assert "/api/connectors/{connector_id}" in paths
    assert "/api/connectors/{connector_id}/authorize" in paths
    assert "/api/connectors/{connector_id}/resume" in paths
    assert "/api/connectors/{connector_id}/revoke" in paths


def test_connector_catalog_does_not_create_profile_when_only_viewed(tmp_path):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    _install_fake_lark(paths)

    connector = ConnectorRegistry(paths, "test", owner_user_id="local").get("lark")

    assert connector["status"] == "unconfigured"
    assert connector["environment"]["health"] == "available"
    assert connector["environment"]["version"] == "1.0.78"
    assert connector["environment"]["availability_scope"] == "all_projects"
    assert connector["driver_kind"] == "managed_cli"
    assert connector["profile"] is None
    assert CredentialProfileStore(paths, "local").resolve("lark", create_default=False) is None


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

    connector = ConnectorRegistry(paths, "test", owner_user_id="local").get("lark")

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

    connector = ConnectorRegistry(paths, "test", owner_user_id="local").get("lark")

    assert connector["status"] == "connected"
    assert connector["profile"]["app_identity"]["verified"] is True
    assert connector["profile"]["user_identity"]["token_status"] == "valid"
    assert connector["active_flow"] is None
    record = next(
        item for item in flow_store._read_registry()["flows"] if item["flow_id"] == flow["flow_id"]
    )
    assert record["status"] == "expired"
    assert record["error"] == "browser_job_missing"
