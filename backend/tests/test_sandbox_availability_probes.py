from __future__ import annotations

from types import SimpleNamespace

from harness import kernel_sandbox
from harness.kernel_sandbox import MacOSSeatbeltRunner
from harness.workspace_backends import AdaptiveWorkspaceBackend, ProjectSandboxManager
from runtime_identity.paths import PuddingClawPaths
from runtime_identity.service import ManagedCliService


def test_seatbelt_probe_retries_transient_failure_and_caches_success(monkeypatch):
    now = [100.0]
    outcomes = [
        (False, "transient startup failure"),
        (True, "enforcement passed"),
    ]
    calls: list[float] = []

    def fake_probe_once(cls):
        calls.append(now[0])
        return outcomes.pop(0)

    monkeypatch.setattr(kernel_sandbox.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(MacOSSeatbeltRunner, "_probe_once", classmethod(fake_probe_once))
    monkeypatch.setattr(MacOSSeatbeltRunner, "_probe_cache", None)

    assert MacOSSeatbeltRunner.probe() == (False, "transient startup failure")
    now[0] += MacOSSeatbeltRunner._PROBE_FAILURE_TTL_SECONDS - 1
    assert MacOSSeatbeltRunner.probe() == (False, "transient startup failure")
    assert calls == [100.0]

    now[0] += 2
    assert MacOSSeatbeltRunner.probe() == (True, "enforcement passed")
    now[0] += 3600
    assert MacOSSeatbeltRunner.probe() == (True, "enforcement passed")
    assert len(calls) == 2


def test_docker_probe_uses_short_configurable_timeout(monkeypatch):
    observed: list[int] = []
    manager = ProjectSandboxManager({"probe_timeout_seconds": 3})

    def fake_run(args, *, timeout=30):
        observed.append(timeout)
        return SimpleNamespace(returncode=0, stdout="29.6.2\n", stderr="")

    monkeypatch.setattr(manager, "_run", fake_run)

    assert manager.probe() == (True, "29.6.2")
    assert observed == [3]


def test_docker_probe_invalid_timeout_uses_five_seconds(monkeypatch):
    observed: list[int] = []
    manager = ProjectSandboxManager({"probe_timeout_seconds": "invalid"})

    def fake_run(args, *, timeout=30):
        observed.append(timeout)
        return SimpleNamespace(returncode=1, stdout="", stderr="daemon unavailable")

    monkeypatch.setattr(manager, "_run", fake_run)

    assert manager.probe() == (False, "daemon unavailable")
    assert observed == [5]


def test_adaptive_backend_keeps_managed_cli_service_available_when_docker_is_down(
    tmp_path,
    monkeypatch,
):
    from harness import workspace_backends

    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    docker_probes: list[str] = []
    monkeypatch.setattr(workspace_backends, "_macos_seatbelt_available", lambda: True)
    monkeypatch.setattr(
        ProjectSandboxManager,
        "probe",
        lambda self: docker_probes.append("probe") or (False, "daemon unavailable"),
    )
    backend = AdaptiveWorkspaceBackend(
        root_dir=workspace,
        scratch_path=scratch,
        docker_config={},
    )

    service = ManagedCliService(
        backend,
        paths=PuddingClawPaths(tmp_path / ".puddingclaw"),
    )

    assert service.backend is backend
    assert service.toolchains.runtime_contract == backend.runtime_contract
    # Crash-recovery discovery may attempt one lazy Docker operation, but a
    # transient failure is contained and does not disable the managed service.
    assert docker_probes == ["probe"]


def test_adaptive_managed_method_lookup_does_not_probe_docker(tmp_path, monkeypatch):
    from harness import workspace_backends

    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    docker_probes: list[str] = []
    monkeypatch.setattr(workspace_backends, "_macos_seatbelt_available", lambda: True)
    monkeypatch.setattr(
        ProjectSandboxManager,
        "probe",
        lambda self: docker_probes.append("probe") or (False, "daemon unavailable"),
    )
    backend = AdaptiveWorkspaceBackend(
        root_dir=workspace,
        scratch_path=scratch,
        docker_config={},
    )

    methods = (
        backend.managed_runtime_image_digest,
        backend.resolve_managed_node_cli,
        backend.run_managed_provider_cli,
    )

    assert all(callable(method) for method in methods)
    assert docker_probes == []


def test_adaptive_toolchain_planning_methods_delegate_to_lazy_docker_backend(tmp_path, monkeypatch):
    from harness import workspace_backends

    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    monkeypatch.setattr(workspace_backends, "_macos_seatbelt_available", lambda: True)
    backend = AdaptiveWorkspaceBackend(
        root_dir=workspace,
        scratch_path=scratch,
        docker_config={},
    )
    resolution = object()
    calls: list[tuple[object, ...]] = []

    class FakeDockerBackend:
        @staticmethod
        def managed_runtime_image_digest():
            calls.append(("runtime_digest",))
            return "sha256:" + "a" * 64

        @staticmethod
        def resolve_managed_node_cli(*, distribution, package):
            calls.append(("resolve", distribution, package))
            return resolution

    monkeypatch.setattr(backend, "_docker_backend", lambda: FakeDockerBackend())

    assert backend.managed_runtime_image_digest() == "sha256:" + "a" * 64
    assert (
        backend.resolve_managed_node_cli(
            distribution="@larksuite/cli",
            package="@larksuite/cli",
        )
        is resolution
    )
    assert calls == [
        ("runtime_digest",),
        ("resolve", "@larksuite/cli", "@larksuite/cli"),
    ]
