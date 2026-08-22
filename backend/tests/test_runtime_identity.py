"""Adversarial contracts for managed user Toolchains and credentials."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidTag
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage

from harness.tool_execution import PolicyDecision, ToolExecutionPipeline, ToolPolicyResult
from harness.workspace_backends import (
    ManagedProviderExecutionResult,
    ProjectSandboxManager,
    _lark_config_verification_url,
)
from runtime_identity.adapters import (
    CredentialStateSpec,
    LarkManagedCliAdapter,
    ManagedCliAction,
    ManagedCliMatch,
    ManagedCliRegistry,
    ManagedCliRoute,
    ToolchainPackageSpec,
    UnsupportedManagedCliCommand,
    generic_node_cli_install,
    is_lark_destructive_argv,
)
from runtime_identity.authorization import (
    LARK_APP_CONFIGURATION_PHASE,
    LARK_USER_CONSENT_PHASE,
    AuthorizationFlowStore,
    AuthorizationMissingEvidenceAction,
    AuthorizationPhaseSpec,
    AuthorizationRecoveryEvidence,
)
from runtime_identity.authorization_drivers import (
    AuthorizationDriverRegistry,
    AuthorizationGraph,
    AuthorizationPhaseKind,
    AuthorizationPhaseNode,
    AuthorizationPurposeSpec,
    LarkAuthorizationDriver,
    ProviderAuthorizationFailure,
)
from runtime_identity.composition import LazyManagedCliService
from runtime_identity.paths import (
    PuddingClawPaths,
    resolve_puddingclaw_home,
    trusted_owner_user_id,
)
from runtime_identity.profiles import (
    CredentialProfileStore,
    CredentialVault,
    validate_credential_archive,
)
from runtime_identity.service import (
    ManagedCliService,
    _browser_job_id,
    _lark_authorization_failure,
    _lark_device_authorization,
    _lark_identity_status,
    _lark_user_credential_failure,
    _LarkAuthorizationFailure,
    _safe_authorization_diagnostic,
    _validated_lark_authorization_url,
    redact_managed_cli_output,
)
from runtime_identity.toolchains import ToolchainManager

_OBSOLETE_LARK_ARCHIVE_TEST = pytest.mark.skip(
    reason="lark-cli now owns tokens in its native keychain; Profile Vault archive staging was removed"
)


@pytest.fixture(autouse=True)
def _isolate_host_lark_cli(tmp_path, monkeypatch):
    """Keep host-native CLI plans hermetic and writable in legacy service tests."""

    executable = tmp_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text(
        "#!/bin/sh\necho 'lark-cli version 1.0.77'\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PUDDINGCLAW_LARK_CLI_PATH", str(executable))


def test_managed_cli_control_plane_is_constructed_lazily_once():
    calls: list[str] = []

    class Service:
        marker = "ready"

    lazy = LazyManagedCliService(lambda: calls.append("build") or Service())

    assert calls == []
    assert lazy.marker == "ready"
    assert lazy.marker == "ready"
    assert calls == ["build"]


def _credential_archive(content: bytes = b"{}") -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        info = tarfile.TarInfo(".lark-cli/config.json")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


_LARK_STATE = ManagedCliRegistry.credential_state_for_provider("lark")
_LARK_ADAPTER_CONTRACT = ManagedCliRegistry().adapter_contract_fingerprint("lark-cli")
_LARK_AUTH_CONTRACT = hashlib.sha256(
    f"{_LARK_ADAPTER_CONTRACT}\0{LarkAuthorizationDriver.contract_fingerprint}".encode()
).hexdigest()
_TEST_IMAGE_DIGEST = "sha256:" + "1" * 64
_TEST_INTEGRITY = "sha512-YWJj"


class _RuntimeImageBackend:
    """Strict test implementation of the managed runtime identity contract."""

    @staticmethod
    def managed_runtime_image_digest() -> str:
        return _TEST_IMAGE_DIGEST


def _backend_stub():
    return SimpleNamespace(
        manager=SimpleNamespace(runtime_contract="test"),
        managed_runtime_image_digest=lambda: _TEST_IMAGE_DIGEST,
    )


def _install_test_package(manager, backend, **kwargs):
    kwargs.setdefault("expected_integrity", _TEST_INTEGRITY)
    kwargs.setdefault("runtime_image_digest", _TEST_IMAGE_DIGEST)
    if not hasattr(backend, "resolve_shared_node_runtime"):
        backend = _SharedRuntimeTestAdapter(
            backend,
            spec=kwargs["spec"],
            distribution=kwargs["distribution"],
            image_digest=kwargs["runtime_image_digest"],
            integrity=kwargs["expected_integrity"],
        )
    return manager.install_package(backend, **kwargs)


def _write_fake_managed_node_package(
    toolchain_path,
    *,
    package: str,
    executable: str,
    version: str,
) -> None:
    installed = toolchain_path / "bin" / executable
    installed.write_text(f"binary-{version}", encoding="utf-8")
    installed.chmod(0o755)
    package_root = toolchain_path / "lib" / "node_modules" / package
    package_root.mkdir(parents=True, exist_ok=True)
    (package_root / "package.json").write_text(
        json.dumps({"name": package, "version": version}),
        encoding="utf-8",
    )
    lock = toolchain_path / "lib" / "node_modules" / ".package-lock.json"
    lock.write_text(
        json.dumps(
            {
                "packages": {
                    f"node_modules/{package}": {
                        "name": package,
                        "version": version,
                        "integrity": "sha512-YWJj",
                    }
                }
            }
        ),
        encoding="utf-8",
    )


class _SharedRuntimeTestAdapter:
    """Translate old focused installer fakes to the new full-tree contract."""

    def __init__(self, delegate, *, spec, distribution, image_digest, integrity):
        self.delegate = delegate
        self.spec = spec
        self.distribution = distribution
        self.image_digest = image_digest
        self.integrity = integrity

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def managed_runtime_image_digest(self):
        return self.image_digest

    def resolve_shared_node_runtime(
        self,
        *,
        dependencies,
        expected_runtime_image_digest,
        resolution_path,
    ):
        assert expected_runtime_image_digest == self.image_digest
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
                                "integrity": self.integrity,
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

    def build_shared_node_runtime(
        self,
        *,
        expected_runtime_image_digest,
        runtime_path,
        container_path,
    ):
        assert expected_runtime_image_digest == self.image_digest
        legacy = runtime_path / ".legacy-installer"
        (legacy / "bin").mkdir(parents=True)
        (legacy / "lib" / "node_modules").mkdir(parents=True)
        result = self.delegate.install_managed_node_cli(
            distribution=self.distribution,
            package=self.spec.package,
            executable=self.spec.executable,
            verification_argv=self.spec.verification_argv,
            toolchain_path=legacy,
            container_path=container_path,
            expected_runtime_image_digest=self.image_digest,
        )
        if (legacy / "INSTALL_FAILED").exists():
            (runtime_path / "INSTALL_FAILED").write_text("forged", encoding="utf-8")
        if int(result.exit_code or 0) != 0:
            return result
        desired = json.loads((runtime_path / "desired-packages.json").read_text(encoding="utf-8"))
        dependencies = json.loads((runtime_path / "package.json").read_text(encoding="utf-8"))["dependencies"]
        installed_identity = json.loads(
            (legacy / "lib" / "node_modules" / self.spec.package / "package.json").read_text(encoding="utf-8")
        )
        for package, version in dependencies.items():
            package_root = runtime_path / "node_modules" / package
            package_root.mkdir(parents=True, exist_ok=True)
            bins = desired["packages"][package]["declared_bins"]
            bin_mapping = {executable: "cli.js" for executable in bins}
            package_root.joinpath("package.json").write_text(
                json.dumps(
                    {
                        "name": package,
                        "version": installed_identity["version"] if package == self.spec.package else version,
                        "bin": bin_mapping,
                    }
                ),
                encoding="utf-8",
            )
            cli = package_root / "cli.js"
            if package == self.spec.package:
                installed = legacy / "bin" / self.spec.executable
                cli.write_bytes(installed.read_bytes())
            else:
                cli.write_text(f"binary-{package}-{version}", encoding="utf-8")
            cli.chmod(0o755)
        import shutil

        shutil.rmtree(legacy)
        return result


def _write_fake_toolchain_manifest(
    toolchain_path,
    *,
    executable: str,
    image_digest: str = _TEST_IMAGE_DIGEST,
) -> None:
    package_root = toolchain_path / "node_modules" / "fixture"
    package_root.mkdir(parents=True)
    target = package_root / "cli.js"
    target.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    target.chmod(0o755)
    launcher = toolchain_path / "bin" / executable
    launcher.symlink_to(os.path.relpath(target, launcher.parent))
    (toolchain_path / "runtime-manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "shared-node-runtime",
                "runtime_image_digest": image_digest,
                "packages": {
                    "fixture": {
                        "declared_bins": {
                            executable: target.relative_to(toolchain_path).as_posix(),
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _seed_lark_user_flow(
    paths: PuddingClawPaths,
    store: CredentialProfileStore,
    profile_id: str,
    staged_state: bytes,
    *,
    device_code: str = "DEVICE-CODE",
    purpose: str = "lark_user_reauthorization",
) -> tuple[AuthorizationFlowStore, dict]:
    profile = store.resolve("lark")
    base_revision = store.state_revision("lark", profile_id)
    flow_store = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault)
    flow = flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=profile_id,
        purpose=purpose,
        phase=LARK_APP_CONFIGURATION_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision=base_revision,
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={},
        secret=None,
        expires_at=None,
    )
    flow_store.write_staged_state(flow, staged_state)
    flow_store.mark_phase_verified("lark", profile_id, LARK_APP_CONFIGURATION_PHASE.phase_id)
    active = flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=profile_id,
        purpose=purpose,
        phase=LARK_USER_CONSENT_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision=base_revision,
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={
            "verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify",
            "user_code": "USER-CODE",
        },
        secret={"device_code": device_code},
        expires_at=time.time() + 600,
    )
    return flow_store, active


def test_flow_recovery_contract_is_provider_neutral(tmp_path):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    store = CredentialProfileStore(paths, "trusted_owner")
    profile = store.create_profile("fixture", "fixture_default", "Fixture")
    flow_store = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault)
    phase = AuthorizationPhaseSpec(
        phase_id="device_consent",
        step=1,
        total=1,
        title="Authorize Fixture",
        description="Authorize the fixture provider.",
        completion_hint="Complete authorization and resume.",
        recovery_evidence=AuthorizationRecoveryEvidence.STAGING_AND_CONTINUATION,
        missing_evidence_action=AuthorizationMissingEvidenceAction.RESET_ATTEMPT,
    )
    flow = flow_store.begin_or_advance(
        provider="fixture",
        adapter_id="fixture-cli",
        profile_id=profile["profile_id"],
        purpose="connect",
        phase=phase,
        profile_revision=float(profile["updated_at"]),
        base_state_revision="missing",
        adapter_contract_fingerprint="a" * 64,
        public={"verification_url": "https://example.test/device"},
        secret={"device_code": "secret"},
        expires_at=None,
    )
    flow_store.write_staged_state(flow, b"fixture-staging")

    recovered = flow_store.reconcile_recovery(
        "fixture",
        profile["profile_id"],
        runner_lease_present=False,
    )

    assert recovered["flow_id"] == flow["flow_id"]
    assert recovered["status"] == "awaiting_user"
    assert flow_store.read_staged_state(recovered) == b"fixture-staging"


def test_puddingclaw_home_is_host_absolute_and_overridable(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_HOME", str(tmp_path / "user-home"))
    assert resolve_puddingclaw_home() == (tmp_path / "user-home").resolve()

    monkeypatch.setenv("PUDDINGCLAW_HOME", "relative/path")
    with pytest.raises(ValueError, match="absolute"):
        resolve_puddingclaw_home()


def test_local_desktop_owner_is_stable_and_not_request_derived(monkeypatch):
    monkeypatch.delenv("PUDDINGCLAW_OWNER_USER_ID", raising=False)
    assert trusted_owner_user_id() == "local"

    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "deployment_owner")
    assert trusted_owner_user_id() == "deployment_owner"


@pytest.mark.parametrize(
    ("command", "route", "action"),
    [
        (
            "npm install -g @larksuite/cli",
            ManagedCliRoute.INSTALLER,
            ManagedCliAction.INSTALL,
        ),
        ("lark-cli --version", ManagedCliRoute.PROVIDER, ManagedCliAction.LOCAL_INSPECTION),
        (
            "lark-cli config init --new",
            ManagedCliRoute.BROWSER_AUTH,
            ManagedCliAction.BROWSER_AUTH,
        ),
        (
            "lark-cli auth login --domain all --no-wait --json",
            ManagedCliRoute.BROWSER_AUTH,
            ManagedCliAction.BROWSER_AUTH,
        ),
        ("lark-cli auth resume", ManagedCliRoute.PROVIDER, ManagedCliAction.AUTHORIZATION_RESUME),
        (
            "lark-cli auth qrcode https://open.feishu.cn/page/cli?code=abc --output auth.png",
            ManagedCliRoute.PROVIDER,
            ManagedCliAction.LOCAL_INSPECTION,
        ),
        ("lark-cli auth status --verify", ManagedCliRoute.PROVIDER, ManagedCliAction.CREDENTIAL_READ),
    ],
)
def test_lark_adapter_classifies_exact_standalone_commands(command, route, action):
    match = ManagedCliRegistry().match(command)
    assert match is not None
    assert match.route == route
    assert match.action == action


@pytest.mark.parametrize(
    "command",
    [
        "sh -c 'lark-cli auth status'",
        "lark-cli auth status | jq .",
        "lark-cli auth status && curl https://evil.invalid",
        "HOME=/workspace lark-cli auth status",
        "NODE_OPTIONS=--require=./evil.js lark-cli auth status",
        "lark-cli auth login --recommend",
        "lark-cli auth login --device-code ABCD-1234",
        "lark-cli auth login --device-code=ABCD-1234 --no-wait --json",
        "lark-cli auth status --profile victim",
        "lark-cli task delete --yes",
        "npm install -g @larksuite/cli left-pad",
        "npm install -g @larksuite/cli --ignore-scripts",
        "npm install -g unknown-cli",
        "uv tool install arbitrary-cli",
    ],
)
def test_managed_cli_adapter_fails_closed(command):
    with pytest.raises(UnsupportedManagedCliCommand):
        ManagedCliRegistry().match(command)


def test_unregistered_project_install_is_not_claimed():
    assert ManagedCliRegistry().match("npm install react") is None


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("npm install --global prettier", ("prettier", "prettier")),
        ("npm add -g @scope/tool@1.2.3", ("@scope/tool@1.2.3", "@scope/tool")),
    ],
)
def test_generic_node_cli_install_parser_accepts_only_registry_identity(command, expected):
    assert generic_node_cli_install(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "npm install -g prettier@latest",
        "npm install -g prettier@^3",
        "npm install -g https://example.invalid/tool.tgz",
        "npm install -g prettier --registry=https://example.invalid",
        "npm install -g prettier && prettier --version",
        "HOME=/tmp npm install -g prettier",
    ],
)
def test_generic_node_cli_install_parser_rejects_non_reproducible_surfaces(command):
    assert generic_node_cli_install(command) is None


def test_lark_adapter_accepts_documented_notifier_environment():
    match = ManagedCliRegistry().match(
        "LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 LARKSUITE_CLI_NO_SKILLS_NOTIFIER=true lark-cli auth status --json --verify"
    )
    assert dict(match.env) == {
        "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "true",
        "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
    }


@pytest.mark.parametrize(
    "command",
    [
        'lark-cli auth status 2>&1 || echo "EXIT:$?"',
        "lark-cli auth status 2>&1 || echo 'EXIT:$?'",
        "lark-cli auth status 2>&1 || echo EXIT:$?",
        # Display-only fallbacks with arbitrary echo wording are stripped too:
        # the runner already captures stderr and the real exit code, and the
        # stripped suffix never reaches argv.
        'lark-cli auth status 2>&1 || echo "EXIT_CODE: $?"',
        "lark-cli auth status 2>&1 || echo hacked",
        'lark-cli auth status 2>&1 || echo "$HOME"',
        'lark-cli auth status || echo "EXIT:$?"',
    ],
)
def test_lark_adapter_normalizes_exact_exit_diagnostic_wrapper(command):
    match = ManagedCliRegistry().match(command)

    assert match is not None
    assert match.argv == ("lark-cli", "auth", "status")
    assert match.action == ManagedCliAction.CREDENTIAL_READ


@pytest.mark.parametrize(
    "command",
    [
        "lark-cli auth status 2>&1 || true",
    ],
)
def test_lark_adapter_rejects_noncanonical_shell_fallbacks(command):
    with pytest.raises(UnsupportedManagedCliCommand):
        ManagedCliRegistry().match(command)


@pytest.mark.parametrize(
    ("command", "destructive"),
    [
        ("lark-cli im send --data '{}'", False),
        ("lark-cli doc update --data '{}'", False),
        ("lark-cli base update --data '{}'", False),
        ("lark-cli drive permissions remove --token abc", False),
        ("lark-cli drive files delete --token abc", True),
        ("lark-cli sheets +cells-clear --range A1", True),
        ("lark-cli openapi request --method=DELETE", True),
        ("lark-cli auth logout --json", True),
        (
            "lark-cli im +messages-send --markdown 'delete / --output=not-a-path'",
            False,
        ),
    ],
)
def test_lark_personal_autonomy_only_marks_delete_semantics(command, destructive):
    match = ManagedCliRegistry().match(command)
    assert match is not None
    assert match.destructive is destructive
    assert is_lark_destructive_argv(match.argv) is destructive


def test_lark_adapter_keeps_message_body_out_of_policy_classification():
    match = ManagedCliRegistry().match(
        "lark-cli im +messages-send --markdown 'delete / --output=not-a-path'"
    )

    assert match is not None
    assert match.argv[-1] == "delete / --output=not-a-path"
    assert match.destructive is False
    assert match.workspace_writable is False


@pytest.mark.parametrize("flag", ["--yes=true", "--yes=1", "-y=true"])
def test_lark_adapter_rejects_confirmation_flag_variants(flag):
    with pytest.raises(UnsupportedManagedCliCommand):
        ManagedCliRegistry().match(f"lark-cli im send {flag}")


@pytest.mark.parametrize(
    "command",
    [
        "lark-cli auth qrcode https://open.feishu.cn/page/cli --output ../secret.png",
        "lark-cli auth login --device-code ABCD extra",
        "lark-cli config init --new --unknown",
    ],
)
def test_lark_adapter_rejects_unsafe_interactive_variants(command):
    with pytest.raises(UnsupportedManagedCliCommand):
        ManagedCliRegistry().match(command)


def test_vault_rejects_tamper_and_cross_profile_replay():
    vault = CredentialVault(b"k" * 32)
    archive = _credential_archive()
    sealed = vault.seal(archive, owner_user_id="owner", provider="lark", profile_id="one")
    assert vault.open(sealed, owner_user_id="owner", provider="lark", profile_id="one") == archive

    with pytest.raises(InvalidTag):
        vault.open(sealed, owner_user_id="owner", provider="lark", profile_id="two")
    envelope = json.loads(sealed)
    envelope["ciphertext"] = envelope["ciphertext"][:-2] + "AA"
    with pytest.raises((InvalidTag, ValueError)):
        vault.open(
            json.dumps(envelope).encode(),
            owner_user_id="owner",
            provider="lark",
            profile_id="one",
        )

    continuation = vault.seal_flow(
        b'{"device_code":"canary"}',
        owner_user_id="owner",
        provider="lark",
        profile_id="one",
        flow_id="auth-one",
    )
    with pytest.raises(InvalidTag):
        vault.open_flow(
            continuation,
            owner_user_id="owner",
            provider="lark",
            profile_id="one",
            flow_id="auth-two",
        )


def test_credential_archive_rejects_links_and_traversal():
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        link = tarfile.TarInfo(".lark-cli/token")
        link.type = tarfile.SYMTYPE
        link.linkname = "/workspace/secret"
        archive.addfile(link)
    with pytest.raises(ValueError, match="unsafe"):
        validate_credential_archive(output.getvalue(), allowed_roots=_LARK_STATE.paths)


def test_lark_adapter_declares_versioned_multi_root_state_contract():
    config = ManagedCliRegistry().match("lark-cli config init --new")
    login = ManagedCliRegistry().match("lark-cli auth login --domain all --no-wait --json")
    operation = ManagedCliRegistry().match("lark-cli im send --data '{}'")

    assert config is not None and login is not None and operation is not None
    assert config.credential_state == login.credential_state == operation.credential_state == _LARK_STATE
    assert _LARK_STATE.paths == (".lark-cli", ".local/share/lark-cli")
    assert dict(_LARK_STATE.env) == {"LARKSUITE_CLI_DATA_DIR": "/home/puddingclaw/.lark-cli/.credential-data"}
    assert len(_LARK_STATE.fingerprint) == 64


@pytest.mark.parametrize(
    ("paths", "env"),
    [
        (("/absolute",), ()),
        ((".lark-cli/../escape",), ()),
        ((".lark-cli", ".lark-cli/nested"), ()),
        ((".lark-cli\\escape",), ()),
        ((".lark-cli",), (("HOME", "/home/puddingclaw/.lark-cli"),)),
        ((".lark-cli",), (("LARKSUITE_CLI_DATA_DIR", "/tmp/outside"),)),
    ],
)
def test_credential_state_spec_rejects_unsafe_contracts(paths, env):
    with pytest.raises(ValueError, match="credential state"):
        CredentialStateSpec(paths=paths, env=env)


def test_credential_archive_accepts_exact_multi_roots_and_rejects_prefix_confusion():
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for directory in (".lark-cli", ".local/share/lark-cli"):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        secret = b"encrypted"
        info = tarfile.TarInfo(".local/share/lark-cli/appsecret.enc")
        info.size = len(secret)
        archive.addfile(info, io.BytesIO(secret))
    payload = output.getvalue()

    assert validate_credential_archive(payload, allowed_roots=_LARK_STATE.paths) == payload

    confused = io.BytesIO()
    with tarfile.open(fileobj=confused, mode="w:gz") as archive:
        data = b"nope"
        info = tarfile.TarInfo(".local/share/lark-cli-evil/secret")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError, match="unsafe"):
        validate_credential_archive(confused.getvalue(), allowed_roots=_LARK_STATE.paths)


def test_credential_archive_rejects_duplicate_members():
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for content in (b"one", b"two"):
            info = tarfile.TarInfo(".lark-cli/config.json")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    with pytest.raises(ValueError, match="unsafe"):
        validate_credential_archive(output.getvalue(), allowed_roots=_LARK_STATE.paths)


def test_profile_resolution_is_explicit_then_project_then_default(tmp_path):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    store = CredentialProfileStore(paths, "owner", vault=CredentialVault(b"a" * 32))
    default = store.resolve("lark")
    alternate = store.create_profile("lark", "lark_company_b", "Company B")
    store.bind_project("project_a", "lark", alternate["profile_id"])

    assert store.resolve("lark", project_id="project_a")["profile_id"] == "lark_company_b"
    assert (
        store.resolve(
            "lark",
            project_id="project_a",
            explicit_profile_id=default["profile_id"],
        )["profile_id"]
        == default["profile_id"]
    )
    assert store.resolve("lark", project_id="project_b")["profile_id"] == default["profile_id"]
    assert (paths.credentials_root("owner").stat().st_mode & 0o777) == 0o700


def test_profile_vault_freezes_credential_state_fingerprint(tmp_path):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    store = CredentialProfileStore(paths, "owner", vault=CredentialVault(b"a" * 32))
    profile = store.resolve("fixture")
    assert profile is not None
    archive = _credential_archive()
    store.write_state(
        "fixture",
        profile["profile_id"],
        archive,
        credential_state=_LARK_STATE,
    )
    assert (
        store.read_state(
            "fixture",
            profile["profile_id"],
            credential_state=_LARK_STATE,
        )
        == archive
    )

    changed = CredentialStateSpec(
        paths=_LARK_STATE.paths,
        env=(("LARKSUITE_CLI_DATA_DIR", "/home/puddingclaw/.lark-cli/changed"),),
        schema_version=_LARK_STATE.schema_version + 1,
    )
    with pytest.raises(ValueError, match="state contract"):
        store.read_state(
            "fixture",
            profile["profile_id"],
            credential_state=changed,
        )


def test_profile_vault_writeback_uses_state_revision_cas(tmp_path):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    store = CredentialProfileStore(paths, "owner", vault=CredentialVault(b"a" * 32))
    profile = store.resolve("fixture")
    assert profile is not None
    first = _credential_archive(b'{"token":"first"}')
    competing = _credential_archive(b'{"token":"competing"}')
    rotated = _credential_archive(b'{"token":"rotated"}')
    store.write_state("fixture", profile["profile_id"], first, credential_state=_LARK_STATE)
    frozen_revision = store.state_revision("fixture", profile["profile_id"])
    store.write_state("fixture", profile["profile_id"], competing, credential_state=_LARK_STATE)

    assert (
        store.write_state_if_revision(
            "fixture",
            profile["profile_id"],
            rotated,
            expected_revision=frozen_revision,
            credential_state=_LARK_STATE,
        )
        is None
    )
    assert store.read_state("fixture", profile["profile_id"], credential_state=_LARK_STATE) == competing

    current_revision = store.state_revision("fixture", profile["profile_id"])
    committed = store.write_state_if_revision(
        "fixture",
        profile["profile_id"],
        rotated,
        expected_revision=current_revision,
        credential_state=_LARK_STATE,
    )
    assert committed == store.state_revision("fixture", profile["profile_id"])
    assert store.read_state("fixture", profile["profile_id"], credential_state=_LARK_STATE) == rotated


def test_profile_vault_metadata_failure_does_not_cross_token_commit_point(tmp_path, monkeypatch):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    store = CredentialProfileStore(paths, "owner", vault=CredentialVault(b"a" * 32))
    profile = store.resolve("fixture")
    assert profile is not None
    initial = _credential_archive(b'{"token":"initial"}')
    rotated = _credential_archive(b'{"token":"rotated"}')
    store.write_state("fixture", profile["profile_id"], initial, credential_state=_LARK_STATE)
    frozen_revision = store.state_revision("fixture", profile["profile_id"])
    monkeypatch.setattr(
        store,
        "_write_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("metadata volume unavailable")),
    )

    with pytest.raises(OSError, match="metadata volume unavailable"):
        store.write_state_if_revision(
            "fixture",
            profile["profile_id"],
            rotated,
            expected_revision=frozen_revision,
            credential_state=_LARK_STATE,
        )

    assert store.state_revision("fixture", profile["profile_id"]) == frozen_revision
    assert store.read_state("fixture", profile["profile_id"], credential_state=_LARK_STATE) == initial


def test_authorization_driver_registry_is_frozen_and_rejects_ambiguity():
    driver = LarkAuthorizationDriver()
    registry = AuthorizationDriverRegistry((driver,))
    assert registry.for_adapter("lark-cli") is driver
    assert registry.for_provider("lark") is driver
    assert registry.for_adapter("fixture-cli", required=False) is None
    with pytest.raises(ValueError, match="unique"):
        AuthorizationDriverRegistry((driver, LarkAuthorizationDriver()))


def test_single_phase_fixture_driver_start_resume_verify_and_vault_commit(tmp_path, monkeypatch):
    """A one-phase non-Lark graph runs without a Coordinator provider branch."""

    phase = AuthorizationPhaseSpec(
        phase_id="workspace_link",
        step=1,
        total=1,
        title="Link Fixture workspace",
        description="Authorize the Fixture CLI for this account.",
        completion_hint="Complete authorization, then continue.",
        recovery_evidence=AuthorizationRecoveryEvidence.STAGING_AND_CONTINUATION,
        missing_evidence_action=AuthorizationMissingEvidenceAction.RESET_ATTEMPT,
    )

    class FixtureAdapter:
        adapter_id = "fixture-auth-cli"
        provider = "fixture-auth"
        executables = frozenset({"fixture-auth"})
        toolchain_package = ToolchainPackageSpec(
            ecosystem="node",
            package="@fixture/auth-cli",
            executable="fixture-auth",
        )
        credential_state = CredentialStateSpec(paths=(".fixture-auth",))
        connector = None

        def claims(self, command):
            return "fixture-auth" in command

        def parse(self, tokens, _env):
            argv = tuple(tokens)
            if argv == ("fixture-auth", "connect"):
                action = ManagedCliAction.BROWSER_AUTH
                route = ManagedCliRoute.BROWSER_AUTH
            elif argv == ("fixture-auth", "resume"):
                action = ManagedCliAction.AUTHORIZATION_RESUME
                route = ManagedCliRoute.PROVIDER
            else:
                return None
            return ManagedCliMatch(
                adapter_id=self.adapter_id,
                action=action,
                route=route,
                argv=argv,
                provider=self.provider,
                requires_profile=True,
                requires_network=True,
                credential_state=self.credential_state,
                authorization_phase=phase.phase_id,
            )

    class FixtureDriver:
        adapter_id = "fixture-auth-cli"
        provider = "fixture-auth"
        display_name = "Fixture Auth"
        graph = AuthorizationGraph(
            phases=(
                AuthorizationPhaseNode(
                    phase=phase,
                    kind=AuthorizationPhaseKind.DEVICE_AUTHORIZATION,
                ),
            ),
            purposes=(
                AuthorizationPurposeSpec(
                    purpose_id="fixture_connect",
                    phase_ids=(phase.phase_id,),
                    entry_phase_id=phase.phase_id,
                ),
            ),
            default_purpose="fixture_connect",
        )
        continuation = LarkAuthorizationDriver.continuation
        app_configuration_argv = ("fixture-auth", "connect")
        identity_status_argv = ("fixture-auth", "status", "--json")
        logout_argv = ("fixture-auth", "logout")
        user_login_argv = ("fixture-auth", "connect")
        continuation_argv = ("fixture-auth", "exchange")
        resume_argv = ("fixture-auth", "resume")
        revoke_argv = ("fixture-auth", "logout")
        contract_fingerprint = graph.fingerprint

        def handles(self, match):
            return match.adapter_id == self.adapter_id and match.provider == self.provider

        def identity_status(self, output):
            value = json.loads(output)
            return value if isinstance(value, dict) else None

        def device_authorization(self, output):
            value = json.loads(output)
            return value if isinstance(value, dict) and value.get("device_code") else None

        def validated_authorization_url(self, raw_url, *, phase_id):
            return raw_url if phase_id == phase.phase_id and raw_url == "https://auth.fixture.test/device" else None

        def config_authorization_url(self, _output):
            return None

        def bot_ready(self, _status):
            return True

        def full_identity_ready(self, status):
            return bool(status and status.get("ready") is True and status.get("verified") is True)

        def safe_identity_projection(self, status):
            return {"ready": bool((status or {}).get("ready"))}

        def authorization_failure(self, _output):
            return ProviderAuthorizationFailure("fixture_authorization_failed", "failed", False)

        def user_credential_failure(self, _output):
            return None

        def successful_operation_identity_updates(self, _output):
            return ()

        def confirmation_required(self, _output):
            return None

        def destructive_argv(self, _argv):
            return False

        def qrcode_argv(self, _verification_url):
            return None

        def profile_identity_updates(self, status):
            return (("account", "active", True, "valid"),) if self.full_identity_ready(status) else ()

        def durable_profile_ready(self, identities):
            account = identities.get("account") or {}
            return account.get("verified") is True and account.get("token_status") == "valid"

    final_state_buffer = io.BytesIO()
    with tarfile.open(fileobj=final_state_buffer, mode="w:gz") as archive:
        content = b'{"token":"fixture-token"}'
        info = tarfile.TarInfo(".fixture-auth/token.json")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    final_state = final_state_buffer.getvalue()

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def run_managed_provider_cli(self, **kwargs):
            if kwargs["argv"] == ["fixture-auth", "connect"]:
                return ManagedProviderExecutionResult(
                    json.dumps(
                        {
                            "device_code": "FIXTURE-SECRET",
                            "user_code": "FIXTURE-CODE",
                            "verification_url": "https://auth.fixture.test/device",
                            "expires_in": 600,
                        }
                    ),
                    0,
                    None,
                )
            if kwargs["argv"] == ["fixture-auth", "exchange"]:
                assert kwargs["continuation_secret"] == b"FIXTURE-SECRET"
                return ManagedProviderExecutionResult("exchanged", 0, final_state)
            assert kwargs["argv"] == ["fixture-auth", "status", "--json"]
            assert kwargs["credential_state"] == final_state
            return ManagedProviderExecutionResult(json.dumps({"ready": True, "verified": True}), 0, final_state)

    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    registry = ManagedCliRegistry((FixtureAdapter(),))
    drivers = AuthorizationDriverRegistry((FixtureDriver(),))
    service = ManagedCliService(Backend(), paths=paths, registry=registry, authorization_drivers=drivers)
    start_match = registry.match("fixture-auth connect")
    assert start_match is not None
    start_plan = service.plan(start_match, {})
    executable = start_plan.toolchain_path / "bin" / "fixture-auth"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("fixture", encoding="utf-8")

    started = service.execute(service.plan(start_match, {}), {})
    assert started.payload["status"] == "awaiting_user_browser"
    assert started.payload["authorization_request"]["phase"]["id"] == "workspace_link"
    assert "FIXTURE-SECRET" not in started.content

    resume_match = registry.match("fixture-auth resume")
    assert resume_match is not None
    completed = service.execute(service.plan(resume_match, {}), {})
    assert completed.payload["authorization_completed"] is True
    assert completed.payload["completed_phases"] == [phase.projection()]
    store = CredentialProfileStore(paths, "trusted_owner")
    assert store.read_state(
        "fixture-auth",
        start_plan.profile_id,
        credential_state=FixtureAdapter.credential_state,
    ) == final_state


@pytest.mark.parametrize(
    ("field", "value", "cancel_reason"),
    [
        ("adapter_contract_fingerprint", "f" * 64, "authorization_contract_changed"),
        ("purpose", "unknown_authorization_path", "authorization_graph_changed"),
    ],
)
def test_authorization_resume_rejects_stale_contract_or_unknown_graph_path(
    tmp_path,
    monkeypatch,
    field,
    value,
    cancel_reason,
):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def run_managed_provider_cli(self, **_kwargs):
            raise AssertionError("stale authorization flow reached the Provider runner")

    service = ManagedCliService(Backend(), paths=paths)
    resume_match = ManagedCliRegistry().match("lark-cli auth resume")
    assert resume_match is not None
    plan = service.plan(resume_match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    original_state = _credential_archive(b'{"token":"old"}')
    store.write_state("lark", plan.profile_id, original_state, credential_state=_LARK_STATE)
    store.update_status(plan.profile_id, "active")
    flow_store, active = _seed_lark_user_flow(paths, store, plan.profile_id, original_state)
    registry = flow_store._read_registry()
    record = next(item for item in registry["flows"] if item["flow_id"] == active["flow_id"])
    record[field] = value
    flow_store._write_registry(registry)

    result = service.execute(service.plan(resume_match, {}), {})

    assert result.payload["error"] == "authorization_flow_missing"
    assert store.read_state_metadata("lark", plan.profile_id)["state_model"] == "provider_native_profile_dirs"
    cancelled = next(item for item in flow_store._read_registry()["flows"] if item["flow_id"] == active["flow_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_reason"] == cancel_reason


def test_toolchain_install_switches_atomically_and_failure_keeps_current(tmp_path):
    manager = ToolchainManager(PuddingClawPaths(tmp_path / ".puddingclaw"), "test-runtime")

    class Backend(_RuntimeImageBackend):
        def __init__(self):
            self.fail = False

        def install_managed_node_cli(
            self, *, distribution, package, executable, verification_argv, toolchain_path, container_path, **_kwargs
        ):
            _write_fake_managed_node_package(
                toolchain_path,
                package="@larksuite/cli",
                executable=executable,
                version="1.2.3",
            )
            return SimpleNamespace(
                exit_code=1 if self.fail else 0,
                output="failed" if self.fail else "v1.2.3",
            )

    backend = Backend()
    _install_test_package(
        manager,
        backend,
        adapter_id="lark-cli",
        spec=LarkManagedCliAdapter.toolchain_package,
        distribution="@larksuite/cli@1.2.3",
        adapter_contract_fingerprint="lark-contract",
        credential_state_fingerprint=LarkManagedCliAdapter.credential_state.fingerprint,
        expected_revision="empty",
    )
    first = manager.resolve_node("lark-cli").host_path
    assert len(first.name) == 64
    assert (first / "bin" / "lark-cli").exists()

    backend.fail = True
    _install_test_package(
        manager,
        backend,
        adapter_id="lark-cli",
        spec=LarkManagedCliAdapter.toolchain_package,
        distribution="@larksuite/cli@2.0.0",
        adapter_contract_fingerprint="lark-contract",
        credential_state_fingerprint=LarkManagedCliAdapter.credential_state.fingerprint,
        expected_revision=first.name,
    )
    assert manager.resolve_node("lark-cli").host_path == first


def test_missing_managed_cli_returns_trusted_installer_command(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    monkeypatch.setenv("PUDDINGCLAW_LARK_CLI_PATH", str(tmp_path / "missing" / "lark-cli"))

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test-runtime")

        @staticmethod
        def managed_runtime_image_digest():
            return _TEST_IMAGE_DIGEST

    service = ManagedCliService(
        Backend(),
        paths=PuddingClawPaths(tmp_path / ".puddingclaw"),
    )
    match = ManagedCliRegistry().match("lark-cli --version")
    assert match is not None

    result = service.execute(service.plan(match, {}), {})

    assert result.payload["error"] == "managed_cli_not_installed"
    assert result.payload["installation"] == {
        "adapter_id": "lark-cli",
        "ecosystem": "node",
        "package": "@larksuite/cli",
        "command_argv": ["npm", "install", "--global", "@larksuite/cli"],
    }


def test_toolchain_rejects_version_drift_and_can_rollback_verified_release(tmp_path):
    manager = ToolchainManager(PuddingClawPaths(tmp_path / ".puddingclaw"), "test-runtime")
    package = ToolchainPackageSpec(
        ecosystem="node",
        package="@fixture/cli",
        executable="fixture-cli",
        compatibility=">=1.0.0 <2.0.0",
    )

    class Backend(_RuntimeImageBackend):
        resolved = "1.2.3"

        def install_managed_node_cli(
            self, *, distribution, package, executable, verification_argv, toolchain_path, container_path, **_kwargs
        ):
            _write_fake_managed_node_package(
                toolchain_path,
                package="@fixture/cli",
                executable=executable,
                version=self.resolved,
            )
            return SimpleNamespace(exit_code=0, output=f"v{self.resolved}")

    backend = Backend()
    first_result = _install_test_package(
        manager,
        backend,
        adapter_id="fixture-cli",
        spec=package,
        distribution="@fixture/cli@1.2.3",
        adapter_contract_fingerprint="fixture-adapter-v1",
        credential_state_fingerprint="fixture-state-v1",
        expected_revision="empty",
    )
    assert first_result.exit_code == 0
    first = manager.resolve_node("fixture-cli").host_path

    backend.resolved = "1.3.0"
    second_result = _install_test_package(
        manager,
        backend,
        adapter_id="fixture-cli",
        spec=package,
        distribution="@fixture/cli@1.3.0",
        adapter_contract_fingerprint="fixture-adapter-v1",
        credential_state_fingerprint="fixture-state-v1",
        expected_revision=first.name,
    )
    assert second_result.exit_code == 0
    second = manager.resolve_node("fixture-cli").host_path
    assert second != first

    with pytest.raises(ValueError, match="rollback approval"):
        manager.rollback_node(
            adapter_id="fixture-cli",
            release_id=first.name,
            spec=package,
            adapter_contract_fingerprint="fixture-adapter-v1",
            credential_state_fingerprint="fixture-state-v1",
            runtime_image_digest=_TEST_IMAGE_DIGEST,
            expected_revision="stale-revision",
        )
    assert (
        manager.rollback_node(
            adapter_id="fixture-cli",
            release_id=first.name,
            spec=package,
            adapter_contract_fingerprint="fixture-adapter-v1",
            credential_state_fingerprint="fixture-state-v1",
            runtime_image_digest=_TEST_IMAGE_DIGEST,
            expected_revision=second.name,
        ).host_path
        == first
    )

    backend.resolved = "2.0.0"
    incompatible = _install_test_package(
        manager,
        backend,
        adapter_id="fixture-cli",
        spec=package,
        distribution="@fixture/cli@2.0.0",
        adapter_contract_fingerprint="fixture-adapter-v1",
        credential_state_fingerprint="fixture-state-v1",
        expected_revision=first.name,
    )
    assert incompatible.exit_code != 0
    assert manager.resolve_node("fixture-cli").host_path == first

    (second / "bin" / "fixture-cli").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="failed validation"):
        manager.rollback_node(
            adapter_id="fixture-cli",
            release_id=second.name,
            spec=package,
            adapter_contract_fingerprint="fixture-adapter-v1",
            credential_state_fingerprint="fixture-state-v1",
            runtime_image_digest=_TEST_IMAGE_DIGEST,
            expected_revision=first.name,
        )


def test_registry_and_service_install_a_second_adapter_without_install_branches(tmp_path, monkeypatch):
    fixture_state = CredentialStateSpec(paths=(".fixture-cli",))
    fixture_package = ToolchainPackageSpec(
        ecosystem="node",
        package="@fixture/cli",
        executable="fixture-cli",
        compatibility=">=3.0.0 <4.0.0",
    )

    class FixtureAdapter:
        adapter_id = "fixture-cli"
        provider = "fixture"
        executables = frozenset({"fixture-cli"})
        toolchain_package = fixture_package
        credential_state = fixture_state

        def claims(self, command):
            return "fixture-cli" in command or "@fixture/cli" in command

        def parse(self, tokens, env):
            if tuple(tokens) == ("npm", "install", "--global", "@fixture/cli@3.1.0") and not env:
                return ManagedCliMatch(
                    adapter_id=self.adapter_id,
                    action=ManagedCliAction.INSTALL,
                    route=ManagedCliRoute.INSTALLER,
                    argv=tuple(tokens),
                    requires_network=True,
                    distribution="@fixture/cli@3.1.0",
                )
            return None

    with pytest.raises(ValueError, match="at least one"):
        ManagedCliRegistry(())
    registry = ManagedCliRegistry((FixtureAdapter(),))
    match = registry.match("npm install --global @fixture/cli@3.1.0")
    assert match is not None

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test-runtime")

        @staticmethod
        def resolve_managed_node_cli(*, distribution, package):
            assert distribution == "@fixture/cli@3.1.0"
            return SimpleNamespace(
                package=package,
                version="3.1.0",
                integrity=_TEST_INTEGRITY,
                distribution="@fixture/cli@3.1.0",
                runtime_image_digest=_TEST_IMAGE_DIGEST,
            )

        @staticmethod
        def install_managed_node_cli(
            *, distribution, package, executable, verification_argv, toolchain_path, container_path, **_kwargs
        ):
            _write_fake_managed_node_package(
                toolchain_path,
                package="@fixture/cli",
                executable=executable,
                version="3.1.0",
            )
            return SimpleNamespace(exit_code=0, output="fixture-cli v3.1.0")

    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    backend = _SharedRuntimeTestAdapter(
        Backend(),
        spec=fixture_package,
        distribution="@fixture/cli@3.1.0",
        image_digest=_TEST_IMAGE_DIGEST,
        integrity=_TEST_INTEGRITY,
    )
    service = ManagedCliService(
        backend,
        paths=PuddingClawPaths(tmp_path / ".puddingclaw"),
        registry=registry,
    )
    plan = service.plan(match, {})
    assert plan.resolved_distribution == "@fixture/cli@3.1.0"
    assert plan.resolved_integrity == _TEST_INTEGRITY
    assert plan.runtime_image_digest == _TEST_IMAGE_DIGEST
    assert plan.resolution_fingerprint in plan.approval_preview()
    replayed = replace(
        plan,
        toolchain_lease_id="replayed-lease",
        toolchain_lease_expires_at=plan.toolchain_lease_expires_at + 60,
    )
    assert replayed.approval_preview() == plan.approval_preview()
    approval = json.loads(plan.approval_preview())
    assert "toolchain_lease_id" not in approval
    assert "toolchain_lease_expires_at" not in approval
    result = service.execute(plan, {})
    assert result.exit_code == 0
    assert result.payload["adapter_id"] == "fixture-cli"
    assert (service.toolchains.resolve_node("fixture-cli").host_path / "bin" / "fixture-cli").exists()


def test_adapter_packages_share_one_runtime_and_install_uses_global_revision_cas(tmp_path):
    manager = ToolchainManager(PuddingClawPaths(tmp_path / ".puddingclaw"), "test-runtime")
    fixture = ToolchainPackageSpec(
        ecosystem="node",
        package="@fixture/cli",
        executable="fixture-cli",
        compatibility=">=3.0.0 <4.0.0",
    )

    class Backend(_RuntimeImageBackend):
        version = "1.2.3"

        def install_managed_node_cli(
            self, *, distribution, package, executable, verification_argv, toolchain_path, container_path, **_kwargs
        ):
            package = distribution.rsplit("@", 1)[0] if distribution.count("@") > 1 else distribution
            _write_fake_managed_node_package(
                toolchain_path,
                package=package,
                executable=executable,
                version=self.version,
            )
            return SimpleNamespace(exit_code=0, output=f"v{self.version}")

    backend = Backend()
    lark_contract = "lark-contract"
    lark = _install_test_package(
        manager,
        backend,
        adapter_id="lark-cli",
        spec=LarkManagedCliAdapter.toolchain_package,
        distribution="@larksuite/cli@1.2.3",
        adapter_contract_fingerprint=lark_contract,
        credential_state_fingerprint=LarkManagedCliAdapter.credential_state.fingerprint,
        expected_revision="empty",
    )
    assert lark.exit_code == 0
    lark_release = manager.resolve_node("lark-cli").host_path

    backend.version = "3.1.0"
    installed_fixture = _install_test_package(
        manager,
        backend,
        adapter_id="fixture-cli",
        spec=fixture,
        distribution="@fixture/cli@3.1.0",
        adapter_contract_fingerprint="fixture-contract",
        credential_state_fingerprint="fixture-state",
        expected_revision=lark_release.name,
    )
    assert installed_fixture.exit_code == 0
    fixture_release = manager.resolve_node("fixture-cli").host_path
    assert fixture_release != lark_release
    assert manager.resolve_node("lark-cli").host_path == fixture_release
    assert (fixture_release / "bin" / "lark-cli").exists()
    assert (fixture_release / "bin" / "fixture-cli").exists()

    backend.version = "3.2.0"
    upgraded = _install_test_package(
        manager,
        backend,
        adapter_id="fixture-cli",
        spec=fixture,
        distribution="@fixture/cli@3.2.0",
        adapter_contract_fingerprint="fixture-contract",
        credential_state_fingerprint="fixture-state",
        expected_revision=fixture_release.name,
    )
    assert upgraded.exit_code == 0
    stale = _install_test_package(
        manager,
        backend,
        adapter_id="fixture-cli",
        spec=fixture,
        distribution="@fixture/cli@3.2.0",
        adapter_contract_fingerprint="fixture-contract",
        credential_state_fingerprint="fixture-state",
        expected_revision=fixture_release.name,
    )
    assert stale.exit_code == 75
    assert manager.resolve_node("fixture-cli").host_path.name == upgraded.active_revision


def test_toolchain_integrity_mismatch_and_post_publish_tamper_fail_closed(tmp_path):
    manager = ToolchainManager(PuddingClawPaths(tmp_path / ".puddingclaw"), "test-runtime")
    incompatible_integrity = ToolchainPackageSpec(
        ecosystem="node",
        package="@fixture/cli",
        executable="fixture-cli",
        compatibility=">=3.0.0 <4.0.0",
        expected_integrity="sha512-ZGVm",
    )

    class Backend(_RuntimeImageBackend):
        @staticmethod
        def install_managed_node_cli(
            *, distribution, package, executable, verification_argv, toolchain_path, container_path, **_kwargs
        ):
            _write_fake_managed_node_package(
                toolchain_path,
                package="@fixture/cli",
                executable=executable,
                version="3.1.0",
            )
            return SimpleNamespace(exit_code=0, output="v3.1.0")

    rejected = _install_test_package(
        manager,
        Backend(),
        adapter_id="fixture-cli",
        spec=incompatible_integrity,
        distribution="@fixture/cli@3.1.0",
        adapter_contract_fingerprint="fixture-contract",
        credential_state_fingerprint="fixture-state",
        expected_revision="empty",
    )
    assert rejected.exit_code != 0
    assert manager.resolve_node("fixture-cli").host_path.name == "empty"

    class ConflictingEvidenceBackend(Backend):
        @staticmethod
        def install_managed_node_cli(
            *, distribution, package, executable, verification_argv, toolchain_path, container_path, **_kwargs
        ):
            _write_fake_managed_node_package(
                toolchain_path,
                package="@fixture/cli",
                executable=executable,
                version="3.1.0",
            )
            (toolchain_path / ".installer-attestation.json").write_text(
                json.dumps(
                    {
                        "package": "@fixture/cli",
                        "version": "3.1.0",
                        "integrity": "sha512-UkVHSVNUUlk=",
                    }
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(exit_code=0, output="v3.1.0")

    conflicting = _install_test_package(
        manager,
        ConflictingEvidenceBackend(),
        adapter_id="fixture-cli",
        spec=ToolchainPackageSpec(
            ecosystem="node",
            package="@fixture/cli",
            executable="fixture-cli",
            compatibility=">=3.0.0 <4.0.0",
        ),
        distribution="@fixture/cli@3.1.0",
        adapter_contract_fingerprint="fixture-contract",
        credential_state_fingerprint="fixture-state",
        expected_revision="empty",
    )
    assert conflicting.exit_code == 0
    assert manager.resolve_for_adapter(
        adapter_id="fixture-cli",
        spec=ToolchainPackageSpec(
            ecosystem="node",
            package="@fixture/cli",
            executable="fixture-cli",
            compatibility=">=3.0.0 <4.0.0",
        ),
        adapter_contract_fingerprint="fixture-contract",
        credential_state_fingerprint="fixture-state",
        runtime_image_digest=_TEST_IMAGE_DIGEST,
    ).host_path.name == conflicting.active_revision

    class ReservedPathBackend(Backend):
        @staticmethod
        def install_managed_node_cli(
            *, distribution, package, executable, verification_argv, toolchain_path, container_path, **_kwargs
        ):
            _write_fake_managed_node_package(
                toolchain_path,
                package="@fixture/cli",
                executable=executable,
                version="3.1.0",
            )
            (toolchain_path / "INSTALL_FAILED").write_text("forged", encoding="utf-8")
            return SimpleNamespace(exit_code=0, output="v3.1.0")

    reserved_manager = ToolchainManager(PuddingClawPaths(tmp_path / ".reserved"), "test-runtime")
    with pytest.raises(ValueError, match="reserved runtime control path"):
        _install_test_package(
            reserved_manager,
            ReservedPathBackend(),
            adapter_id="fixture-cli",
            spec=ToolchainPackageSpec(
                ecosystem="node",
                package="@fixture/cli",
                executable="fixture-cli",
                compatibility=">=3.0.0 <4.0.0",
            ),
            distribution="@fixture/cli@3.1.0",
            adapter_contract_fingerprint="fixture-contract",
            credential_state_fingerprint="fixture-state",
            expected_revision="empty",
        )
    assert reserved_manager.resolve_node("fixture-cli").host_path.name == "empty"

    package = ToolchainPackageSpec(
        ecosystem="node",
        package="@fixture/cli",
        executable="fixture-cli",
        compatibility=">=3.0.0 <4.0.0",
        expected_integrity="sha512-YWJj",
    )
    installed = _install_test_package(
        manager,
        Backend(),
        adapter_id="fixture-cli",
        spec=package,
        distribution="@fixture/cli@3.1.0",
        adapter_contract_fingerprint="fixture-contract",
        credential_state_fingerprint="fixture-state",
        expected_revision=conflicting.active_revision,
    )
    assert installed.exit_code == 0
    active = manager.resolve_node("fixture-cli").host_path
    (active / "INSTALL_FAILED").write_text("forged legacy marker", encoding="utf-8")

    class FailedUpdateBackend:
        @staticmethod
        def install_managed_node_cli(
            *, distribution, package, executable, verification_argv, toolchain_path, container_path, **_kwargs
        ):
            return SimpleNamespace(exit_code=1, output="failed update")

    with pytest.raises(ValueError, match="failed validation"):
        _install_test_package(
            manager,
            FailedUpdateBackend(),
            adapter_id="fixture-cli",
            spec=package,
            distribution="@fixture/cli@3.1.0",
            adapter_contract_fingerprint="fixture-contract",
            credential_state_fingerprint="fixture-state",
            expected_revision=active.name,
        )
    assert active.exists()
    assert (manager.paths.shared_node_runtime("test-runtime") / "current").resolve() == active
    (active / "INSTALL_FAILED").unlink()
    (active / "lib" / "node_modules" / "@fixture" / "cli" / "tampered.js").write_text(
        "malicious",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="failed validation"):
        manager.resolve_for_adapter(
            adapter_id="fixture-cli",
            spec=package,
            adapter_contract_fingerprint="fixture-contract",
            credential_state_fingerprint="fixture-state",
            runtime_image_digest=_TEST_IMAGE_DIGEST,
        )


def test_registry_attestation_remains_verifiable_without_npm_lock(tmp_path):
    manager = ToolchainManager(PuddingClawPaths(tmp_path / ".puddingclaw"), "test-runtime")
    package = ToolchainPackageSpec(
        ecosystem="node",
        package="@attestation/cli",
        executable="attestation-cli",
        compatibility=">=1.0.0 <2.0.0",
    )

    class Backend(_RuntimeImageBackend):
        @staticmethod
        def install_managed_node_cli(
            *, distribution, package, executable, verification_argv, toolchain_path, container_path, **_kwargs
        ):
            installed = toolchain_path / "bin" / executable
            installed.write_text("binary", encoding="utf-8")
            installed.chmod(0o755)
            package_root = toolchain_path / "lib" / "node_modules" / "@attestation" / "cli"
            package_root.mkdir(parents=True)
            (package_root / "package.json").write_text(
                json.dumps({"name": package, "version": "1.2.3"}),
                encoding="utf-8",
            )
            (toolchain_path / ".installer-attestation.json").write_text(
                json.dumps(
                    {
                        "package": package,
                        "version": "1.2.3",
                        "integrity": "sha512-YWJj",
                    }
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(exit_code=0, output="v1.2.3")

    result = _install_test_package(
        manager,
        Backend(),
        adapter_id="attestation-cli",
        spec=package,
        distribution="@attestation/cli@1.2.3",
        adapter_contract_fingerprint="attestation-contract",
        credential_state_fingerprint="attestation-state",
        runtime_image_digest=_TEST_IMAGE_DIGEST,
        expected_revision="empty",
    )
    assert result.exit_code == 0
    resolved = manager.resolve_for_adapter(
        adapter_id="attestation-cli",
        spec=package,
        adapter_contract_fingerprint="attestation-contract",
        credential_state_fingerprint="attestation-state",
        runtime_image_digest=_TEST_IMAGE_DIGEST,
    )
    assert resolved.host_path.name == result.active_revision


def test_toolchain_rollback_preview_commit_is_bound_one_time_and_audited(tmp_path, monkeypatch):
    fixture_state = CredentialStateSpec(paths=(".fixture-cli",))
    fixture_package = ToolchainPackageSpec(
        ecosystem="node",
        package="@fixture/cli",
        executable="fixture-cli",
        compatibility=">=3.0.0 <4.0.0",
    )

    class FixtureAdapter:
        adapter_id = "fixture-cli"
        provider = "fixture"
        executables = frozenset({"fixture-cli"})
        toolchain_package = fixture_package
        credential_state = fixture_state

        @staticmethod
        def claims(command):
            return "@fixture/cli" in command or "fixture-cli" in command

        def parse(self, tokens, env):
            if len(tokens) == 4 and tuple(tokens[:3]) == ("npm", "install", "--global") and not env:
                return ManagedCliMatch(
                    adapter_id=self.adapter_id,
                    action=ManagedCliAction.INSTALL,
                    route=ManagedCliRoute.INSTALLER,
                    argv=tuple(tokens),
                    requires_network=True,
                    distribution=tokens[3],
                )
            return None

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test-runtime")
        version = "3.1.0"

        @staticmethod
        def managed_runtime_image_digest():
            return _TEST_IMAGE_DIGEST

        def resolve_managed_node_cli(self, *, distribution, package):
            return SimpleNamespace(
                package=package,
                version=self.version,
                integrity=_TEST_INTEGRITY,
                distribution=f"{package}@{self.version}",
                runtime_image_digest=_TEST_IMAGE_DIGEST,
            )

        def install_managed_node_cli(
            self,
            *,
            distribution,
            package,
            executable,
            verification_argv,
            toolchain_path,
            container_path,
            **_kwargs,
        ):
            _write_fake_managed_node_package(
                toolchain_path,
                package=package,
                executable=executable,
                version=self.version,
            )
            return SimpleNamespace(exit_code=0, output=f"v{self.version}")

    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    backend = Backend()
    registry = ManagedCliRegistry((FixtureAdapter(),))
    shared_backend = _SharedRuntimeTestAdapter(
        backend,
        spec=fixture_package,
        distribution="@fixture/cli@3.1.0",
        image_digest=_TEST_IMAGE_DIGEST,
        integrity=_TEST_INTEGRITY,
    )
    service = ManagedCliService(
        shared_backend,
        paths=PuddingClawPaths(tmp_path / ".puddingclaw"),
        registry=registry,
    )
    first_match = registry.match("npm install --global @fixture/cli@3.1.0")
    first = service.execute(service.plan(first_match, {}), {})
    assert first.exit_code == 0
    first_revision = first.payload["active_revision"]

    backend.version = "3.2.0"
    second_match = registry.match("npm install --global @fixture/cli@3.2.0")
    second = service.execute(service.plan(second_match, {}), {})
    assert second.exit_code == 0
    second_revision = second.payload["active_revision"]

    plan = service.plan_toolchain_rollback("fixture-cli", first_revision)
    assert plan.expected_current_revision == second_revision
    unconfirmed = service.execute_toolchain_rollback("fixture-cli", plan.plan_id, plan.binding)
    assert unconfirmed.payload["error"] == "managed_toolchain_rollback_confirmation_required"
    wrong = service.execute_toolchain_rollback("fixture-cli", plan.plan_id, "0" * 64, confirmed=True)
    assert wrong.exit_code != 0

    committed = service.execute_toolchain_rollback("fixture-cli", plan.plan_id, plan.binding, confirmed=True)
    assert committed.exit_code == 0
    assert committed.payload["active_revision"] == first_revision
    repeated = service.execute_toolchain_rollback("fixture-cli", plan.plan_id, plan.binding, confirmed=True)
    assert repeated.exit_code != 0

    audit_failure_plan = service.plan_toolchain_rollback("fixture-cli", second_revision)
    original_record_event = service.toolchains.record_event

    def fail_success_audit(adapter_id, event):
        if event.get("event") == "rollback_succeeded":
            raise OSError("audit volume unavailable")
        return original_record_event(adapter_id, event)

    monkeypatch.setattr(service.toolchains, "record_event", fail_success_audit)
    committed_without_audit = service.execute_toolchain_rollback(
        "fixture-cli",
        audit_failure_plan.plan_id,
        audit_failure_plan.binding,
        confirmed=True,
    )
    assert committed_without_audit.exit_code == 0
    assert committed_without_audit.payload["active_revision"] == second_revision
    assert committed_without_audit.payload["audit_status"] == "failed"

    events = service.toolchains.resolve_node("fixture-cli").root_path / "events"
    event_names = {json.loads(path.read_text(encoding="utf-8"))["event"] for path in events.glob("*.json")}
    assert {"rollback_planned", "rollback_succeeded", "rollback_failed"} <= event_names


def test_revision_lease_protects_gc_and_malformed_lease_fails_closed(tmp_path):
    manager = ToolchainManager(
        PuddingClawPaths(tmp_path / ".puddingclaw"),
        "test-runtime",
    )
    package = ToolchainPackageSpec(
        ecosystem="node",
        package="@fixture/cli",
        executable="fixture-cli",
        compatibility=">=1.0.0 <2.0.0",
    )

    class Backend(_RuntimeImageBackend):
        version = "1.1.0"

        def install_managed_node_cli(
            self,
            *,
            package,
            executable,
            toolchain_path,
            **_kwargs,
        ):
            _write_fake_managed_node_package(
                toolchain_path,
                package=package,
                executable=executable,
                version=self.version,
            )
            return SimpleNamespace(exit_code=0, output=f"v{self.version}")

    backend = Backend()
    first = _install_test_package(
        manager,
        backend,
        adapter_id="fixture-cli",
        spec=package,
        distribution="@fixture/cli@1.1.0",
        adapter_contract_fingerprint="fixture-contract",
        credential_state_fingerprint="fixture-state",
        expected_revision="empty",
    )
    lease_id, _expires = manager.acquire_revision_lease(
        adapter_id="fixture-cli",
        revision=first.active_revision,
        owner_kind="plan",
        owner_id="pending-approval",
        contract_fingerprint="fixture-contract",
    )
    backend.version = "1.2.0"
    second = _install_test_package(
        manager,
        backend,
        adapter_id="fixture-cli",
        spec=package,
        distribution="@fixture/cli@1.2.0",
        adapter_contract_fingerprint="fixture-contract",
        credential_state_fingerprint="fixture-state",
        expected_revision=first.active_revision,
    )
    backend.version = "1.3.0"
    third = _install_test_package(
        manager,
        backend,
        adapter_id="fixture-cli",
        spec=package,
        distribution="@fixture/cli@1.3.0",
        adapter_contract_fingerprint="fixture-contract",
        credential_state_fingerprint="fixture-state",
        expected_revision=second.active_revision,
    )

    removed = manager.gc_revisions("fixture-cli")
    assert removed == []
    assert (manager.resolve_node("fixture-cli").root_path / "releases" / first.active_revision).exists()
    assert (manager.resolve_node("fixture-cli").root_path / "releases" / second.active_revision).exists()
    assert manager.resolve_node("fixture-cli").host_path.name == third.active_revision

    manager.release_revision_lease(adapter_id="fixture-cli", lease_id=lease_id)
    assert manager.gc_revisions("fixture-cli") == []
    assert (manager.resolve_node("fixture-cli").root_path / "releases" / first.active_revision).exists()

    leases = manager.resolve_node("fixture-cli").root_path / "leases"
    (leases / "malformed.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed Toolchain lease"):
        manager.gc_revisions("fixture-cli")


def test_request_user_id_cannot_change_managed_credential_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    backend = _backend_stub()
    service = ManagedCliService(backend, paths=PuddingClawPaths(tmp_path / ".puddingclaw"))
    match = ManagedCliRegistry().match("lark-cli auth status --verify")
    plan = service.plan(match, {"user_id": "attacker", "project_id": None})
    assert plan.owner_user_id == "trusted_owner"
    assert plan.profile_id == "lark_default"


def test_config_init_can_transactionally_repair_existing_shared_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    backend = _backend_stub()
    service = ManagedCliService(backend, paths=paths)
    match = ManagedCliRegistry().match("lark-cli config init --new")
    first = service.plan(match, {})
    CredentialProfileStore(paths, "trusted_owner", vault=CredentialVault(b"z" * 32)).update_status(
        first.profile_id,
        "active",
    )

    repaired = service.plan(match, {})
    assert repaired.profile_id == first.profile_id


@_OBSOLETE_LARK_ARCHIVE_TEST
def test_config_init_can_repair_a_legacy_vault_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    backend = _backend_stub()
    service = ManagedCliService(backend, paths=paths)
    match = ManagedCliRegistry().match("lark-cli config init --new")
    first = service.plan(match, {})
    store = CredentialProfileStore(paths, "trusted_owner", vault=CredentialVault(b"z" * 32))
    store.write_state(
        "lark",
        first.profile_id,
        _credential_archive(),
        credential_state=_LARK_STATE,
    )
    metadata_path = paths.provider_profile("trusted_owner", "lark", first.profile_id) / "profile.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop("credential_state_fingerprint")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    store.update_status(first.profile_id, "active")

    assert service.plan(match, {}).profile_id == first.profile_id


def test_config_init_can_restart_an_expired_browser_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    backend = _backend_stub()
    service = ManagedCliService(backend, paths=paths)
    match = ManagedCliRegistry().match("lark-cli config init --new")
    first = service.plan(match, {})
    CredentialProfileStore(paths, "trusted_owner", vault=CredentialVault(b"z" * 32)).update_status(
        first.profile_id,
        "awaiting_user_browser",
    )

    assert service.plan(match, {}).profile_id == first.profile_id


def test_provider_rechecks_profile_revision_inside_profile_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    backend = _backend_stub()
    service = ManagedCliService(backend, paths=paths)
    match = ManagedCliRegistry().match("lark-cli auth status --json --verify")
    plan = service.plan(match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner", vault=CredentialVault(b"z" * 32))
    store.update_status(plan.profile_id, "active")
    stale_plan = replace(plan)

    result = service.execute(stale_plan)

    assert result.payload["error"] == "managed_plan_stale"


@_OBSOLETE_LARK_ARCHIVE_TEST
def test_provider_rotation_writeback_rejects_concurrent_vault_change(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    store = CredentialProfileStore(paths, "trusted_owner")
    profile = store.resolve("lark")
    initial = _credential_archive(b'{"token":"initial"}')
    competing = _credential_archive(b'{"token":"competing"}')
    rotated = _credential_archive(b'{"token":"rotated"}')
    store.write_state("lark", profile["profile_id"], initial, credential_state=_LARK_STATE)
    store.update_status(profile["profile_id"], "active")

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def run_managed_provider_cli(self, **_kwargs):
            # Simulate a writer that violates the cooperative Profile lock.
            # The Store-level CAS must still prevent this command's stale
            # exported token from overwriting the newer durable state.
            store.write_state(
                "lark",
                profile["profile_id"],
                competing,
                credential_state=_LARK_STATE,
            )
            return ManagedProviderExecutionResult("sent", 0, rotated)

    service = ManagedCliService(Backend(), paths=paths)
    match = ManagedCliRegistry().match("lark-cli im send --data '{}'")
    plan = service.plan(match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")

    result = service.execute(plan, {})

    assert result.payload["error"] == "credential_writeback_conflict"
    assert store.read_state("lark", profile["profile_id"], credential_state=_LARK_STATE) == competing


def test_successful_user_operation_marks_auto_refreshed_identity_valid(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    store = CredentialProfileStore(paths, "trusted_owner")
    profile = store.resolve("lark")
    initial = _credential_archive(b'{"token":"expired-access-token"}')
    rotated = _credential_archive(b'{"token":"refreshed-access-token"}')
    store.write_state("lark", profile["profile_id"], initial, credential_state=_LARK_STATE)
    store.update_status(profile["profile_id"], "authorization_required")
    store.update_identity_status(profile["profile_id"], "bot", "ready", verified=True)
    store.update_identity_status(
        profile["profile_id"],
        "user",
        "needs_refresh",
        verified=True,
        token_status="needs_refresh",
    )

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def run_managed_provider_cli(self, **_kwargs):
            return ManagedProviderExecutionResult(
                json.dumps(
                    {
                        "ok": True,
                        "identity": "user",
                        "data": {"message_id": "om_test"},
                    }
                ),
                0,
                rotated,
            )

    service = ManagedCliService(Backend(), paths=paths)
    match = ManagedCliRegistry().match("lark-cli im send --data '{}'")
    plan = service.plan(match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")

    result = service.execute(plan, {})

    assert result.exit_code == 0
    refreshed = store.resolve("lark", create_default=False)
    assert refreshed["status"] == "active"
    assert refreshed["identities"]["bot"]["status"] == "ready"
    assert refreshed["identities"]["user"] == {
        "status": "active",
        "verified": True,
        "token_status": "valid",
        "updated_at": refreshed["identities"]["user"]["updated_at"],
    }
    assert store.read_state_metadata("lark", profile["profile_id"])["state_model"] == "provider_native_profile_dirs"


def test_host_lark_cli_is_not_bound_to_managed_runtime_image(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def __init__(self):
            self.image_digest = _TEST_IMAGE_DIGEST

        def managed_runtime_image_digest(self):
            return self.image_digest

        def run_managed_provider_cli(self, **_kwargs):
            return ManagedProviderExecutionResult('{"ok":true}', 0, None)

    backend = Backend()
    service = ManagedCliService(backend, paths=paths)
    match = ManagedCliRegistry().match("lark-cli im send --data '{}'")
    plan = service.plan(match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    backend.image_digest = "sha256:" + "2" * 64

    result = service.execute(plan, {})

    assert result.payload["status"] == "completed"


def test_redaction_removes_common_secret_shapes():
    output = (
        'access_token=abc refresh-token: def Authorization: Bearer ghi {"app_secret":"jkl"} '
        '{"device_code":"mno"} {"nested":{"accessToken":"stu","refreshToken":"vwx",'
        '"appSecret":"yz1","deviceCode":"yz2"}} --device-code=pqr'
    )
    redacted = redact_managed_cli_output(output)
    assert all(
        secret not in redacted for secret in ("abc", "def", "ghi", "jkl", "mno", "pqr", "stu", "vwx", "yz1", "yz2")
    )
    assert redacted.count("<redacted>") == 10


def test_authorization_diagnostics_are_strictly_structural_and_urls_reject_secrets():
    canary = "SECRET-ACCESS-TOKEN-CANARY"
    failure = _LarkAuthorizationFailure(
        "provider_authorization_error",
        "failed",
        True,
        "user",
        "not-a-safe-code-" + canary,
    )
    diagnostic = _safe_authorization_diagnostic(
        json.dumps(
            {
                "nested": {
                    "accessToken": canary,
                    "refreshToken": canary,
                    "appSecret": canary,
                    "deviceCode": canary,
                }
            }
        ),
        failure,
        exit_code=1,
        candidate_state_exported=True,
        candidate_identity_verified=False,
    )
    serialized = json.dumps(diagnostic)
    assert canary not in serialized
    assert "redacted_excerpt" not in diagnostic
    assert "output_sha256" not in diagnostic
    assert "provider_code" not in diagnostic
    assert "provider_error_type" not in diagnostic
    assert "provider_error_subtype" not in diagnostic
    classified = _safe_authorization_diagnostic(
        json.dumps(
            {
                "ok": False,
                "error": {
                    "type": "authorization",
                    "subtype": "invalid_client",
                    "message": canary,
                    "hint": f"open https://example.invalid/?token={canary}",
                },
            }
        ),
        _LarkAuthorizationFailure(
            "provider_authorization_error",
            "failed",
            True,
            "user",
            20001,
        ),
        exit_code=1,
        candidate_state_exported=False,
        candidate_identity_verified=False,
    )
    assert classified["provider_code"] == 20001
    assert classified["provider_error_type"] == "authorization"
    assert classified["provider_error_subtype"] == "invalid_client"
    assert canary not in json.dumps(classified)
    assert (
        _validated_lark_authorization_url(
            f"https://accounts.feishu.cn/oauth/v1/device/verify?device_code={canary}",
            phase_id="user_consent",
        )
        is None
    )
    assert (
        _validated_lark_authorization_url(
            "https://accounts.feishu.cn/oauth/v1/device/verify",
            phase_id="user_consent",
        )
        == "https://accounts.feishu.cn/oauth/v1/device/verify"
    )


def test_lark_user_consent_url_accepts_public_flow_id_but_not_continuation_secret():
    valid = "https://accounts.feishu.cn/oauth/v1/device/verify?flow_id=opaque-provider-flow&user_code=ABCD-EFGH"
    assert _validated_lark_authorization_url(valid, phase_id="user_consent") == valid
    assert (
        _validated_lark_authorization_url(
            valid + "&device_code=must-stay-backend-only",
            phase_id="user_consent",
        )
        is None
    )


def test_lark_identity_status_rejects_conflicting_envelopes():
    ready = {
        "identities": {
            "bot": {"status": "ready", "verified": True},
            "user": {"status": "ready", "verified": True, "tokenStatus": "valid"},
        }
    }
    invalid = {
        "identities": {
            "bot": {"status": "ready", "verified": True},
            "user": {"status": "expired", "verified": False, "tokenStatus": "expired"},
        }
    }
    assert _lark_identity_status(json.dumps(ready) + "\n" + json.dumps(invalid)) is None
    assert _lark_identity_status(json.dumps(ready) + "\n" + json.dumps(ready)) == ready
    assert _lark_identity_status(json.dumps(ready) + '\n{"ok":false,"error":"invalid"}') is None

    first_device = {
        "device_code": "first-secret",
        "verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify",
    }
    second_device = {**first_device, "device_code": "second-secret"}
    assert _lark_device_authorization(json.dumps(first_device) + "\n" + json.dumps(second_device)) is None


@pytest.mark.parametrize(
    ("error", "status", "reason", "retryable"),
    [
        (
            {"type": "oauth", "subtype": "authorization_pending", "message": "pending"},
            "awaiting_user",
            "authorization_pending",
            True,
        ),
        (
            {"type": "oauth", "subtype": "access_denied", "message": "denied"},
            "cancelled",
            "access_denied",
            False,
        ),
        (
            {"type": "oauth", "subtype": "invalid_request", "code": 20001},
            "failed",
            "provider_invalid_request",
            False,
        ),
        (
            {"type": "validation", "subtype": "invalid_argument", "message": "invalid CLI arguments"},
            "failed",
            "provider_invalid_request",
            False,
        ),
        (
            {"type": "internal", "subtype": "unexpected"},
            "failed",
            "provider_authorization_error",
            True,
        ),
    ],
)
def test_lark_resume_failure_classification_requires_explicit_terminal_evidence(
    error,
    status,
    reason,
    retryable,
):
    classified = _lark_authorization_failure(json.dumps({"ok": False, "identity": "user", "error": error}))
    assert classified.flow_status == status
    assert classified.reason == reason
    assert classified.retryable is retryable


def test_only_proven_user_token_failure_triggers_reauthorization():
    expired = json.dumps(
        {
            "ok": False,
            "identity": "user",
            "error": {
                "type": "authorization",
                "subtype": "token_expired",
                "message": "user token expired",
            },
        }
    )
    bot_failure = expired.replace('"user"', '"bot"', 1)
    missing_scope = expired.replace("token_expired", "missing_scope")
    assert _lark_user_credential_failure(expired).reason == "user_token_expired"
    assert _lark_user_credential_failure(bot_failure) is None
    assert _lark_user_credential_failure(missing_scope) is None


def test_awaiting_browser_result_allows_summary_then_blocks_next_tool():
    pipeline = ToolExecutionPipeline(known_tools={"execute"}, backend_mode="docker")
    message = ToolMessage(
        content=json.dumps(
            {
                "managed_by": "managed_cli",
                "status": "awaiting_user_browser",
                "output": "Open https://open.feishu.cn/page/cli?code=abc",
            }
        ),
        name="execute",
        tool_call_id="call-auth",
        status="success",
    )
    attempted = AIMessage(
        content="继续登录",
        tool_calls=[
            {
                "id": "next",
                "name": "execute",
                "args": {"command": "lark-cli auth login --no-wait"},
            }
        ],
    )
    assert pipeline.before_model({"messages": [message]}, SimpleNamespace(context={})) is None
    update = pipeline.after_model(
        {"messages": [message, attempted]},
        SimpleNamespace(context={}),
    )
    assert update["jump_to"] == "end"
    assert update["messages"][0].tool_calls == []
    assert "尚未完成" in update["messages"][0].content
    assert "上方卡片" in update["messages"][0].content
    assert "https://" not in update["messages"][0].content


def test_managed_authorization_failure_blocks_guessed_recovery_command():
    pipeline = ToolExecutionPipeline(known_tools={"execute"}, backend_mode="docker")
    message = ToolMessage(
        content=json.dumps(
            {
                "ok": False,
                "managed_by": "managed_cli",
                "error": "managed_authorization_failed",
                "message": "托管授权事务未能继续；Credential Profile 未被提交。",
            }
        ),
        name="execute",
        tool_call_id="call-auth",
        status="success",
    )
    attempted = AIMessage(
        content="尝试恢复流程",
        tool_calls=[
            {
                "id": "next",
                "name": "execute",
                "args": {"command": "lark-cli auth resume"},
            }
        ],
    )

    update = pipeline.after_model(
        {"messages": [message, attempted]},
        SimpleNamespace(context={}),
    )

    assert update["jump_to"] == "end"
    assert update["messages"][0].tool_calls == []
    assert "不会尝试其他授权命令" in update["messages"][0].content
    assert "auth resume" not in update["messages"][0].content


@pytest.mark.asyncio
async def test_pipeline_routes_managed_cli_without_calling_workspace_handler(tmp_path):
    calls: list[str] = []

    class Service:
        def plan_command(self, command, context):
            match = ManagedCliRegistry().match(command)
            return SimpleNamespace(match=match, approval_preview=lambda: "frozen-plan")

        def execute(self, plan, context):
            calls.append("managed")
            return SimpleNamespace(content='{"ok":true}', exit_code=0)

    async def workspace_handler(_request):
        calls.append("workspace")
        return ToolMessage(content="unsafe", name="execute", tool_call_id="call")

    request = ToolCallRequest(
        tool_call={"id": "call", "name": "execute", "args": {"command": "lark-cli --version"}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode="docker",
        managed_cli_service=Service(),
    )
    result = await pipeline.awrap_tool_call(request, workspace_handler)

    assert result.status == "success"
    assert calls == ["managed"]


@pytest.mark.asyncio
async def test_pipeline_does_not_treat_managed_message_slash_as_host_root(tmp_path, monkeypatch):
    command = (
        "LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 "
        "LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1 "
        "lark-cli im +messages-send --as bot --user-id ou_test "
        "--markdown 'Bot 状态：ready / token valid'"
    )
    calls: list[str] = []

    class Service:
        def plan_command(self, raw_command, context):
            del context
            match = ManagedCliRegistry().match(raw_command)
            assert match is not None
            return SimpleNamespace(match=match, approval_preview=lambda: "frozen-plan")

        def execute(self, plan, context):
            del plan, context
            calls.append("managed")
            return SimpleNamespace(content='{"ok":true}', exit_code=0)

    async def workspace_handler(_request):
        raise AssertionError("managed command must not reach the project shell")

    request = ToolCallRequest(
        tool_call={"id": "call", "name": "execute", "args": {"command": command}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode="kernel",
        managed_cli_service=Service(),
    )
    monkeypatch.setattr(
        pipeline,
        "_require_external_shell_authority",
        lambda _request: (_ for _ in ()).throw(
            AssertionError("managed payload must not be re-parsed as project shell paths")
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "_managed_cli_preflight",
        lambda _plan: ToolPolicyResult(PolicyDecision.ALLOW, "test", "low"),
    )

    result = await pipeline.awrap_tool_call(request, workspace_handler)

    assert result.status == "success"
    assert calls == ["managed"]


@pytest.mark.asyncio
async def test_pipeline_keeps_browser_material_in_ui_artifact_not_model_tool_content(tmp_path, monkeypatch):
    from graph.session_manager import session_manager
    from harness.models import RunRecord

    # Smart approval now persists a run-bound permission request before the
    # managed result is returned.  This test only needs an empty durable Home
    # session store; it must not rely on another test having initialized the
    # process-global SessionManager first.
    session_manager.initialize(tmp_path / "puddingclaw-home")
    session_manager.create_session("session-browser-material")
    session_manager.start_harness_run(
        "session-browser-material",
        RunRecord(
            run_id="run-browser-material",
            query_id="query-browser-material",
            session_id="session-browser-material",
            objective="managed authorization artifact",
        ).model_dump(mode="json"),
    )
    session_manager.transition_run_status(
        "session-browser-material",
        "run-browser-material",
        "running",
    )
    authorization = {
        "type": "managed_authorization_request",
        "flow_id": "auth-test",
        "revision": 1,
        "attempt": 1,
        "provider": "lark",
        "profile_id": "lark_default",
        "status": "awaiting_user",
        "phase": {
            "id": "user_consent",
            "step": 2,
            "total": 2,
            "title": "授权应用访问你的飞书数据",
            "description": "用户授权",
        },
        "verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify",
        "user_code": "ABCD-EFGH",
        "qr_ascii": "▀▄█\n" * 12,
        "completion_hint": "完成后告诉我。",
    }
    payload = {
        "ok": True,
        "managed_by": "managed_cli",
        "status": "awaiting_user_browser",
        "authorization_completed": False,
        "authorization_request": authorization,
        "output": "第 2/2 步已开始。",
    }

    class Service:
        def plan_command(self, command, context):
            match = ManagedCliRegistry().match(command)
            return SimpleNamespace(match=match, approval_preview=lambda: "frozen-plan")

        def execute(self, plan, context):
            return SimpleNamespace(payload=payload, content=json.dumps(payload), exit_code=0)

    async def workspace_handler(_request):
        raise AssertionError("managed command must not reach workspace handler")

    request = ToolCallRequest(
        tool_call={
            "id": "call-auth",
            "name": "execute",
            "args": {"command": "lark-cli auth login --domain all --no-wait --json"},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(
            context={
                "workspace_path": str(tmp_path),
                "session_id": "session-browser-material",
                "run_id": "run-browser-material",
            }
        ),
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode="docker",
        managed_cli_service=Service(),
    )
    # The service contract is the subject here. Freeze the preflight result so
    # this unit test stays independent from policy classification details.
    monkeypatch.setattr(
        pipeline,
        "_managed_cli_preflight",
        lambda _plan: ToolPolicyResult(PolicyDecision.ALLOW, "test", "low"),
    )

    message = await pipeline.awrap_tool_call(request, workspace_handler)

    assert "verification_url" not in message.content
    assert "user_code" not in message.content
    assert "qr_ascii" not in message.content
    assert "flow_id" in message.content
    assert message.artifact["puddingclaw_raw_tool_output"] == json.dumps(payload)


@pytest.mark.parametrize(
    ("command", "decision"),
    [
        ("lark-cli auth status --json --verify", PolicyDecision.ALLOW),
        ("lark-cli im send --data '{}'", PolicyDecision.ALLOW),
        ("lark-cli doc create --data '{}'", PolicyDecision.ALLOW),
        ("lark-cli base update --data '{}'", PolicyDecision.ALLOW),
        ("lark-cli drive upload --file report.md", PolicyDecision.ALLOW),
        ("lark-cli drive permissions update --data '{}'", PolicyDecision.ALLOW),
        ("lark-cli drive files delete --token abc", PolicyDecision.ASK),
        ("npm install -g @larksuite/cli", PolicyDecision.ASK),
    ],
)
def test_managed_lark_policy_gates_effects_not_credentialed_network(command, decision):
    match = ManagedCliRegistry().match(command)
    result = ToolExecutionPipeline._managed_cli_preflight(SimpleNamespace(match=match))
    assert result.decision == decision


def _managed_service_plan(tmp_path, monkeypatch, command):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def __init__(self):
            self.calls = []
            self.results = []

        def run_managed_provider_cli(self, **kwargs):
            self.calls.append(kwargs)
            return self.results.pop(0)

    backend = Backend()
    service = ManagedCliService(backend, paths=paths)
    match = ManagedCliRegistry().match(command)
    first = service.plan(match, {})
    executable = first.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    store.update_status(first.profile_id, "active")
    return service, backend, service.plan(match, {})


def _confirmation_result(action, *, risk="high-risk-write"):
    return ManagedProviderExecutionResult(
        output=json.dumps(
            {
                "ok": False,
                "identity": "user",
                "error": {
                    "type": "confirmation",
                    "subtype": "confirmation_required",
                    "risk": risk,
                    "action": action,
                },
            }
        ),
        exit_code=10,
        credential_state=None,
    )


def test_exit_10_non_delete_retries_once_with_backend_owned_yes(tmp_path, monkeypatch):
    service, backend, plan = _managed_service_plan(
        tmp_path,
        monkeypatch,
        "lark-cli im send --data '{}'",
    )
    backend.results = [
        _confirmation_result("im send"),
        ManagedProviderExecutionResult("sent", 0, None),
    ]

    result = service.execute(plan, {})

    assert result.payload["confirmation"] == "auto_approved_non_delete"
    assert [call["argv"] for call in backend.calls] == [
        ["lark-cli", "im", "send", "--data", "{}"],
        ["lark-cli", "im", "send", "--data", "{}", "--yes"],
    ]


def test_exit_10_invalid_envelope_never_retries(tmp_path, monkeypatch):
    service, backend, plan = _managed_service_plan(
        tmp_path,
        monkeypatch,
        "lark-cli im send --data '{}'",
    )
    backend.results = [_confirmation_result("im send", risk="write")]

    result = service.execute(plan, {})

    assert result.exit_code == 10
    assert len(backend.calls) == 1


def test_user_consent_cannot_start_without_verified_app_configuration(tmp_path, monkeypatch):
    service, backend, plan = _managed_service_plan(
        tmp_path,
        monkeypatch,
        "lark-cli auth login --recommend --no-wait --json",
    )
    result = service.execute(plan, {})

    assert result.payload["error"] == "authorization_prerequisite_failed"
    assert len(backend.calls) == 0
    profile = CredentialProfileStore(service.paths, "trusted_owner").resolve("lark")
    assert profile["status"] == "active"


def test_exit_10_delete_requires_plan_bound_approval(tmp_path, monkeypatch):
    service, backend, plan = _managed_service_plan(
        tmp_path,
        monkeypatch,
        "lark-cli drive files delete --token abc",
    )
    backend.results = [_confirmation_result("drive files delete")]
    denied = service.execute(plan, {})
    assert denied.payload["status"] == "confirmation_required"
    assert len(backend.calls) == 1

    backend.results = [
        _confirmation_result("drive files delete"),
        ManagedProviderExecutionResult("deleted", 0, None),
    ]
    approved = service.execute(
        plan,
        {"_managed_cli_destructive_approval": plan.destructive_approval_binding()},
    )
    assert approved.payload["status"] == "completed"
    assert backend.calls[-1]["argv"][-1] == "--yes"


def test_browser_lifecycle_worker_persists_without_another_agent_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    archive = _credential_archive(b'{"configured":true}')
    finalized = threading.Event()
    browser_job_id = _browser_job_id("trusted_owner", "lark", "lark_default")

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def run_managed_browser_auth_cli(self, **_kwargs):
            return ManagedProviderExecutionResult(
                "https://open.feishu.cn/page/cli?user_code=ABCD\n" + "\n".join(["▀▄█ " * 12] * 12),
                0,
                None,
                browser_status="awaiting_user_browser",
                browser_job_id=browser_job_id,
            )

        def collect_managed_browser_auth_cli(self, **_kwargs):
            return ManagedProviderExecutionResult(
                "configured",
                0,
                archive,
                browser_status="completed",
                browser_job_id=browser_job_id,
            )

        def finalize_managed_browser_auth_cli(self, **_kwargs):
            store = CredentialProfileStore(paths, "trusted_owner")
            flow = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault).active("lark", "lark_default")
            stored = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault).read_staged_state(flow)
            assert stored == archive
            finalized.set()
            return True

    backend = Backend()
    service = ManagedCliService(backend, paths=paths)
    match = ManagedCliRegistry().match("lark-cli config init --new")
    plan = service.plan(match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")

    started = service.execute(plan, {})

    assert started.payload["status"] == "awaiting_user_browser"
    assert finalized.wait(timeout=3)
    deadline = time.monotonic() + 3
    profile = None
    while time.monotonic() < deadline:
        profile = CredentialProfileStore(paths, "trusted_owner").resolve("lark")
        if profile.get("last_browser_job_status") == "completed":
            break
        time.sleep(0.05)
    assert profile["status"] == "pending_configuration"
    assert profile["last_browser_job_status"] == "completed"
    assert "browser_job_id" not in profile
    assert (
        CredentialProfileStore(paths, "trusted_owner").read_state("lark", "lark_default", credential_state=_LARK_STATE)
        == b""
    )


def test_orphaned_app_configuration_flow_expires_before_provider_status(tmp_path, monkeypatch):
    """An awaiting Flow without Runner evidence must not hijack Profile reads."""

    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    old_state = _credential_archive(b'{"bot":"ready","user":"active"}')
    store = CredentialProfileStore(paths, "trusted_owner")
    profile = store.resolve("lark")
    store.write_state("lark", profile["profile_id"], old_state, credential_state=_LARK_STATE)
    store.update_status(profile["profile_id"], "active")
    profile = store.resolve("lark")
    flow_store = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault)
    orphan = flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=profile["profile_id"],
        purpose="lark_full_authorization",
        phase=LARK_APP_CONFIGURATION_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision=store.state_revision("lark", profile["profile_id"]),
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={"verification_url": "https://open.feishu.cn/page/cli?user_code=ORPHAN"},
        secret=None,
        expires_at=None,
    )

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def __init__(self):
            self.calls = []

        def run_managed_provider_cli(self, **kwargs):
            self.calls.append(kwargs)
            return ManagedProviderExecutionResult(
                json.dumps(
                    {
                        "identities": {
                            "bot": {"status": "ready", "verified": True},
                            "user": {
                                "status": "active",
                                "verified": True,
                                "tokenStatus": "valid",
                            },
                        }
                    }
                ),
                0,
                old_state,
            )

    backend = Backend()
    service = ManagedCliService(backend, paths=paths)
    match = ManagedCliRegistry().match("lark-cli auth status --json --verify")
    plan = service.plan(match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")

    result = service.execute(plan, {})

    assert result.payload["status"] == "completed"
    assert backend.calls[0]["argv"] == ["lark-cli", "auth", "status", "--json", "--verify"]
    assert flow_store.active("lark", profile["profile_id"]) is None
    record = next(item for item in flow_store._read_registry()["flows"] if item["flow_id"] == orphan["flow_id"])
    assert record["status"] == "expired"
    assert record["error"] == "browser_job_missing"
    assert store.read_state_metadata("lark", profile["profile_id"])["state_model"] == "provider_native_profile_dirs"


def test_missing_browser_runner_terminalizes_flow_and_continues_provider_status(tmp_path, monkeypatch):
    """Runner loss releases both the Profile lease and the corresponding Flow."""

    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    old_state = _credential_archive(b'{"bot":"ready","user":"active"}')
    browser_job_id = _browser_job_id("trusted_owner", "lark", "lark_default")
    store = CredentialProfileStore(paths, "trusted_owner")
    profile = store.resolve("lark")
    store.write_state("lark", profile["profile_id"], old_state, credential_state=_LARK_STATE)
    store.update_status(profile["profile_id"], "active")
    profile = store.resolve("lark")
    flow_store = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault)
    flow = flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=profile["profile_id"],
        purpose="lark_full_authorization",
        phase=LARK_APP_CONFIGURATION_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision=store.state_revision("lark", profile["profile_id"]),
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={"verification_url": "https://open.feishu.cn/page/cli?user_code=MISSING"},
        secret=None,
        expires_at=None,
    )
    store.begin_browser_job(profile["profile_id"], browser_job_id, _LARK_STATE.fingerprint)

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def __init__(self):
            self.provider_calls = []

        def collect_managed_browser_auth_cli(self, **_kwargs):
            return ManagedProviderExecutionResult(
                "Managed browser authorization job is missing or expired.",
                1,
                None,
                browser_status="missing",
                browser_job_id=browser_job_id,
            )

        def run_managed_provider_cli(self, **kwargs):
            self.provider_calls.append(kwargs)
            return ManagedProviderExecutionResult('{"status":"ok"}', 0, old_state)

    backend = Backend()
    service = ManagedCliService(backend, paths=paths)
    match = ManagedCliRegistry().match("lark-cli auth status --json --verify")
    plan = service.plan(match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")

    result = service.execute(plan, {})

    assert result.payload["status"] == "completed"
    assert len(backend.provider_calls) == 1
    assert flow_store.active("lark", profile["profile_id"]) is None
    record = next(item for item in flow_store._read_registry()["flows"] if item["flow_id"] == flow["flow_id"])
    assert record["status"] == "expired"
    assert record["error"] == "browser_job_missing"
    current = store.resolve("lark")
    assert "browser_job_id" not in current
    assert current["last_browser_job_status"] == "expired"


@_OBSOLETE_LARK_ARCHIVE_TEST
def test_provider_read_cannot_commit_phase_one_candidate_or_invalidate_flow(tmp_path, monkeypatch):
    """Regression: config show used to overwrite the durable Vault mid-flow."""

    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    old_state = _credential_archive(b'{"generation":"last-known-good"}')
    candidate_state = _credential_archive(b'{"generation":"phase-one-candidate"}')
    browser_job_id = _browser_job_id("trusted_owner", "lark", "lark_default")

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def __init__(self):
            self.provider_calls = 0

        def run_managed_browser_auth_cli(self, **_kwargs):
            return ManagedProviderExecutionResult(
                "https://open.feishu.cn/page/cli?user_code=ISOLATED\n" + "\n".join(["▀▄█ " * 12] * 12),
                0,
                None,
                browser_status="awaiting_user_browser",
                browser_job_id=browser_job_id,
            )

        def collect_managed_browser_auth_cli(self, **_kwargs):
            return ManagedProviderExecutionResult(
                "configured",
                0,
                candidate_state,
                browser_status="completed",
                browser_job_id=browser_job_id,
            )

        def finalize_managed_browser_auth_cli(self, **_kwargs):
            return True

        def run_managed_provider_cli(self, **_kwargs):
            self.provider_calls += 1
            raise AssertionError("provider reads must not run while authorization owns the Profile")

    backend = Backend()
    service = ManagedCliService(backend, paths=paths)
    monkeypatch.setattr(service, "_start_browser_watcher", lambda **_kwargs: None)
    init_match = ManagedCliRegistry().match("lark-cli config init --new")
    initial = service.plan(init_match, {})
    executable = initial.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    store.write_state("lark", initial.profile_id, old_state, credential_state=_LARK_STATE)

    started = service.execute(service.plan(init_match, {}), {})
    assert started.payload["status"] == "awaiting_user_browser"
    base_revision = store.state_revision("lark", initial.profile_id)

    show_match = ManagedCliRegistry().match("lark-cli config show")
    result = service.execute(service.plan(show_match, {}), {})

    assert result.payload["status"] == "completed"
    assert result.payload["profile_status"]["freshness"] == "cached"
    assert result.payload["profile_status"]["reason"] == "authorization_write_in_progress"
    assert result.payload["authorization_flow"]["flow_id"]
    assert result.payload["authorization_flow"]["status"] == "starting"
    assert "authorization_request" not in result.payload
    assert "verification_url" not in result.payload["authorization_flow"]
    assert "next_action" not in result.payload
    assert backend.provider_calls == 0
    assert store.state_revision("lark", initial.profile_id) == base_revision
    assert store.read_state("lark", initial.profile_id, credential_state=_LARK_STATE) == old_state
    flow_store = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault)
    active = flow_store.active("lark", initial.profile_id)
    assert active is not None
    assert active["status"] == "starting"
    assert LARK_APP_CONFIGURATION_PHASE.phase_id in active["completed_phase_ids"]
    assert flow_store.read_staged_state(active) == candidate_state


def test_provider_read_during_live_browser_flow_returns_cached_profile_status(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    durable_state = _credential_archive(b'{"bot":"ready","user":"active"}')
    browser_job_id = _browser_job_id("trusted_owner", "lark", "lark_default")

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def __init__(self):
            self.provider_calls = 0

        def run_managed_browser_auth_cli(self, **_kwargs):
            return ManagedProviderExecutionResult(
                "https://open.feishu.cn/page/cli?user_code=WAITING\n" + "\n".join(["▀▄█ " * 12] * 12),
                0,
                None,
                browser_status="awaiting_user_browser",
                browser_job_id=browser_job_id,
            )

        def collect_managed_browser_auth_cli(self, **_kwargs):
            return ManagedProviderExecutionResult(
                "still waiting",
                0,
                None,
                browser_status="awaiting_user_browser",
                browser_job_id=browser_job_id,
            )

        def run_managed_provider_cli(self, **_kwargs):
            self.provider_calls += 1
            raise AssertionError("status CLI must not run while the Flow owns credential writes")

    backend = Backend()
    service = ManagedCliService(backend, paths=paths)
    monkeypatch.setattr(service, "_start_browser_watcher", lambda **_kwargs: None)
    init_match = ManagedCliRegistry().match("lark-cli config init --new")
    init_plan = service.plan(init_match, {})
    executable = init_plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    store.write_state("lark", init_plan.profile_id, durable_state, credential_state=_LARK_STATE)
    store.update_status(init_plan.profile_id, "active")
    store.update_identity_status(init_plan.profile_id, "bot", "ready", verified=True)
    store.update_identity_status(
        init_plan.profile_id,
        "user",
        "active",
        verified=True,
        token_status="valid",
    )

    started = service.execute(service.plan(init_match, {}), {})
    assert started.payload["status"] == "awaiting_user_browser"

    status_match = ManagedCliRegistry().match("lark-cli auth status --json --verify")
    result = service.execute(service.plan(status_match, {}), {})

    assert result.payload["status"] == "completed"
    assert result.payload["profile_status"] == {
        "health": "active",
        "identities": {
            "bot": {
                "status": "ready",
                "verified": True,
                "updated_at": result.payload["profile_status"]["identities"]["bot"]["updated_at"],
            },
            "user": {
                "status": "active",
                "verified": True,
                "token_status": "valid",
                "updated_at": result.payload["profile_status"]["identities"]["user"]["updated_at"],
            },
        },
        "freshness": "cached",
        "reason": "authorization_write_in_progress",
    }
    assert result.payload["authorization_flow"]["phase"]["id"] == "app_configuration"
    assert "authorization_request" not in result.payload
    assert backend.provider_calls == 0


def test_fresh_verified_status_updates_independent_identity_assessment(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    durable_state = _credential_archive(b'{"bot":"ready","user":"active"}')

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def run_managed_provider_cli(self, **_kwargs):
            return ManagedProviderExecutionResult(
                json.dumps(
                    {
                        "identities": {
                            "bot": {"status": "ready", "verified": True},
                            "user": {
                                "status": "expired",
                                "verified": False,
                                "tokenStatus": "expired",
                            },
                        }
                    }
                ),
                0,
                durable_state,
            )

    service = ManagedCliService(Backend(), paths=paths)
    match = ManagedCliRegistry().match("lark-cli auth status --json --verify")
    plan = service.plan(match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    store.write_state("lark", plan.profile_id, durable_state, credential_state=_LARK_STATE)

    result = service.execute(service.plan(match, {}), {})

    assert result.payload["status"] == "completed"
    profile = store.resolve("lark")
    assert profile["status"] == "authorization_required"
    assert profile["identities"]["bot"]["verified"] is True
    assert profile["identities"]["user"]["status"] == "expired"
    assert profile["identities"]["user"]["verified"] is False
    assert profile["identities"]["user"]["token_status"] == "expired"


def test_browser_recovery_acks_container_after_phase_one_was_durably_staged(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    store = CredentialProfileStore(paths, "trusted_owner")
    profile = store.resolve("lark")
    flow_store = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault)
    flow = flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=profile["profile_id"],
        purpose="lark_full_authorization",
        phase=LARK_APP_CONFIGURATION_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision="missing",
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={},
        secret=None,
        expires_at=None,
    )
    flow_store.write_staged_state(flow, _credential_archive(b'{"configured":true}'))
    flow_store.mark_phase_verified("lark", profile["profile_id"], LARK_APP_CONFIGURATION_PHASE.phase_id)
    store.update_status(profile["profile_id"], "awaiting_user_authorization")
    finalized = []

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def list_managed_browser_auth_jobs(self, **_kwargs):
            return [
                {
                    "provider": "lark",
                    "profile_id": profile["profile_id"],
                    "browser_job_id": "browser-orphan",
                    "credential_state_fingerprint": _LARK_STATE.fingerprint,
                }
            ]

        def finalize_managed_browser_auth_cli(self, **kwargs):
            finalized.append(kwargs)
            return True

    ManagedCliService(Backend(), paths=paths)

    assert finalized[0]["browser_job_id"] == "browser-orphan"


def test_browser_recovery_rebinds_live_runner_after_profile_lease_loss(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    store = CredentialProfileStore(paths, "trusted_owner")
    profile = store.resolve("lark")
    browser_job_id = _browser_job_id("trusted_owner", "lark", profile["profile_id"])
    flow_store = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault)
    flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=profile["profile_id"],
        purpose="lark_full_authorization",
        phase=LARK_APP_CONFIGURATION_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision="missing",
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={"verification_url": "https://open.feishu.cn/page/cli?user_code=RECOVER"},
        secret=None,
        expires_at=None,
    )
    watchers = []

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def list_managed_browser_auth_jobs(self, **_kwargs):
            return [
                {
                    "provider": "lark",
                    "profile_id": profile["profile_id"],
                    "browser_job_id": browser_job_id,
                    "adapter_id": "lark-cli",
                    "authorization_contract_fingerprint": _LARK_AUTH_CONTRACT,
                    "credential_state_fingerprint": _LARK_STATE.fingerprint,
                }
            ]

    monkeypatch.setattr(
        ManagedCliService,
        "_start_browser_watcher",
        lambda self, **kwargs: watchers.append(kwargs),
    )
    ManagedCliService(Backend(), paths=paths)

    recovered = store.resolve("lark")
    assert recovered["browser_job_id"] == browser_job_id
    assert recovered["browser_job_status"] == "awaiting_user_browser"
    assert watchers[0]["browser_job_id"] == browser_job_id
    assert flow_store.active("lark", profile["profile_id"])["status"] == "awaiting_user"


def test_browser_recovery_expires_lease_and_flow_when_runner_is_gone(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    store = CredentialProfileStore(paths, "trusted_owner")
    profile = store.resolve("lark")
    browser_job_id = _browser_job_id("trusted_owner", "lark", profile["profile_id"])
    flow_store = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault)
    flow = flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=profile["profile_id"],
        purpose="lark_full_authorization",
        phase=LARK_APP_CONFIGURATION_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision="missing",
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={"verification_url": "https://open.feishu.cn/page/cli?user_code=GONE"},
        secret=None,
        expires_at=None,
    )
    store.begin_browser_job(profile["profile_id"], browser_job_id, _LARK_STATE.fingerprint)

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def list_managed_browser_auth_jobs(self, **_kwargs):
            return []

    ManagedCliService(Backend(), paths=paths)

    recovered = store.resolve("lark")
    assert "browser_job_id" not in recovered
    assert recovered["last_browser_job_status"] == "expired"
    assert flow_store.active("lark", profile["profile_id"]) is None
    record = next(item for item in flow_store._read_registry()["flows"] if item["flow_id"] == flow["flow_id"])
    assert record["status"] == "expired"
    assert record["error"] == "browser_job_missing"


def test_browser_recovery_rechecks_runtime_before_expiring_a_new_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    store = CredentialProfileStore(paths, "trusted_owner")
    profile = store.resolve("lark")
    browser_job_id = _browser_job_id("trusted_owner", "lark", profile["profile_id"])
    flow_store = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault)
    flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=profile["profile_id"],
        purpose="lark_full_authorization",
        phase=LARK_APP_CONFIGURATION_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision="missing",
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={"verification_url": "https://open.feishu.cn/page/cli?user_code=RACE"},
        secret=None,
        expires_at=None,
    )
    store.begin_browser_job(profile["profile_id"], browser_job_id, _LARK_STATE.fingerprint)
    calls = 0
    watchers = []

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def list_managed_browser_auth_jobs(self, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return []
            return [
                {
                    "provider": "lark",
                    "profile_id": profile["profile_id"],
                    "browser_job_id": browser_job_id,
                    "adapter_id": "lark-cli",
                    "authorization_contract_fingerprint": _LARK_AUTH_CONTRACT,
                    "credential_state_fingerprint": _LARK_STATE.fingerprint,
                }
            ]

    monkeypatch.setattr(
        ManagedCliService,
        "_start_browser_watcher",
        lambda self, **kwargs: watchers.append(kwargs),
    )
    ManagedCliService(Backend(), paths=paths)

    recovered = store.resolve("lark")
    assert recovered["browser_job_id"] == browser_job_id
    assert flow_store.active("lark", profile["profile_id"])["status"] == "awaiting_user"
    assert watchers[0]["browser_job_id"] == browser_job_id


def test_flow_recovery_resets_user_attempt_when_continuation_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    store = CredentialProfileStore(paths, "trusted_owner")
    profile = store.resolve("lark")
    staged = _credential_archive(b'{"bot":"ready","user":null}')
    flow_store = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault)
    app_flow = flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=profile["profile_id"],
        purpose="lark_full_authorization",
        phase=LARK_APP_CONFIGURATION_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision="missing",
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={},
        secret=None,
        expires_at=None,
    )
    flow_store.write_staged_state(app_flow, staged)
    flow_store.mark_phase_verified("lark", profile["profile_id"], LARK_APP_CONFIGURATION_PHASE.phase_id)
    user_flow = flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=profile["profile_id"],
        purpose="lark_full_authorization",
        phase=LARK_USER_CONSENT_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision="missing",
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={"verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify"},
        secret={"device_code": "secret"},
        expires_at=None,
    )
    for path in flow_store._secret_paths(user_flow["flow_id"]):
        path.unlink()

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def list_managed_browser_auth_jobs(self, **_kwargs):
            return []

    ManagedCliService(Backend(), paths=paths)

    recovered = flow_store.active("lark", profile["profile_id"])
    assert recovered["status"] == "starting"
    assert recovered["retry_user_consent"] is True
    assert recovered["error"] == "authorization_continuation_missing"
    assert recovered["public"] == {}
    assert flow_store.read_staged_state(recovered) == staged


@_OBSOLETE_LARK_ARCHIVE_TEST
def test_lark_two_phase_flow_stages_then_atomically_commits_without_leaking_device_code(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    old_state = _credential_archive(b'{"generation":"old"}')
    app_state = _credential_archive(b'{"generation":"app"}')
    final_state = _credential_archive(b'{"generation":"user"}')
    device_code = "SECRET-DEVICE-CANARY-1234"
    browser_job_id = _browser_job_id("trusted_owner", "lark", "lark_default")
    browser_collected = threading.Event()

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def __init__(self):
            self.calls = []

        def run_managed_browser_auth_cli(self, **_kwargs):
            return ManagedProviderExecutionResult(
                "https://open.feishu.cn/page/cli?user_code=APP-CODE\n" + "\n".join(["▀▄█ " * 12] * 12),
                0,
                None,
                browser_status="awaiting_user_browser",
                browser_job_id=browser_job_id,
            )

        def collect_managed_browser_auth_cli(self, **_kwargs):
            return ManagedProviderExecutionResult(
                "configured",
                0,
                app_state,
                browser_status="completed",
                browser_job_id=browser_job_id,
            )

        def finalize_managed_browser_auth_cli(self, **_kwargs):
            browser_collected.set()
            return True

        def run_managed_provider_cli(self, **kwargs):
            self.calls.append(kwargs)
            argv = kwargs["argv"]
            if argv == ["lark-cli", "auth", "status", "--json", "--verify"]:
                full = any(call.get("continuation_secret") for call in self.calls)
                identities = {
                    "bot": {"status": "ready", "verified": True, "appName": "PuddingClaw"},
                    "user": (
                        {
                            "status": "ready",
                            "verified": True,
                            "tokenStatus": "valid",
                            "userName": "Pet",
                            "openId": "ou_test",
                        }
                        if full
                        else {"status": "not_configured", "verified": False}
                    ),
                }
                return ManagedProviderExecutionResult(json.dumps({"identities": identities}), 0, None)
            if argv[:3] == ["lark-cli", "auth", "qrcode"]:
                return ManagedProviderExecutionResult("\n".join(["▀▄█ " * 12] * 12), 0, None)
            if argv == ["lark-cli", "auth", "login"]:
                assert kwargs["continuation_secret"] == device_code.encode()
                return ManagedProviderExecutionResult("authorized", 0, final_state)
            assert argv[:3] == ["lark-cli", "auth", "login"] and "--no-wait" in argv
            return ManagedProviderExecutionResult(
                json.dumps(
                    {
                        "device_code": device_code,
                        "user_code": "USER-CODE",
                        "verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify",
                        "expires_in": 600,
                    }
                ),
                0,
                app_state,
            )

    backend = Backend()
    service = ManagedCliService(backend, paths=paths)
    config_match = ManagedCliRegistry().match("lark-cli config init --new")
    first = service.plan(config_match, {})
    executable = first.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    store.write_state("lark", first.profile_id, old_state, credential_state=_LARK_STATE)
    store.update_status(first.profile_id, "active")

    phase_one = service.execute(service.plan(config_match, {}), {})
    assert phase_one.payload["authorization_request"]["phase"]["step"] == 1
    assert browser_collected.wait(timeout=3)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if store.resolve("lark")["status"] == "awaiting_user_authorization":
            break
        time.sleep(0.05)
    assert store.read_state("lark", first.profile_id, credential_state=_LARK_STATE) == old_state

    consent_match = ManagedCliRegistry().match("lark-cli auth login --domain all --no-wait --json")
    phase_two = service.execute(service.plan(consent_match, {}), {})
    assert phase_two.payload["authorization_request"]["phase"]["step"] == 2
    assert device_code not in phase_two.content
    assert (
        device_code.encode()
        not in (paths.credentials_root("trusted_owner") / "authorization-flows" / "flows.json").read_bytes()
    )
    for path in (paths.credentials_root("trusted_owner") / "authorization-flows").glob("*.enc"):
        assert device_code.encode() not in path.read_bytes()
    assert store.read_state("lark", first.profile_id, credential_state=_LARK_STATE) == old_state

    resume_match = ManagedCliRegistry().match("lark-cli auth resume")
    original_write_state = CredentialProfileStore.write_state

    def crash_before_profile_commit(self, provider, profile_id, payload, *, credential_state):
        if payload == final_state:
            raise OSError("simulated crash before Profile Vault commit")
        return original_write_state(
            self,
            provider,
            profile_id,
            payload,
            credential_state=credential_state,
        )

    monkeypatch.setattr(CredentialProfileStore, "write_state", crash_before_profile_commit)
    interrupted = service.execute(service.plan(resume_match, {}), {})
    assert interrupted.payload["error"] == "managed_authorization_failed"
    assert store.read_state("lark", first.profile_id, credential_state=_LARK_STATE) == old_state
    recovering = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault).active("lark", first.profile_id)
    assert recovering["status"] == "verifying"

    monkeypatch.setattr(CredentialProfileStore, "write_state", original_write_state)
    original_complete = AuthorizationFlowStore.complete
    monkeypatch.setattr(
        AuthorizationFlowStore,
        "complete",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("simulated crash after Profile Vault commit")),
    )
    committed_but_unacked = service.execute(service.plan(resume_match, {}), {})
    assert committed_but_unacked.payload["error"] == "managed_authorization_failed"
    assert store.read_state("lark", first.profile_id, credential_state=_LARK_STATE) == final_state

    monkeypatch.setattr(AuthorizationFlowStore, "complete", original_complete)
    completed = service.execute(service.plan(resume_match, {}), {})
    assert completed.payload["authorization_completed"] is True
    assert device_code not in completed.content
    assert store.read_state("lark", first.profile_id, credential_state=_LARK_STATE) == final_state
    assert all(device_code not in " ".join(call["argv"]) for call in backend.calls)
    assert sum(bool(call.get("continuation_secret")) for call in backend.calls) == 1


def test_resume_without_active_flow_never_uses_old_valid_token_as_success(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def __init__(self):
            self.calls = []

        def run_managed_provider_cli(self, **kwargs):
            self.calls.append(kwargs)
            return ManagedProviderExecutionResult(
                json.dumps(
                    {
                        "identities": {
                            "bot": {"status": "ready", "verified": True},
                            "user": {
                                "status": "ready",
                                "verified": True,
                                "tokenStatus": "valid",
                            },
                        }
                    }
                ),
                0,
                None,
            )

    backend = Backend()
    service = ManagedCliService(backend, paths=paths)
    match = ManagedCliRegistry().match("lark-cli auth resume")
    plan = service.plan(match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    store.write_state("lark", plan.profile_id, _credential_archive(b'{"old":true}'), credential_state=_LARK_STATE)
    store.update_status(plan.profile_id, "active")

    result = service.execute(service.plan(match, {}), {})

    assert result.payload["error"] == "authorization_flow_missing"
    assert result.payload.get("authorization_completed") is not True
    assert backend.calls == []


@_OBSOLETE_LARK_ARCHIVE_TEST
def test_invalid_resume_resets_only_user_attempt_without_damaging_profile_or_step_one(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    old_state = _credential_archive(b'{"user":"old-valid"}')
    staged_state = _credential_archive(b'{"user":null}')

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def run_managed_provider_cli(self, **_kwargs):
            return ManagedProviderExecutionResult(
                json.dumps(
                    {
                        "ok": False,
                        "identity": "user",
                        "error": {
                            "type": "oauth",
                            "subtype": "invalid_request",
                            "code": 20001,
                            "message": "invalid device authorization request",
                        },
                    }
                ),
                1,
                None,
            )

    backend = Backend()
    service = ManagedCliService(backend, paths=paths)
    resume_match = ManagedCliRegistry().match("lark-cli auth resume")
    initial_plan = service.plan(resume_match, {})
    executable = initial_plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    store.write_state("lark", initial_plan.profile_id, old_state, credential_state=_LARK_STATE)
    store.update_status(initial_plan.profile_id, "active")
    profile = store.resolve("lark")
    base_revision = store.state_revision("lark", initial_plan.profile_id)
    flow_store = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault)
    flow = flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=initial_plan.profile_id,
        purpose="lark_user_reauthorization",
        phase=LARK_APP_CONFIGURATION_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision=base_revision,
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={},
        secret=None,
        expires_at=None,
    )
    flow_store.write_staged_state(flow, staged_state)
    flow_store.mark_phase_verified("lark", initial_plan.profile_id, LARK_APP_CONFIGURATION_PHASE.phase_id)
    flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=initial_plan.profile_id,
        purpose="lark_user_reauthorization",
        phase=LARK_USER_CONSENT_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision=base_revision,
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={
            "verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify",
            "user_code": "BAD-CODE",
        },
        secret={"device_code": "BAD-DEVICE"},
        expires_at=time.time() + 600,
    )

    result = service.execute(service.plan(resume_match, {}), {})

    assert result.payload["status"] == "failed"
    assert result.payload["reason"] == "provider_invalid_request"
    assert result.payload["authorization_completed"] is False
    assert result.payload["authorization_request"]["status"] == "failed"
    assert store.read_state("lark", initial_plan.profile_id, credential_state=_LARK_STATE) == old_state
    current = store.resolve("lark")
    assert current["status"] == "active"
    assert current.get("identities", {}).get("user") is None
    active = flow_store.active("lark", initial_plan.profile_id)
    assert active is not None
    assert active["status"] == "starting"
    assert active["retry_user_consent"] is True
    assert LARK_APP_CONFIGURATION_PHASE.phase_id in active["completed_phase_ids"]
    assert flow_store.read_staged_state(active) == staged_state


@_OBSOLETE_LARK_ARCHIVE_TEST
def test_nonzero_resume_with_independently_verified_candidate_commits(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    old_state = _credential_archive(b'{"user":"old"}')
    app_state = _credential_archive(b'{"user":null}')
    candidate_state = _credential_archive(b'{"user":"new"}')
    canary = "SECRET-DEVICE-OR-TOKEN-CANARY"

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def __init__(self):
            self.calls = []

        def run_managed_provider_cli(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["argv"] == ["lark-cli", "auth", "login"]:
                return ManagedProviderExecutionResult(
                    json.dumps(
                        {
                            "ok": False,
                            "error": {"subtype": "access_denied", "message": canary},
                            "accessToken": canary,
                        }
                    ),
                    1,
                    candidate_state,
                )
            assert kwargs["argv"] == ["lark-cli", "auth", "status", "--json", "--verify"]
            assert kwargs["credential_state"] == candidate_state
            return ManagedProviderExecutionResult(
                json.dumps(
                    {
                        "identities": {
                            "bot": {"status": "ready", "verified": True},
                            "user": {
                                "status": "ready",
                                "verified": True,
                                "tokenStatus": "valid",
                            },
                        }
                    }
                ),
                0,
                candidate_state,
            )

    backend = Backend()
    service = ManagedCliService(backend, paths=paths)
    resume_match = ManagedCliRegistry().match("lark-cli auth resume")
    plan = service.plan(resume_match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    store.write_state("lark", plan.profile_id, old_state, credential_state=_LARK_STATE)
    store.update_status(plan.profile_id, "active")
    flow_store, _ = _seed_lark_user_flow(paths, store, plan.profile_id, app_state, device_code=canary)

    result = service.execute(service.plan(resume_match, {}), {})

    assert result.payload["authorization_completed"] is True
    assert canary not in result.content
    assert store.read_state("lark", plan.profile_id, credential_state=_LARK_STATE) == candidate_state
    assert flow_store.active("lark", plan.profile_id) is None
    assert sum(call["argv"] == ["lark-cli", "auth", "login"] for call in backend.calls) == 1


@_OBSOLETE_LARK_ARCHIVE_TEST
def test_unknown_resume_failure_preserves_same_attempt_and_step_one(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    old_state = _credential_archive(b'{"user":"old"}')
    app_state = _credential_archive(b'{"user":null}')

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def run_managed_provider_cli(self, **_kwargs):
            return ManagedProviderExecutionResult(
                json.dumps({"ok": False, "error": {"type": "internal", "message": "unknown"}}),
                1,
                None,
            )

    service = ManagedCliService(Backend(), paths=paths)
    resume_match = ManagedCliRegistry().match("lark-cli auth resume")
    plan = service.plan(resume_match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    store.write_state("lark", plan.profile_id, old_state, credential_state=_LARK_STATE)
    store.update_status(plan.profile_id, "active")
    flow_store, original = _seed_lark_user_flow(
        paths,
        store,
        plan.profile_id,
        app_state,
        device_code="PRESERVED-DEVICE",
    )

    result = service.execute(service.plan(resume_match, {}), {})

    assert result.payload["retryable"] is True
    assert result.payload["reason"] == "provider_authorization_error"
    active = flow_store.active("lark", plan.profile_id)
    assert active["flow_id"] == original["flow_id"]
    assert active["status"] == "awaiting_user"
    assert flow_store.read_secret(active)["device_code"] == "PRESERVED-DEVICE"
    assert flow_store.read_staged_state(active) == app_state
    assert store.read_state("lark", plan.profile_id, credential_state=_LARK_STATE) == old_state


@_OBSOLETE_LARK_ARCHIVE_TEST
def test_pending_resume_with_exported_baseline_does_not_reset_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    old_state = _credential_archive(b'{"user":"old"}')
    app_state = _credential_archive(b'{"user":null}')

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def __init__(self):
            self.login_calls = 0
            self.verify_calls = 0

        def run_managed_provider_cli(self, **kwargs):
            if kwargs["argv"] == ["lark-cli", "auth", "login"]:
                self.login_calls += 1
                return ManagedProviderExecutionResult(
                    json.dumps({"ok": False, "error": {"subtype": "authorization_pending"}}),
                    1,
                    app_state,
                )
            self.verify_calls += 1
            if self.verify_calls == 1:
                return ManagedProviderExecutionResult(
                    json.dumps({"ok": False, "error": {"code": 429, "message": "rate limit"}}),
                    1,
                    app_state,
                )
            return ManagedProviderExecutionResult(
                json.dumps(
                    {
                        "identities": {
                            "bot": {"status": "ready", "verified": True},
                            "user": {"status": "not_configured", "verified": False},
                        }
                    }
                ),
                0,
                app_state,
            )

    backend = Backend()
    service = ManagedCliService(backend, paths=paths)
    resume_match = ManagedCliRegistry().match("lark-cli auth resume")
    plan = service.plan(resume_match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    store.write_state("lark", plan.profile_id, old_state, credential_state=_LARK_STATE)
    store.update_status(plan.profile_id, "active")
    flow_store, original = _seed_lark_user_flow(paths, store, plan.profile_id, app_state)

    collecting = service.execute(service.plan(resume_match, {}), {})
    assert collecting.payload["retryable"] is True
    assert flow_store.active("lark", plan.profile_id)["status"] == "collecting"

    result = service.execute(service.plan(resume_match, {}), {})

    assert result.payload["reason"] == "authorization_pending"
    active = flow_store.active("lark", plan.profile_id)
    assert active["flow_id"] == original["flow_id"]
    assert active["attempt"] == original["attempt"]
    assert active["status"] == "awaiting_user"
    assert flow_store.read_candidate_state(active) is None
    assert flow_store.read_staged_state(active) == app_state
    assert store.read_state("lark", plan.profile_id, credential_state=_LARK_STATE) == old_state
    assert backend.login_calls == 1
    assert backend.verify_calls == 2


def test_candidate_state_is_cryptographically_bound_to_one_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    old_state = _credential_archive(b'{"user":"old"}')
    app_state = _credential_archive(b'{"user":null}')
    candidate_state = _credential_archive(b'{"user":"candidate"}')
    store = CredentialProfileStore(paths, "trusted_owner")
    profile = store.create_profile("lark", "lark_default", "Lark Default")
    store.write_state("lark", profile["profile_id"], old_state, credential_state=_LARK_STATE)
    flow_store, active = _seed_lark_user_flow(paths, store, profile["profile_id"], app_state)
    flow_store.write_candidate_state(active, candidate_state)
    flow_store.record_retryable_user_error(
        "lark",
        profile["profile_id"],
        error="provider_retryable_error",
        diagnostic={"reason": "provider_retryable_error", "exit_code": 124},
    )
    active = flow_store.active("lark", profile["profile_id"])
    assert active is not None
    assert flow_store.read_candidate_state(active) == candidate_state

    renewed = flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=profile["profile_id"],
        purpose="lark_user_reauthorization",
        phase=LARK_USER_CONSENT_PHASE,
        profile_revision=float(store.resolve("lark")["updated_at"]),
        base_state_revision=str(active["base_state_revision"]),
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={"verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify"},
        secret={"device_code": "NEW-DEVICE"},
        expires_at=time.time() + 600,
        new_attempt=True,
    )

    assert renewed["attempt"] == active["attempt"] + 1
    assert "error" not in renewed
    assert "last_user_attempt" not in renewed
    assert flow_store.read_candidate_state(renewed) is None
    assert flow_store.read_secret(active)["device_code"] == "DEVICE-CODE"
    assert flow_store.read_secret(renewed)["device_code"] == "NEW-DEVICE"


@_OBSOLETE_LARK_ARCHIVE_TEST
def test_verifying_recovery_rebinds_digest_after_crash_between_state_and_manifest(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    old_state = _credential_archive(b'{"user":"old"}')
    app_state = _credential_archive(b'{"user":null}')
    first_candidate = _credential_archive(b'{"user":"first"}')
    refreshed_candidate = _credential_archive(b'{"user":"refreshed"}')

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def run_managed_provider_cli(self, **kwargs):
            assert kwargs["argv"] == ["lark-cli", "auth", "status", "--json", "--verify"]
            return ManagedProviderExecutionResult(
                json.dumps(
                    {
                        "identities": {
                            "bot": {"status": "ready", "verified": True},
                            "user": {
                                "status": "ready",
                                "verified": True,
                                "tokenStatus": "valid",
                            },
                        }
                    }
                ),
                0,
                refreshed_candidate,
            )

    service = ManagedCliService(Backend(), paths=paths)
    resume_match = ManagedCliRegistry().match("lark-cli auth resume")
    plan = service.plan(resume_match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    store.write_state("lark", plan.profile_id, old_state, credential_state=_LARK_STATE)
    store.update_status(plan.profile_id, "active")
    flow_store, active = _seed_lark_user_flow(paths, store, plan.profile_id, app_state)
    flow_store.write_staged_state(active, first_candidate)
    flow_store.mark_verifying(
        "lark",
        plan.profile_id,
        staged_sha256=hashlib.sha256(first_candidate).hexdigest(),
    )

    original_mark_verifying = AuthorizationFlowStore.mark_verifying
    monkeypatch.setattr(
        AuthorizationFlowStore,
        "mark_verifying",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("crash after staged write")),
    )
    interrupted = service.execute(service.plan(resume_match, {}), {})
    assert interrupted.payload["error"] == "managed_authorization_failed"
    recovering = flow_store.active("lark", plan.profile_id)
    assert flow_store.read_staged_state(recovering) == refreshed_candidate
    assert recovering["commit_staged_sha256"] == hashlib.sha256(first_candidate).hexdigest()

    monkeypatch.setattr(AuthorizationFlowStore, "mark_verifying", original_mark_verifying)
    completed = service.execute(service.plan(resume_match, {}), {})

    assert completed.payload["authorization_completed"] is True
    assert store.read_state("lark", plan.profile_id, credential_state=_LARK_STATE) == refreshed_candidate


@_OBSOLETE_LARK_ARCHIVE_TEST
def test_candidate_verify_transient_failure_is_recoverable_without_reusing_device_code(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    old_state = _credential_archive(b'{"user":"old"}')
    app_state = _credential_archive(b'{"user":null}')
    candidate_state = _credential_archive(b'{"user":"new"}')

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def __init__(self):
            self.login_calls = 0
            self.verify_calls = 0

        def run_managed_provider_cli(self, **kwargs):
            if kwargs["argv"] == ["lark-cli", "auth", "login"]:
                self.login_calls += 1
                return ManagedProviderExecutionResult("authorized", 0, candidate_state)
            assert kwargs["argv"] == ["lark-cli", "auth", "status", "--json", "--verify"]
            self.verify_calls += 1
            assert kwargs["credential_state"] == candidate_state
            if self.verify_calls == 1:
                return ManagedProviderExecutionResult(
                    json.dumps({"ok": False, "error": {"code": 429, "message": "rate limit"}}),
                    1,
                    candidate_state,
                )
            return ManagedProviderExecutionResult(
                json.dumps(
                    {
                        "identities": {
                            "bot": {"status": "ready", "verified": True},
                            "user": {
                                "status": "ready",
                                "verified": True,
                                "tokenStatus": "valid",
                            },
                        }
                    }
                ),
                0,
                candidate_state,
            )

    backend = Backend()
    service = ManagedCliService(backend, paths=paths)
    resume_match = ManagedCliRegistry().match("lark-cli auth resume")
    plan = service.plan(resume_match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    store.write_state("lark", plan.profile_id, old_state, credential_state=_LARK_STATE)
    store.update_status(plan.profile_id, "active")
    flow_store, _ = _seed_lark_user_flow(paths, store, plan.profile_id, app_state)

    first = service.execute(service.plan(resume_match, {}), {})

    assert first.payload["retryable"] is True
    active = flow_store.active("lark", plan.profile_id)
    assert active["status"] == "collecting"
    assert flow_store.read_staged_state(active) == app_state
    assert flow_store.read_candidate_state(active) == candidate_state
    assert store.read_state("lark", plan.profile_id, credential_state=_LARK_STATE) == old_state

    completed = service.execute(service.plan(resume_match, {}), {})

    assert completed.payload["authorization_completed"] is True
    assert backend.login_calls == 1
    assert backend.verify_calls == 2
    assert store.read_state("lark", plan.profile_id, credential_state=_LARK_STATE) == candidate_state


@_OBSOLETE_LARK_ARCHIVE_TEST
def test_explicit_user_reauthorization_clears_only_staged_old_login(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    old_state = _credential_archive(b'{"user":"old"}')
    bot_only_state = _credential_archive(b'{"user":null}')

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def __init__(self):
            self.calls = []

        def run_managed_provider_cli(self, **kwargs):
            self.calls.append(kwargs)
            argv = kwargs["argv"]
            if argv == ["lark-cli", "auth", "status", "--json", "--verify"]:
                return ManagedProviderExecutionResult(
                    json.dumps(
                        {
                            "identities": {
                                "bot": {"status": "ready", "verified": True},
                                "user": {
                                    "status": "ready",
                                    "verified": True,
                                    "tokenStatus": "valid",
                                },
                            }
                        }
                    ),
                    0,
                    kwargs["credential_state"],
                )
            if argv == ["lark-cli", "auth", "logout", "--json"]:
                assert kwargs["credential_state"] == old_state
                return ManagedProviderExecutionResult("logged out", 0, bot_only_state)
            if argv[:3] == ["lark-cli", "auth", "login"]:
                assert kwargs["credential_state"] == bot_only_state
                return ManagedProviderExecutionResult(
                    json.dumps(
                        {
                            "device_code": "NEW-DEVICE-CODE",
                            "user_code": "NEW-CODE",
                            "verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify",
                            "expires_in": 600,
                        }
                    ),
                    0,
                    bot_only_state,
                )
            if argv[:3] == ["lark-cli", "auth", "qrcode"]:
                return ManagedProviderExecutionResult("\n".join(["▀▄█ " * 12] * 12), 0, None)
            raise AssertionError(argv)

    backend = Backend()
    service = ManagedCliService(backend, paths=paths)
    match = ManagedCliRegistry().match("lark-cli auth login --domain all --no-wait --json")
    plan = service.plan(match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    store.write_state("lark", plan.profile_id, old_state, credential_state=_LARK_STATE)
    store.update_status(plan.profile_id, "active")
    profile = store.resolve("lark")
    flow_store = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault)
    stale = flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=plan.profile_id,
        purpose="lark_user_reauthorization",
        phase=LARK_APP_CONFIGURATION_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision=store.state_revision("lark", plan.profile_id),
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={},
        secret=None,
        expires_at=None,
    )
    flow_store.write_staged_state(stale, old_state)
    flow_store.mark_phase_verified("lark", plan.profile_id, LARK_APP_CONFIGURATION_PHASE.phase_id)
    flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=plan.profile_id,
        purpose="lark_user_reauthorization",
        phase=LARK_USER_CONSENT_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision=store.state_revision("lark", plan.profile_id),
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={"verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify"},
        secret={"device_code": "STALE-DEVICE"},
        expires_at=time.time() - 1,
    )

    stale_flow_id = flow_store.active("lark", plan.profile_id)["flow_id"]
    result = service.execute(service.plan(match, {}), {})

    assert result.payload["status"] == "awaiting_user_browser"
    assert result.payload["authorization_request"]["flow_id"] == stale_flow_id
    assert result.payload["authorization_request"]["attempt"] == 2
    assert result.payload["authorization_request"]["phase"]["step"] == 1
    assert result.payload["authorization_request"]["phase"]["total"] == 1
    assert store.read_state("lark", plan.profile_id, credential_state=_LARK_STATE) == old_state
    profile = store.resolve("lark")
    assert profile["status"] == "active"
    assert [call["argv"] for call in backend.calls].count(["lark-cli", "auth", "logout", "--json"]) == 1
    logout = next(call for call in backend.calls if call["argv"] == ["lark-cli", "auth", "logout", "--json"])
    assert logout["network_enabled"] is False


@_OBSOLETE_LARK_ARCHIVE_TEST
def test_user_reauthorization_supersedes_incomplete_accidental_full_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    old_state = _credential_archive(b'{"user":"expired","bot":"ready"}')
    bot_only_state = _credential_archive(b'{"user":null,"bot":"ready"}')
    browser_job_id = _browser_job_id("trusted_owner", "lark", "lark_default")

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def __init__(self):
            self.calls = []
            self.finalized_jobs = []

        def finalize_managed_browser_auth_cli(self, **kwargs):
            self.finalized_jobs.append(kwargs)
            return True

        def run_managed_provider_cli(self, **kwargs):
            self.calls.append(kwargs)
            argv = kwargs["argv"]
            if argv == ["lark-cli", "auth", "logout", "--json"]:
                assert kwargs["credential_state"] == old_state
                return ManagedProviderExecutionResult("logged out", 0, bot_only_state)
            if argv[:3] == ["lark-cli", "auth", "login"]:
                assert kwargs["credential_state"] == bot_only_state
                return ManagedProviderExecutionResult(
                    json.dumps(
                        {
                            "device_code": "USER-REAUTH-DEVICE",
                            "user_code": "USER-REAUTH-CODE",
                            "verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify",
                            "expires_in": 600,
                        }
                    ),
                    0,
                    bot_only_state,
                )
            if argv[:3] == ["lark-cli", "auth", "qrcode"]:
                return ManagedProviderExecutionResult("\n".join(["▀▄█ " * 12] * 12), 0, None)
            raise AssertionError(argv)

    backend = Backend()
    service = ManagedCliService(backend, paths=paths)
    match = ManagedCliRegistry().match("lark-cli auth login --domain all --no-wait --json")
    plan = service.plan(match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    store.write_state("lark", plan.profile_id, old_state, credential_state=_LARK_STATE)
    store.update_status(plan.profile_id, "active")
    store.update_identity_status(plan.profile_id, "bot", "ready")
    store.update_identity_status(plan.profile_id, "user", "authorization_required")
    profile = store.resolve("lark")
    flow_store = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault)
    accidental = flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=plan.profile_id,
        purpose="lark_full_authorization",
        phase=LARK_APP_CONFIGURATION_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision=store.state_revision("lark", plan.profile_id),
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={"verification_url": "https://open.feishu.cn/page/cli?user_code=OLD"},
        secret=None,
        expires_at=None,
    )
    store.begin_browser_job(plan.profile_id, browser_job_id, _LARK_STATE.fingerprint)

    result = service.execute(service.plan(match, {}), {})

    assert result.payload["status"] == "awaiting_user_browser"
    request = result.payload["authorization_request"]
    assert request["phase"]["step"] == 1
    assert request["phase"]["total"] == 1
    assert request["purpose"] == "lark_user_reauthorization"
    replacement = flow_store.active("lark", plan.profile_id)
    assert replacement["flow_id"] != accidental["flow_id"]
    assert replacement["purpose"] == "lark_user_reauthorization"
    old_record = next(item for item in flow_store._read_registry()["flows"] if item["flow_id"] == accidental["flow_id"])
    assert old_record["status"] == "cancelled"
    assert old_record["cancel_reason"] == "superseded_by_user_reauthorization"
    assert backend.finalized_jobs[0]["browser_job_id"] == browser_job_id
    assert store.resolve("lark").get("browser_job_id") is None
    assert store.read_state("lark", plan.profile_id, credential_state=_LARK_STATE) == old_state


@_OBSOLETE_LARK_ARCHIVE_TEST
def test_full_replacement_supersedes_user_flow_without_deleting_old_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    old_state = _credential_archive(b'{"user":"old-valid"}')
    browser_job_id = _browser_job_id("trusted_owner", "lark", "lark_default")

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def run_managed_browser_auth_cli(self, **_kwargs):
            return ManagedProviderExecutionResult(
                "https://open.feishu.cn/page/cli?user_code=REPLACE\n" + "\n".join(["▀▄█ " * 12] * 12),
                0,
                None,
                browser_status="awaiting_user_browser",
                browser_job_id=browser_job_id,
            )

        def collect_managed_browser_auth_cli(self, **_kwargs):
            return ManagedProviderExecutionResult(
                "waiting",
                0,
                None,
                browser_status="awaiting_user_browser",
                browser_job_id=browser_job_id,
            )

    service = ManagedCliService(Backend(), paths=paths)
    monkeypatch.setattr(service, "_start_browser_watcher", lambda **_kwargs: None)
    match = ManagedCliRegistry().match("lark-cli config init --new")
    plan = service.plan(match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    store.write_state("lark", plan.profile_id, old_state, credential_state=_LARK_STATE)
    store.update_status(plan.profile_id, "active")
    profile = store.resolve("lark")
    flow_store = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault)
    old_flow = flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=plan.profile_id,
        purpose="lark_user_reauthorization",
        phase=LARK_APP_CONFIGURATION_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision=store.state_revision("lark", plan.profile_id),
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={},
        secret=None,
        expires_at=None,
    )
    flow_store.write_staged_state(old_flow, old_state)

    result = service.execute(service.plan(match, {}), {})

    assert result.payload["status"] == "awaiting_user_browser"
    replacement = flow_store.active("lark", plan.profile_id)
    assert replacement["flow_id"] != old_flow["flow_id"]
    assert replacement["purpose"] == "lark_full_authorization"
    old_record = next(item for item in flow_store._read_registry()["flows"] if item["flow_id"] == old_flow["flow_id"])
    assert old_record["status"] == "cancelled"
    assert old_record["cancel_reason"] == "superseded_by_full_replacement"
    assert store.read_state("lark", plan.profile_id, credential_state=_LARK_STATE) == old_state
    current = store.resolve("lark")
    assert current["status"] == "active"
    assert current["browser_job_id"] == browser_job_id


@_OBSOLETE_LARK_ARCHIVE_TEST
def test_full_replacement_supersedes_same_purpose_flow_with_stale_base(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    first_state = _credential_archive(b'{"generation":"first"}')
    current_state = _credential_archive(b'{"generation":"current"}')
    browser_job_id = _browser_job_id("trusted_owner", "lark", "lark_default")

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def run_managed_browser_auth_cli(self, **_kwargs):
            return ManagedProviderExecutionResult(
                "https://open.feishu.cn/page/cli?user_code=FRESH\n" + "\n".join(["▀▄█ " * 12] * 12),
                0,
                None,
                browser_status="awaiting_user_browser",
                browser_job_id=browser_job_id,
            )

    service = ManagedCliService(Backend(), paths=paths)
    monkeypatch.setattr(service, "_start_browser_watcher", lambda **_kwargs: None)
    match = ManagedCliRegistry().match("lark-cli config init --new")
    plan = service.plan(match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    store.write_state("lark", plan.profile_id, first_state, credential_state=_LARK_STATE)
    profile = store.resolve("lark")
    flow_store = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault)
    old_flow = flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=plan.profile_id,
        purpose="lark_full_authorization",
        phase=LARK_USER_CONSENT_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision=store.state_revision("lark", plan.profile_id),
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={"verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify"},
        secret={"device_code": "OLD-DEVICE"},
        expires_at=time.time() + 600,
    )
    flow_store.write_staged_state(old_flow, first_state)
    # Simulate a separately committed Profile mutation after this Flow began.
    store.write_state("lark", plan.profile_id, current_state, credential_state=_LARK_STATE)

    result = service.execute(service.plan(match, {}), {})

    assert result.payload["status"] == "awaiting_user_browser"
    replacement = flow_store.active("lark", plan.profile_id)
    assert replacement["flow_id"] != old_flow["flow_id"]
    assert replacement["phase_id"] == LARK_APP_CONFIGURATION_PHASE.phase_id
    old_record = next(item for item in flow_store._read_registry()["flows"] if item["flow_id"] == old_flow["flow_id"])
    assert old_record["status"] == "cancelled"
    assert old_record["cancel_reason"] == "stale_base_state"
    assert store.read_state("lark", plan.profile_id, credential_state=_LARK_STATE) == current_state


def test_profile_revoke_cancels_active_flow_and_marks_both_identities(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    old_state = _credential_archive(b'{"user":"old-valid"}')
    revoked_state = _credential_archive(b"{}")

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def run_managed_provider_cli(self, **kwargs):
            assert kwargs["argv"] == ["lark-cli", "config", "remove"]
            return ManagedProviderExecutionResult("removed", 0, revoked_state)

    service = ManagedCliService(Backend(), paths=paths)
    match = ManagedCliRegistry().match("lark-cli config remove")
    plan = service.plan(match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    store.write_state("lark", plan.profile_id, old_state, credential_state=_LARK_STATE)
    store.update_status(plan.profile_id, "active")
    store.update_identity_status(plan.profile_id, "bot", "ready")
    store.update_identity_status(plan.profile_id, "user", "active")
    profile = store.resolve("lark")
    flow_store = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault)
    active = flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=plan.profile_id,
        purpose="lark_user_reauthorization",
        phase=LARK_APP_CONFIGURATION_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision=store.state_revision("lark", plan.profile_id),
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={},
        secret=None,
        expires_at=None,
    )
    flow_store.write_staged_state(active, old_state)

    current_plan = service.plan(match, {})
    result = service.execute(
        current_plan,
        {"_managed_cli_destructive_approval": current_plan.destructive_approval_binding()},
    )

    assert result.payload["status"] == "completed"
    assert flow_store.active("lark", plan.profile_id) is None
    profile = store.resolve("lark")
    assert profile["status"] == "revoked"
    assert profile["identities"]["bot"]["status"] == "revoked"
    assert profile["identities"]["user"]["status"] == "revoked"


def test_confirmed_profile_revoke_stops_active_browser_runner_before_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    old_state = _credential_archive(b'{"generation":"old"}')
    revoked_state = _credential_archive(b"{}")
    browser_job_id = _browser_job_id("trusted_owner", "lark", "lark_default")
    finalized = []

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def finalize_managed_browser_auth_cli(self, **kwargs):
            finalized.append(kwargs)
            return True

        def run_managed_provider_cli(self, **kwargs):
            assert finalized
            assert kwargs["argv"] == ["lark-cli", "config", "remove"]
            return ManagedProviderExecutionResult("removed", 0, revoked_state)

    service = ManagedCliService(Backend(), paths=paths)
    match = ManagedCliRegistry().match("lark-cli config remove")
    bootstrap_plan = service.plan(match, {})
    executable = bootstrap_plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    store.write_state("lark", bootstrap_plan.profile_id, old_state, credential_state=_LARK_STATE)
    store.update_status(bootstrap_plan.profile_id, "active")
    store.begin_browser_job(bootstrap_plan.profile_id, browser_job_id, _LARK_STATE.fingerprint)
    profile = store.resolve("lark")
    flow_store = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault)
    flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=bootstrap_plan.profile_id,
        purpose="lark_full_authorization",
        phase=LARK_APP_CONFIGURATION_PHASE,
        profile_revision=float(profile["updated_at"]),
        base_state_revision=store.state_revision("lark", bootstrap_plan.profile_id),
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={},
        secret=None,
        expires_at=None,
    )

    plan = service.plan(match, {})
    result = service.execute(
        plan,
        {"_managed_cli_destructive_approval": plan.destructive_approval_binding()},
    )

    assert result.payload["status"] == "completed"
    assert finalized[0]["browser_job_id"] == browser_job_id
    assert flow_store.active("lark", plan.profile_id) is None
    profile = store.resolve("lark")
    assert "browser_job_id" not in profile
    assert profile["last_browser_job_status"] == "cancelled"
    assert profile["status"] == "revoked"


@_OBSOLETE_LARK_ARCHIVE_TEST
def test_user_token_expiry_pauses_provider_command_and_starts_user_consent(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    old_state = _credential_archive(b'{"user":"expired"}')
    bot_only_state = _credential_archive(b'{"user":null}')

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def __init__(self):
            self.calls = []

        def run_managed_provider_cli(self, **kwargs):
            self.calls.append(kwargs)
            argv = kwargs["argv"]
            if argv[:3] == ["lark-cli", "calendar", "list"]:
                return ManagedProviderExecutionResult(
                    json.dumps(
                        {
                            "ok": False,
                            "identity": "user",
                            "error": {
                                "type": "authorization",
                                "subtype": "token_expired",
                                "message": "user token expired",
                            },
                        }
                    ),
                    1,
                    None,
                )
            if argv == ["lark-cli", "auth", "status", "--json", "--verify"]:
                return ManagedProviderExecutionResult(
                    json.dumps(
                        {
                            "identities": {
                                "bot": {"status": "ready", "verified": True},
                                "user": {"status": "expired", "verified": False},
                            }
                        }
                    ),
                    0,
                    kwargs["credential_state"],
                )
            if argv == ["lark-cli", "auth", "logout", "--json"]:
                return ManagedProviderExecutionResult("logged out", 0, bot_only_state)
            if argv[:3] == ["lark-cli", "auth", "login"]:
                return ManagedProviderExecutionResult(
                    json.dumps(
                        {
                            "device_code": "REPAIR-DEVICE-CODE",
                            "user_code": "REPAIR",
                            "verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify",
                            "expires_in": 600,
                        }
                    ),
                    0,
                    bot_only_state,
                )
            if argv[:3] == ["lark-cli", "auth", "qrcode"]:
                return ManagedProviderExecutionResult("\n".join(["▀▄█ " * 12] * 12), 0, None)
            raise AssertionError(argv)

    backend = Backend()
    service = ManagedCliService(backend, paths=paths)
    match = ManagedCliRegistry().match("lark-cli calendar list --as user")
    plan = service.plan(match, {})
    executable = plan.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    store.write_state("lark", plan.profile_id, old_state, credential_state=_LARK_STATE)
    store.update_status(plan.profile_id, "active")

    result = service.execute(service.plan(match, {}), {})

    assert result.payload["status"] == "awaiting_user_browser"
    assert result.payload["trigger"]["reason"] == "user_token_expired"
    assert result.payload["trigger"]["identity"] == "user"
    assert result.payload["trigger"]["safe_to_retry"] is True
    profile = store.resolve("lark")
    assert profile["status"] == "active"
    assert profile["identities"]["user"]["status"] == "authorization_required"
    assert (
        AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault).active("lark", plan.profile_id)["status"]
        == "awaiting_user"
    )
    assert store.read_state("lark", plan.profile_id, credential_state=_LARK_STATE) == old_state


def test_browser_collection_is_two_phase_and_url_is_provider_scoped(monkeypatch):
    manager = ProjectSandboxManager({})
    calls: list[list[str]] = []
    archive = _credential_archive(b'{"configured":true}')
    job_id = manager._managed_browser_job_id("local", "lark", "lark_default")

    def fake_run(args, *, timeout=30):
        calls.append(list(args))
        if args[:2] == ["inspect", "--format"] and "State.Running" in args[2]:
            return SimpleNamespace(
                returncode=0,
                stdout=f"true {_LARK_STATE.fingerprint} lark-cli {_LARK_AUTH_CONTRACT}\n",
                stderr="",
            )
        if args[:3] == ["exec", args[1], "cat"] and args[-1].endswith("-output"):
            return SimpleNamespace(
                returncode=0,
                stdout="https://open.feishu.cn/page/cli?user_code=ABCD\n",
                stderr="",
            )
        if args[:3] == ["exec", args[1], "cat"] and args[-1].endswith("-exit"):
            return SimpleNamespace(returncode=0, stdout="0", stderr="")
        if args[:2] == ["inspect", "--format"]:
            return SimpleNamespace(returncode=0, stdout=f"{job_id}\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_run", fake_run)
    monkeypatch.setattr(
        manager,
        "_run_bytes",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=archive, stderr=b""),
    )

    prepared = manager.collect_managed_browser_auth_cli(
        owner_user_id="local",
        provider="lark",
        profile_id="lark_default",
        credential_state_spec=_LARK_STATE,
        adapter_id="lark-cli",
        authorization_contract_fingerprint=_LARK_AUTH_CONTRACT,
    )

    assert prepared.browser_status == "completed"
    assert prepared.credential_state == archive
    stale_contract = manager.collect_managed_browser_auth_cli(
        owner_user_id="local",
        provider="lark",
        profile_id="lark_default",
        credential_state_spec=_LARK_STATE,
        adapter_id="lark-cli",
        authorization_contract_fingerprint="0" * 64,
    )
    assert stale_contract.browser_status == "missing"
    assert not any(call[:2] == ["rm", "-f"] for call in calls)
    assert not any(call[-2:] == ["touch", "/tmp/puddingclaw-browser-collected"] for call in calls)
    assert manager.finalize_managed_browser_auth_cli(
        owner_user_id="local",
        provider="lark",
        profile_id="lark_default",
        browser_job_id=job_id,
    )
    assert any(call[:2] == ["rm", "-f"] for call in calls)
    assert (
        _lark_config_verification_url("\x1b[32mhttps://open.feishu.cn/page/cli?user_code=ABCD\x1b[0m")
        == "https://open.feishu.cn/page/cli?user_code=ABCD"
    )
    assert _lark_config_verification_url("https://evil.invalid/page/cli?code=ABCD") is None


def test_browser_vault_failure_keeps_job_unacked_for_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_OWNER_USER_ID", "trusted_owner")
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    archive = _credential_archive(b'{"configured":true}')

    class Backend(_RuntimeImageBackend):
        manager = SimpleNamespace(runtime_contract="test")

        def __init__(self):
            self.finalized = False

        def collect_managed_browser_auth_cli(self, **_kwargs):
            return ManagedProviderExecutionResult(
                "configured",
                0,
                archive,
                browser_status="completed",
                browser_job_id="job-keep",
            )

        def finalize_managed_browser_auth_cli(self, **_kwargs):
            self.finalized = True
            return True

    backend = Backend()
    service = ManagedCliService(backend, paths=paths)
    match = ManagedCliRegistry().match("lark-cli config init --new")
    first = service.plan(match, {})
    executable = first.toolchain_path / "bin" / "lark-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("test", encoding="utf-8")
    store = CredentialProfileStore(paths, "trusted_owner")
    flow_store = AuthorizationFlowStore(paths, "trusted_owner", vault=store.vault)
    flow_store.begin_or_advance(
        provider="lark",
        adapter_id="lark-cli",
        profile_id=first.profile_id,
        purpose="lark_full_authorization",
        phase=LARK_APP_CONFIGURATION_PHASE,
        profile_revision=float(first.profile_revision or 0),
        base_state_revision=store.state_revision("lark", first.profile_id),
        adapter_contract_fingerprint=_LARK_AUTH_CONTRACT,
        public={},
        secret=None,
        expires_at=None,
    )
    store.begin_browser_job(first.profile_id, "job-keep", _LARK_STATE.fingerprint)
    plan = service.plan(match, {})
    monkeypatch.setattr(
        AuthorizationFlowStore,
        "write_staged_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("vault full")),
    )

    result = service.execute(plan, {})

    assert result.payload["error"] == "managed_authorization_failed"
    assert backend.finalized is False
    profile = CredentialProfileStore(paths, "trusted_owner").resolve("lark")
    assert profile["status"] == "pending_configuration"
    assert profile["browser_job_id"] == "job-keep"
    assert profile["browser_job_id"] == "job-keep"


def test_package_resolution_freezes_exact_registry_identity_and_image(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = ProjectSandboxManager({})
    calls: list[list[str]] = []
    monkeypatch.setattr(manager, "ensure_image", lambda _image: _TEST_IMAGE_DIGEST)

    def fake_run(args, *, timeout=30):
        calls.append(list(args))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "name": "@larksuite/cli",
                    "version": "1.2.3",
                    "dist.integrity": _TEST_INTEGRITY,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(manager, "_run", fake_run)
    resolution = manager.resolve_managed_node_cli(
        workspace,
        distribution="@larksuite/cli@latest",
        package="@larksuite/cli",
    )

    assert resolution.distribution == "@larksuite/cli@1.2.3"
    assert resolution.integrity == _TEST_INTEGRITY
    assert resolution.runtime_image_digest == _TEST_IMAGE_DIGEST
    rendered = " ".join(calls[0])
    assert "@larksuite/cli@latest" in rendered
    assert str(workspace.resolve()) not in rendered
    assert "--mount" not in calls[0]


def test_removed_incremental_installer_fails_closed(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    toolchain = tmp_path / "toolchain"
    workspace.mkdir()
    toolchain.mkdir()
    manager = ProjectSandboxManager({})
    monkeypatch.setattr(
        manager,
        "_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    with pytest.raises(RuntimeError, match="single-package managed CLI installation is disabled"):
        manager.install_managed_node_cli(
            workspace,
            distribution="@larksuite/cli@1.0.0",
            package="@larksuite/cli",
            executable="lark-cli",
            expected_runtime_image_digest=_TEST_IMAGE_DIGEST,
            toolchain_path=toolchain,
            container_path="/opt/puddingclaw/toolchain/node",
        )


def test_installer_rejects_runtime_image_change_after_approval(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    toolchain = tmp_path / "toolchain"
    workspace.mkdir()
    toolchain.mkdir()
    manager = ProjectSandboxManager({})
    monkeypatch.setattr(manager, "ensure_image", lambda _image: "sha256:" + "2" * 64)
    monkeypatch.setattr(
        manager,
        "_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )

    with pytest.raises(RuntimeError, match="single-package managed CLI installation is disabled"):
        manager.install_managed_node_cli(
            workspace,
            distribution="@larksuite/cli@1.2.3",
            package="@larksuite/cli",
            executable="lark-cli",
            expected_runtime_image_digest=_TEST_IMAGE_DIGEST,
            toolchain_path=toolchain,
            container_path="/opt/puddingclaw/toolchain/node",
        )


def test_provider_and_browser_runner_enforce_approved_runtime_image(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    toolchain = tmp_path / "toolchain"
    workspace.mkdir()
    (toolchain / "bin").mkdir(parents=True)
    _write_fake_toolchain_manifest(toolchain, executable="lark-cli")
    manager = ProjectSandboxManager({})
    monkeypatch.setattr(manager, "ensure_image", lambda _image: "sha256:" + "2" * 64)
    monkeypatch.setattr(
        manager,
        "_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )

    with pytest.raises(ValueError, match="provider runtime image changed"):
        manager.run_managed_provider_cli(
            workspace,
            argv=["lark-cli", "--version"],
            environment={},
            credential_state_spec=None,
            toolchain_path=toolchain,
            container_path="/opt/puddingclaw/toolchain/node",
            credential_state=b"",
            network_enabled=False,
            workspace_writable=False,
            expected_runtime_image_digest=_TEST_IMAGE_DIGEST,
        )
    with pytest.raises(ValueError, match="browser runtime image changed"):
        manager.run_managed_browser_auth_cli(
            workspace,
            argv=["lark-cli", "config", "init", "--new"],
            environment={},
            credential_state_spec=_LARK_STATE,
            toolchain_path=toolchain,
            container_path="/opt/puddingclaw/toolchain/node",
            credential_state=b"",
            owner_user_id="owner",
            provider="lark",
            profile_id="lark_default",
            adapter_id="lark-cli",
            authorization_contract_fingerprint=_LARK_AUTH_CONTRACT,
            expected_runtime_image_digest=_TEST_IMAGE_DIGEST,
        )


def test_provider_runner_injects_archive_via_stdin_and_uses_tmpfs_home(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    toolchain = tmp_path / "toolchain"
    workspace.mkdir()
    (toolchain / "bin").mkdir(parents=True)
    _write_fake_toolchain_manifest(toolchain, executable="lark-cli", image_digest="sha256:image")
    archive = _credential_archive(b'{"token":"secret"}')
    manager = ProjectSandboxManager({})
    calls: list[list[str]] = []
    byte_calls: list[tuple[list[str], bytes | None]] = []
    monkeypatch.setattr(manager, "ensure_image", lambda _image: "sha256:image")

    def fake_run(args, *, timeout=30):
        calls.append(list(args))
        stdout = "status ok" if args[:3] == ["exec", "--workdir", "/workspace"] else "ok"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    def fake_run_bytes(args, *, input_bytes=None, timeout=30):
        byte_calls.append((list(args), input_bytes))
        return SimpleNamespace(returncode=0, stdout=archive if input_bytes is None else b"", stderr=b"")

    monkeypatch.setattr(manager, "_run", fake_run)
    monkeypatch.setattr(manager, "_run_bytes", fake_run_bytes)
    result = manager.run_managed_provider_cli(
        workspace,
        argv=["lark-cli", "auth", "status", "--verify"],
        environment={"LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1"},
        credential_state_spec=_LARK_STATE,
        toolchain_path=toolchain,
        container_path="/opt/puddingclaw/toolchain/node",
        credential_state=archive,
        network_enabled=True,
        workspace_writable=False,
        expected_runtime_image_digest="sha256:image",
    )

    create = calls[0]
    rendered = " ".join(create)
    assert result.exit_code == 0
    assert result.credential_state == archive
    assert "/home/puddingclaw:rw,nosuid,nodev,size=128m" in rendered
    assert f"src={toolchain.resolve()},dst=/opt/puddingclaw/toolchain/node,readonly" in rendered
    assert f"src={workspace.resolve()},dst=/workspace,readonly" in rendered
    assert "LARKSUITE_CLI_DATA_DIR=/home/puddingclaw/.lark-cli/.credential-data" in rendered
    assert "/home/puddingclaw/.local/share/lark-cli" in rendered
    assert "umask 077" in rendered
    assert "vault.enc" not in rendered
    assert "com.puddingclaw.kind=provider-runner" in rendered
    assert byte_calls[0][1] == archive
    assert byte_calls[1][1] is None
    assert byte_calls[1][0][-3:] == ["--", ".lark-cli", ".local/share/lark-cli"]


def test_provider_runner_keeps_authorization_continuation_out_of_host_argv(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    toolchain = tmp_path / "toolchain"
    workspace.mkdir()
    (toolchain / "bin").mkdir(parents=True)
    _write_fake_toolchain_manifest(toolchain, executable="lark-cli", image_digest="sha256:image")
    archive = _credential_archive(b'{"configured":true}')
    continuation = b"SECRET-DEVICE-CODE-CANARY"
    manager = ProjectSandboxManager({})
    calls: list[list[str]] = []
    byte_calls: list[tuple[list[str], bytes | None]] = []
    monkeypatch.setattr(manager, "ensure_image", lambda _image: "sha256:image")

    def fake_run(args, *, timeout=30):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    def fake_run_bytes(args, *, input_bytes=None, timeout=30):
        byte_calls.append((list(args), input_bytes))
        return SimpleNamespace(returncode=0, stdout=archive if input_bytes is None else b"", stderr=b"")

    monkeypatch.setattr(manager, "_run", fake_run)
    monkeypatch.setattr(manager, "_run_bytes", fake_run_bytes)
    result = manager.run_managed_provider_cli(
        workspace,
        argv=["lark-cli", "auth", "login"],
        environment={},
        credential_state_spec=_LARK_STATE,
        toolchain_path=toolchain,
        container_path="/opt/puddingclaw/toolchain/node",
        credential_state=archive,
        network_enabled=True,
        workspace_writable=False,
        expected_runtime_image_digest="sha256:image",
        continuation_secret=continuation,
        continuation_argument="--device-code",
        continuation_trailing_argv=("--json",),
    )

    assert result.exit_code == 0
    assert continuation in [payload for _, payload in byte_calls]
    assert all(continuation.decode() not in " ".join(argv) for argv in calls)
    assert all(continuation.decode() not in " ".join(argv) for argv, _ in byte_calls)
    secure_call = next(argv for argv, payload in byte_calls if payload == continuation)
    assert secure_call[:2] == ["exec", "-i"]
    shell_index = secure_call.index("sh")
    assert secure_call[shell_index : shell_index + 2] == ["sh", "-c"]
    assert "--device-code" in secure_call[shell_index + 2]
    assert secure_call[shell_index + 2].endswith('"$secret" --json')


def test_provider_runner_timeout_preserves_container_and_exports_candidate_state(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    toolchain = tmp_path / "toolchain"
    workspace.mkdir()
    (toolchain / "bin").mkdir(parents=True)
    _write_fake_toolchain_manifest(toolchain, executable="lark-cli", image_digest="sha256:image")
    baseline = _credential_archive(b'{"configured":true}')
    candidate = _credential_archive(b'{"user":"new-token"}')
    continuation = b"SECRET-DEVICE-CODE-CANARY"
    manager = ProjectSandboxManager({})
    calls: list[list[str]] = []
    byte_calls: list[tuple[list[str], bytes | None]] = []
    monkeypatch.setattr(manager, "ensure_image", lambda _image: "sha256:image")

    def fake_run(args, *, timeout=30):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    def fake_run_bytes(args, *, input_bytes=None, timeout=30):
        byte_calls.append((list(args), input_bytes))
        if input_bytes == continuation:
            raise subprocess.TimeoutExpired(args, timeout)
        if input_bytes == baseline:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        return SimpleNamespace(returncode=0, stdout=candidate, stderr=b"")

    monkeypatch.setattr(manager, "_run", fake_run)
    monkeypatch.setattr(manager, "_run_bytes", fake_run_bytes)

    result = manager.run_managed_provider_cli(
        workspace,
        argv=["lark-cli", "auth", "login"],
        environment={},
        credential_state_spec=_LARK_STATE,
        toolchain_path=toolchain,
        container_path="/opt/puddingclaw/toolchain/node",
        credential_state=baseline,
        network_enabled=True,
        workspace_writable=False,
        expected_runtime_image_digest="sha256:image",
        continuation_secret=continuation,
        continuation_argument="--device-code",
        continuation_trailing_argv=("--json",),
        timeout=1,
    )

    assert result.exit_code == 124
    assert result.credential_state == candidate
    assert not any(call[0] == "stop" for call in calls)
    assert any("timeout" in " ".join(call) for call, _ in byte_calls)
    assert byte_calls[-1][1] is None


def test_credentialless_provider_runner_does_not_import_or_export_state(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    toolchain = tmp_path / "toolchain"
    workspace.mkdir()
    (toolchain / "bin").mkdir(parents=True)
    _write_fake_toolchain_manifest(toolchain, executable="lark-cli", image_digest="sha256:image")
    manager = ProjectSandboxManager({})
    calls: list[list[str]] = []
    byte_calls: list[list[str]] = []
    monkeypatch.setattr(manager, "ensure_image", lambda _image: "sha256:image")

    def fake_run(args, *, timeout=30):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="v1.0.78", stderr="")

    def fake_run_bytes(args, *, input_bytes=None, timeout=30):
        byte_calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(manager, "_run", fake_run)
    monkeypatch.setattr(manager, "_run_bytes", fake_run_bytes)
    result = manager.run_managed_provider_cli(
        workspace,
        argv=["lark-cli", "--version"],
        environment={},
        credential_state_spec=None,
        toolchain_path=toolchain,
        container_path="/opt/puddingclaw/toolchain/node",
        credential_state=b"",
        network_enabled=False,
        workspace_writable=False,
        expected_runtime_image_digest="sha256:image",
    )

    assert result.exit_code == 0
    assert result.credential_state is None
    assert byte_calls == []
    rendered = " ".join(calls[0])
    assert "LARKSUITE_CLI_DATA_DIR" not in rendered
    assert ".local/share/lark-cli" not in rendered


def test_startup_gc_removes_owned_and_structurally_verified_legacy_workspaces(monkeypatch):
    manager = ProjectSandboxManager({})
    calls: list[list[str]] = []
    owner = manager._owner_label()
    fixtures = {
        "owned": {
            "Name": "/puddingclaw-project-aaaaaaaaaaaaaaaa",
            "Config": {
                "Labels": {
                    "com.puddingclaw.managed": "true",
                    "com.puddingclaw.kind": "workspace",
                    "com.puddingclaw.owner": owner,
                }
            },
        },
        "legacy": {
            "Name": "/puddingclaw-project-bbbbbbbbbbbbbbbb",
            "Config": {"Labels": {"com.puddingclaw.managed": "true", "com.puddingclaw.spec-hash": "hash"}},
        },
        "foreign": {
            "Name": "/puddingclaw-project-cccccccccccccccc",
            "Config": {
                "Labels": {
                    "com.puddingclaw.managed": "true",
                    "com.puddingclaw.kind": "workspace",
                    "com.puddingclaw.owner": "someone-else",
                }
            },
        },
    }

    def fake_run(args, *, timeout=30):
        calls.append(list(args))
        if args[0] == "ps":
            return SimpleNamespace(returncode=0, stdout="owned\nlegacy\nforeign\n", stderr="")
        if args[0] == "inspect":
            return SimpleNamespace(returncode=0, stdout=json.dumps([fixtures[args[1]]]), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_run", fake_run)
    assert manager.gc_stopped_workspace_containers() == 2
    assert [call for call in calls if call[0] == "rm"] == [["rm", "owned"], ["rm", "legacy"]]
