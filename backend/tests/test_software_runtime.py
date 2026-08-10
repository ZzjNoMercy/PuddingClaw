from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.backends.protocol import ExecuteResponse

from harness.host_skill_runtime import HostSkillRuntimeBackend
from runtime_identity.adapters import ToolchainPackageSpec
from runtime_identity.paths import PuddingClawPaths
from runtime_identity.service import GenericNodeCliInstallPlan, ManagedCliService
from runtime_identity.software_runtime import SoftwareRuntimeManager
from runtime_identity.toolchains import ToolchainManager


class FakeRuntimeBackend:
    runtime_contract = "python3.12+node22+test-v1"
    image_digest = "sha256:" + "a" * 64

    def __init__(self, *, fail_node_build: bool = False, fail_python_build: bool = False) -> None:
        self.fail_node_build = fail_node_build
        self.fail_python_build = fail_python_build
        self.node_resolutions: list[dict[str, str]] = []

    def managed_runtime_image_digest(self) -> str:
        return self.image_digest

    @staticmethod
    def _integrity(package: str, version: str) -> str:
        payload = base64.b64encode(f"{package}@{version}".encode()).decode()
        return f"sha512-{payload}"

    def resolve_shared_node_runtime(
        self,
        *,
        dependencies: dict[str, str],
        expected_runtime_image_digest: str,
        resolution_path: Path,
    ) -> ExecuteResponse:
        assert expected_runtime_image_digest == self.image_digest
        dependencies = dict(sorted(dependencies.items()))
        self.node_resolutions.append(dependencies)
        resolution_path.joinpath("package.json").write_text(
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
        resolution_path.joinpath("package-lock.json").write_text(
            json.dumps(
                {
                    "name": "puddingclaw-managed-runtime",
                    "lockfileVersion": 3,
                    "packages": {
                        "": {"dependencies": dependencies},
                        **{
                            f"node_modules/{package}": {
                                "name": package,
                                "version": version,
                                "integrity": self._integrity(package, version),
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
        return ExecuteResponse(output="resolved", exit_code=0)

    def build_shared_node_runtime(
        self,
        *,
        expected_runtime_image_digest: str,
        runtime_path: Path,
        container_path: str,
    ) -> ExecuteResponse:
        assert expected_runtime_image_digest == self.image_digest
        assert container_path == "/opt/puddingclaw/runtime/node"
        if self.fail_node_build:
            return ExecuteResponse(output="node build failed", exit_code=23)
        package_json = json.loads(runtime_path.joinpath("package.json").read_text(encoding="utf-8"))
        desired = json.loads(runtime_path.joinpath("desired-packages.json").read_text(encoding="utf-8"))
        for package, version in package_json["dependencies"].items():
            package_root = runtime_path / "node_modules" / Path(*package.split("/"))
            package_root.mkdir(parents=True)
            bins = desired["packages"][package]["declared_bins"]
            package_root.joinpath("package.json").write_text(
                json.dumps(
                    {
                        "name": package,
                        "version": version,
                        "bin": {executable: "cli.js" for executable in bins},
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            if bins:
                package_root.joinpath("cli.js").write_bytes(b"#!/usr/bin/env node\n")
        return ExecuteResponse(output="built", exit_code=0)

    def resolve_python_skill_runtime(
        self,
        *,
        expected_runtime_image_digest: str,
        resolution_path: Path,
    ) -> ExecuteResponse:
        assert expected_runtime_image_digest == self.image_digest
        requirements = [
            line.strip()
            for line in resolution_path.joinpath("requirements.in").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        resolution_path.joinpath("requirements.lock").write_text(
            "\n".join(f"{item} \\\n    --hash=sha256:{'b' * 64}" for item in requirements) + "\n",
            encoding="utf-8",
        )
        return ExecuteResponse(output="resolved", exit_code=0)

    def build_python_skill_runtime(
        self,
        *,
        expected_runtime_image_digest: str,
        runtime_path: Path,
        container_path: str,
        uv_cache_path: Path,
    ) -> ExecuteResponse:
        assert expected_runtime_image_digest == self.image_digest
        assert container_path == "/opt/puddingclaw/runtime/python-skill"
        assert "python/uv-cache" in uv_cache_path.as_posix()
        if self.fail_python_build:
            return ExecuteResponse(output="python build failed", exit_code=24)
        python = runtime_path / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"#!/bin/sh\nexit 0\n")
        os.chmod(python, 0o755)
        return ExecuteResponse(output="built", exit_code=0)


def _manager(tmp_path: Path) -> SoftwareRuntimeManager:
    return SoftwareRuntimeManager(PuddingClawPaths(tmp_path / ".puddingclaw"), FakeRuntimeBackend.runtime_contract)


def test_node_shared_runtime_is_declarative_and_install_order_independent(tmp_path):
    first_backend = FakeRuntimeBackend()
    first = _manager(tmp_path / "first")
    first.install_node_owner(
        first_backend,
        owner="skill:alpha@sha256-a",
        distributions=["alpha@1.2.3"],
    )
    first_result = first.install_node_owner(
        first_backend,
        owner="skill:beta@sha256-b",
        distributions=["beta@4.5.6"],
    )

    second_backend = FakeRuntimeBackend()
    second = _manager(tmp_path / "second")
    second.install_node_owner(
        second_backend,
        owner="skill:beta@sha256-b",
        distributions=["beta@4.5.6"],
    )
    second_result = second.install_node_owner(
        second_backend,
        owner="skill:alpha@sha256-a",
        distributions=["alpha@1.2.3"],
    )

    assert first_result.exit_code == second_result.exit_code == 0
    assert first_result.revision == second_result.revision
    assert first_backend.node_resolutions[-1] == second_backend.node_resolutions[-1] == {
        "alpha": "1.2.3",
        "beta": "4.5.6",
    }
    first_current = first.node_current(first_backend.image_digest)
    second_current = second.node_current(second_backend.image_digest)
    assert first_current.joinpath("desired-packages.json").read_bytes() == second_current.joinpath(
        "desired-packages.json"
    ).read_bytes()


def test_node_shared_runtime_rejects_cross_skill_version_conflicts(tmp_path):
    backend = FakeRuntimeBackend()
    manager = _manager(tmp_path)
    manager.install_node_owner(
        backend,
        owner="skill:alpha@sha256-a",
        distributions=["shared@1.0.0"],
    )

    with pytest.raises(ValueError, match="shared Node dependency conflict"):
        manager.install_node_owner(
            backend,
            owner="skill:beta@sha256-b",
            distributions=["shared@2.0.0"],
        )


def test_failed_node_build_never_switches_current(tmp_path):
    backend = FakeRuntimeBackend()
    manager = _manager(tmp_path)
    installed = manager.install_node_owner(
        backend,
        owner="skill:alpha@sha256-a",
        distributions=["alpha@1.2.3"],
    )
    current_before = manager.node_current(backend.image_digest)
    backend.fail_node_build = True

    failed = manager.install_node_owner(
        backend,
        owner="skill:beta@sha256-b",
        distributions=["beta@4.5.6"],
    )

    assert failed.exit_code == 23
    assert manager.node_current(backend.image_digest) == current_before
    assert installed.revision == current_before.name


def test_node_lock_rejects_unverified_transitive_entries(tmp_path):
    class UnverifiedTransitiveBackend(FakeRuntimeBackend):
        def resolve_shared_node_runtime(self, **kwargs):
            result = super().resolve_shared_node_runtime(**kwargs)
            lock_path = kwargs["resolution_path"] / "package-lock.json"
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            lock["packages"]["node_modules/transitive"] = {
                "version": "9.9.9",
                "integrity": self._integrity("transitive", "9.9.9"),
                "resolved": "https://untrusted.example/transitive.tgz",
            }
            lock_path.write_text(json.dumps(lock, sort_keys=True) + "\n", encoding="utf-8")
            return result

    backend = UnverifiedTransitiveBackend()
    manager = _manager(tmp_path)

    with pytest.raises(ValueError, match="lock entry is not reproducible"):
        manager.install_node_owner(
            backend,
            owner="skill:alpha",
            distributions=["alpha@1.2.3"],
        )
    assert manager.node_current(backend.image_digest).name == "empty"


def test_cli_and_skill_packages_share_one_tree_without_cross_owner_loss(tmp_path):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    backend = FakeRuntimeBackend()
    toolchains = ToolchainManager(paths, backend.runtime_contract)
    spec = ToolchainPackageSpec(
        ecosystem="node",
        package="fixture-cli",
        executable="fixture-cli",
    )
    integrity = backend._integrity("fixture-cli", "1.0.0")
    installed_cli = toolchains.install_package(
        backend,
        adapter_id="fixture-cli",
        spec=spec,
        distribution="fixture-cli@1.0.0",
        expected_integrity=integrity,
        runtime_image_digest=backend.image_digest,
        adapter_contract_fingerprint="adapter-contract",
        credential_state_fingerprint="credential-contract",
        expected_revision="empty",
    )
    assert installed_cli.exit_code == 0

    runtime = SoftwareRuntimeManager(paths, backend.runtime_contract)
    skill_result = runtime.install_node_owner(
        backend,
        owner="skill:alpha",
        owner_revision="sha256-alpha",
        distributions=["alpha@2.0.0"],
        expected_base_revision=installed_cli.active_revision,
    )
    assert skill_result.exit_code == 0
    current = runtime.node_current(backend.image_digest)
    desired = json.loads(current.joinpath("desired-packages.json").read_text(encoding="utf-8"))
    assert set(desired["packages"]) == {"alpha", "fixture-cli"}
    assert desired["packages"]["fixture-cli"]["requested_by"] == ["integration:fixture-cli"]
    resolved = toolchains.resolve_for_adapter(
        adapter_id="fixture-cli",
        spec=spec,
        adapter_contract_fingerprint="adapter-contract",
        credential_state_fingerprint="credential-contract",
        runtime_image_digest=backend.image_digest,
    )
    assert resolved.host_path == current


def test_generic_cli_install_is_public_without_exposing_integration_bins(tmp_path):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    backend = FakeRuntimeBackend()
    runtime = SoftwareRuntimeManager(paths, backend.runtime_contract)
    runtime.install_node_owner(
        backend,
        owner="integration:private-cli",
        distributions=["private-cli@1.0.0"],
        declared_bins={"private-cli": ("private-cli",)},
    )
    installed = runtime.install_node_owner(
        backend,
        owner="cli:prettier",
        distributions=["prettier@3.6.2"],
        declared_bins={"prettier": ("prettier",)},
    )

    assert installed.exit_code == 0
    current = runtime.node_current(backend.image_digest)
    assert (current / "public-bin" / "prettier").resolve(strict=True).is_file()
    assert not os.path.lexists(current / "public-bin" / "private-cli")
    assert (current / "bin" / "private-cli").resolve(strict=True).is_file()


def test_generic_cli_public_bin_projection_fails_closed_on_tamper(tmp_path):
    backend = FakeRuntimeBackend()
    runtime = _manager(tmp_path)
    runtime.install_node_owner(
        backend,
        owner="cli:prettier",
        distributions=["prettier@3.6.2"],
        declared_bins={"prettier": ("prettier",)},
    )
    current = runtime.node_current(backend.image_digest)
    public_bin = current / "public-bin"
    (public_bin / "undeclared").symlink_to(public_bin / "prettier")

    with pytest.raises(ValueError, match="current release failed validation"):
        runtime.node_current(backend.image_digest)


def test_generic_cli_service_installs_verified_bin_without_adapter(tmp_path):
    class GenericCliBackend(FakeRuntimeBackend):
        def resolve_generic_node_cli(self, *, distribution, package):
            assert distribution == package == "prettier"
            return SimpleNamespace(
                package="prettier",
                version="3.6.2",
                integrity=self._integrity("prettier", "3.6.2"),
                distribution="prettier@3.6.2",
                runtime_image_digest=self.image_digest,
                executables=("prettier",),
            )

        def generic_node_runtime_current(self, runtime_digest):
            return SoftwareRuntimeManager(paths, self.runtime_contract).node_current(runtime_digest)

        def install_generic_node_cli(self, **kwargs):
            return SoftwareRuntimeManager(paths, self.runtime_contract).install_node_owner(
                self,
                owner=f"cli:{kwargs['package']}",
                owner_revision=kwargs["owner_revision"],
                distributions=[kwargs["distribution"]],
                declared_bins={kwargs["package"]: kwargs["executables"]},
                expected_integrities={kwargs["package"]: kwargs["integrity"]},
                expected_runtime_image_digest=kwargs["runtime_digest"],
                expected_base_revision=kwargs["base_revision"],
            )

    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    backend = GenericCliBackend()
    service = ManagedCliService(backend, paths=paths)

    plan = service.plan_command("npm install --global prettier", {})
    assert isinstance(plan, GenericNodeCliInstallPlan)
    assert plan.executables == ("prettier",)
    result = service.execute(plan)

    assert result.exit_code == 0
    assert result.payload["credentials"] == "none"
    assert result.payload["executables"] == ["prettier"]
    current = service.toolchains.software.node_current(backend.image_digest)
    assert (current / "public-bin" / "prettier").resolve(strict=True).is_file()
    assert not paths.credentials_root("local-user").exists()


def test_generic_cli_service_rejects_package_without_a_bin(tmp_path):
    class LibraryBackend(FakeRuntimeBackend):
        def resolve_generic_node_cli(self, *, distribution, package):
            return SimpleNamespace(
                package=package,
                version="1.0.0",
                integrity=self._integrity(package, "1.0.0"),
                distribution=f"{package}@1.0.0",
                runtime_image_digest=self.image_digest,
                executables=(),
            )

        def generic_node_runtime_current(self, runtime_digest):
            return SoftwareRuntimeManager(paths, self.runtime_contract).node_current(runtime_digest)

    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    service = ManagedCliService(
        LibraryBackend(),
        paths=paths,
    )

    with pytest.raises(ValueError, match="does not expose a reproducible top-level CLI"):
        service.plan_command("npm install -g library-only", {})


def test_runtime_image_drift_invalidates_published_node_tree(tmp_path):
    backend = FakeRuntimeBackend()
    manager = _manager(tmp_path)
    manager.install_node_owner(
        backend,
        owner="skill:alpha",
        distributions=["alpha@1.2.3"],
    )

    with pytest.raises(ValueError, match="failed validation"):
        manager.node_current("sha256:" + "c" * 64)


def test_node_manifest_cannot_self_attest_a_forged_package_projection(tmp_path):
    backend = FakeRuntimeBackend()
    manager = _manager(tmp_path)
    manager.install_node_owner(
        backend,
        owner="skill:alpha",
        distributions=["alpha@1.2.3"],
    )
    current = manager.node_current(backend.image_digest)
    manifest_path = current / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["packages"]["alpha"]["version"] = "9.9.9"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="projection is invalid"):
        manager.node_current(backend.image_digest)
    with pytest.raises(ValueError, match="projection is invalid"):
        manager.install_node_owner(
            backend,
            owner="skill:beta",
            distributions=["beta@1.0.0"],
        )


def test_missing_node_current_pointer_is_not_reinitialized_as_empty(tmp_path):
    backend = FakeRuntimeBackend()
    manager = _manager(tmp_path)
    manager.install_node_owner(
        backend,
        owner="skill:alpha",
        distributions=["alpha@1.2.3"],
    )
    root = manager.paths.shared_node_runtime(manager.runtime_contract)
    (root / "current").unlink()

    with pytest.raises(ValueError, match="missing its current pointer"):
        manager.node_current(backend.image_digest)


def test_python_skill_runtimes_are_isolated_by_skill_and_content_version(tmp_path):
    backend = FakeRuntimeBackend()
    manager = _manager(tmp_path)
    alpha = manager.install_python_skill(
        backend,
        skill_id="alpha",
        skill_version="sha256-alpha",
        requirements=["demo==1.0.0"],
    )
    beta = manager.install_python_skill(
        backend,
        skill_id="beta",
        skill_version="sha256-beta",
        requirements=["demo==2.0.0"],
    )

    assert alpha.exit_code == beta.exit_code == 0
    alpha_current = manager.python_skill_current("alpha", "sha256-alpha", backend.image_digest)
    beta_current = manager.python_skill_current("beta", "sha256-beta", backend.image_digest)
    assert alpha_current is not None and beta_current is not None
    assert alpha_current != beta_current
    assert "demo==1.0.0" in alpha_current.joinpath("requirements.lock").read_text(encoding="utf-8")
    assert "demo==2.0.0" in beta_current.joinpath("requirements.lock").read_text(encoding="utf-8")


def test_python_skills_with_same_lock_share_one_physical_environment(tmp_path):
    backend = FakeRuntimeBackend()
    manager = _manager(tmp_path)
    manager.install_python_skill(
        backend,
        skill_id="alpha",
        skill_version="sha256-alpha",
        requirements=["demo==1.0.0"],
    )
    manager.install_python_skill(
        backend,
        skill_id="beta",
        skill_version="sha256-beta",
        requirements=["demo==1.0.0"],
    )

    alpha = manager.python_skill_current("alpha", "sha256-alpha", backend.image_digest)
    beta = manager.python_skill_current("beta", "sha256-beta", backend.image_digest)

    assert alpha is not None
    assert beta == alpha
    releases = manager.paths.python_environment_runtime(manager.runtime_contract) / "releases"
    assert [item.name for item in releases.iterdir()] == [alpha.name]


def test_python_dependency_discovery_merges_desired_set_and_rebuilds(tmp_path):
    backend = FakeRuntimeBackend()
    manager = _manager(tmp_path)
    first = manager.install_python_skill(
        backend,
        skill_id="alpha",
        skill_version="sha256-alpha",
        requirements=["alpha==1.0.0"],
    )
    second = manager.install_python_skill(
        backend,
        skill_id="alpha",
        skill_version="sha256-alpha",
        requirements=["beta==2.0.0"],
    )
    current = manager.python_skill_current("alpha", "sha256-alpha", backend.image_digest)

    assert first.revision != second.revision
    assert current is not None
    assert set(json.loads((current / "runtime-manifest.json").read_text())["requirements"]) == {
        "alpha==1.0.0",
        "beta==2.0.0",
    }


def test_failed_python_build_does_not_publish_current(tmp_path):
    backend = FakeRuntimeBackend(fail_python_build=True)
    manager = _manager(tmp_path)

    failed = manager.install_python_skill(
        backend,
        skill_id="alpha",
        skill_version="sha256-alpha",
        requirements=["demo==1.0.0"],
    )

    assert failed.exit_code == 24
    assert manager.python_skill_current("alpha", "sha256-alpha", backend.image_digest) is None


def test_python_lock_rejects_non_pypi_sources(tmp_path):
    class UnsupportedIndexBackend(FakeRuntimeBackend):
        def resolve_python_skill_runtime(self, **kwargs):
            result = super().resolve_python_skill_runtime(**kwargs)
            lock = kwargs["resolution_path"] / "requirements.lock"
            lock.write_text(
                "--extra-index-url https://untrusted.example/simple\n"
                f"demo==1.0.0 \\\n    --hash=sha256:{'b' * 64}\n",
                encoding="utf-8",
            )
            return result

    backend = UnsupportedIndexBackend()
    manager = _manager(tmp_path)

    with pytest.raises(ValueError, match="unsupported resolver option"):
        manager.install_python_skill(
            backend,
            skill_id="alpha",
            skill_version="sha256-alpha",
            requirements=["demo==1.0.0"],
        )
    assert manager.python_skill_current("alpha", "sha256-alpha", backend.image_digest) is None


def test_missing_python_current_pointer_is_not_treated_as_uninstalled(tmp_path):
    backend = FakeRuntimeBackend()
    manager = _manager(tmp_path)
    manager.install_python_skill(
        backend,
        skill_id="alpha",
        skill_version="sha256-alpha",
        requirements=["demo==1.0.0"],
    )
    root = manager.paths.python_skill_runtime(manager.runtime_contract, "alpha", "sha256-alpha")
    (root / "current").unlink()

    with pytest.raises(ValueError, match="missing its current pointer"):
        manager.python_skill_current("alpha", "sha256-alpha", backend.image_digest)


def test_python_builder_cannot_publish_an_external_interpreter_symlink(tmp_path):
    class SymlinkedInterpreterBackend(FakeRuntimeBackend):
        def build_python_skill_runtime(self, **kwargs):
            result = super().build_python_skill_runtime(**kwargs)
            python = kwargs["runtime_path"] / ".venv" / "bin" / "python"
            python.unlink()
            python.symlink_to("/bin/sh")
            return result

    backend = SymlinkedInterpreterBackend()
    manager = _manager(tmp_path)

    with pytest.raises(ValueError):
        manager.install_python_skill(
            backend,
            skill_id="alpha",
            skill_version="sha256-alpha",
            requirements=["demo==1.0.0"],
        )
    assert manager.python_skill_current("alpha", "sha256-alpha", backend.image_digest) is None


def test_host_skill_projection_binds_published_venv_without_docker(tmp_path):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    host = HostSkillRuntimeBackend(paths)
    skill_root = tmp_path / "skills" / "fixture"
    (skill_root / "scripts").mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# Fixture\n", encoding="utf-8")
    (skill_root / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")
    from runtime_identity.software_runtime import skill_content_version

    version = skill_content_version(skill_root)
    fake = FakeRuntimeBackend()
    fake.image_digest = host.managed_python_runtime_image_digest()
    SoftwareRuntimeManager(paths, host.python_runtime_contract).install_python_skill(
        fake,
        skill_id="fixture",
        skill_version=version,
        requirements=["demo==1.0.0"],
    )

    projection = host.project_skill_execution(
        "python3 /skills/fixture/scripts/run.py",
        (("/skills", skill_root.parent.resolve()),),
    )
    command, roots = projection

    environment = dict(projection.environment)
    assert "PYTHONHOME" in environment
    assert ".venv/bin" in environment["PATH"]
    assert "export " not in command
    assert command.endswith("python3 /skills/fixture/scripts/run.py")
    assert any((path / ".venv" / "bin" / "python").is_file() for path in roots)


def test_host_skill_projection_uses_trusted_active_skill_for_inline_python(tmp_path):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    host = HostSkillRuntimeBackend(paths)
    skill_root = tmp_path / "skills" / "fixture"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# Fixture\n", encoding="utf-8")
    from runtime_identity.software_runtime import skill_content_version

    version = skill_content_version(skill_root)
    fake = FakeRuntimeBackend()
    fake.image_digest = host.managed_python_runtime_image_digest()
    SoftwareRuntimeManager(paths, host.python_runtime_contract).install_python_skill(
        fake,
        skill_id="fixture",
        skill_version=version,
        requirements=["demo==1.0.0"],
    )

    projection = host.project_skill_execution(
        "python3 -c 'import demo'",
        (("/skills", skill_root.parent.resolve()),),
        skill_id="fixture",
    )

    environment = dict(projection.environment)
    assert environment["PATH"].split(":")[0].endswith("/.venv/bin")
    assert any((path / ".venv" / "bin" / "python").is_file() for path in projection.read_roots)


def test_published_python_skill_ids_disambiguates_active_skills(tmp_path):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    host = HostSkillRuntimeBackend(paths)
    skills_root = tmp_path / "skills"
    for skill_id in ("with-runtime", "without-runtime"):
        root = skills_root / skill_id
        root.mkdir(parents=True)
        (root / "SKILL.md").write_text(f"# {skill_id}\n", encoding="utf-8")
    from runtime_identity.software_runtime import skill_content_version

    runtime_root = skills_root / "with-runtime"
    fake = FakeRuntimeBackend()
    fake.image_digest = host.managed_python_runtime_image_digest()
    SoftwareRuntimeManager(paths, host.python_runtime_contract).install_python_skill(
        fake,
        skill_id="with-runtime",
        skill_version=skill_content_version(runtime_root),
        requirements=["demo==1.0.0"],
    )

    assert host.published_python_skill_ids(
        ("without-runtime", "with-runtime"),
        (("/skills", skills_root.resolve()),),
    ) == ("with-runtime",)


def test_host_cli_projection_exposes_only_public_declared_bins(tmp_path):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    host = HostSkillRuntimeBackend(paths)
    fake = FakeRuntimeBackend()
    fake.image_digest = host.managed_runtime_image_digest()
    SoftwareRuntimeManager(paths, host.runtime_contract).install_node_owner(
        fake,
        owner="cli:prettier",
        distributions=["prettier@3.6.2"],
        declared_bins={"prettier": ("prettier",)},
    )

    projection = host.project_cli_execution("prettier --version")
    command, roots = projection

    assert command.endswith("prettier --version")
    assert "/public-bin" in dict(projection.environment)["PATH"]
    assert "export " not in command
    assert len(roots) == 1
    assert (roots[0] / "public-bin" / "prettier").resolve(strict=True).is_file()


def test_host_node_skill_projection_exposes_declared_skill_bin(tmp_path):
    paths = PuddingClawPaths(tmp_path / ".puddingclaw")
    host = HostSkillRuntimeBackend(paths)
    skill_root = tmp_path / "skills" / "fixture"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# Fixture\n", encoding="utf-8")
    from runtime_identity.software_runtime import skill_content_version

    version = skill_content_version(skill_root)
    fake = FakeRuntimeBackend()
    fake.image_digest = host.managed_runtime_image_digest()
    SoftwareRuntimeManager(paths, host.runtime_contract).install_node_owner(
        fake,
        owner="skill:fixture",
        owner_revision=version,
        distributions=["demo@1.0.0"],
        declared_bins={"demo": ("demo",)},
        merge_owner=True,
    )

    projection = host.project_skill_execution(
        "demo /skills/fixture/SKILL.md",
        (("/skills", skill_root.parent.resolve()),),
    )

    assert any(item.endswith("/bin") for item in dict(projection.environment)["PATH"].split(":"))
