"""Adversarial tests for typed, Docker-only Skill package installation."""

from __future__ import annotations

import pytest
from deepagents.backends.protocol import ExecuteResponse
from pydantic import ValidationError

from tools.package_install import InstallPackagesInput, create_install_packages_tool


@pytest.mark.parametrize(
    ("ecosystem", "package"),
    [
        ("python", "requests @ https://evil.example/pkg.whl"),
        ("python", "git+https://evil.example/repo.git"),
        ("python", "../../host-package"),
        ("python", "--index-url=https://evil.example"),
        ("node", "demo@file:../../host-package"),
        ("node", "demo@github:attacker/repo"),
        ("node", "https://evil.example/package.tgz"),
        ("node", "--registry=https://evil.example"),
    ],
)
def test_package_input_rejects_non_registry_references(ecosystem, package):
    with pytest.raises(ValidationError, match="non-registry"):
        InstallPackagesInput(ecosystem=ecosystem, packages=[package])


@pytest.mark.parametrize(
    ("ecosystem", "packages"),
    [
        ("python", ["requests==2.32.4", "pandas[excel]>=2.2.0"]),
        ("node", ["typescript@^5.8.0", "@playwright/test@latest"]),
    ],
)
def test_package_input_accepts_registry_names_and_deduplicates(ecosystem, packages):
    parsed = InstallPackagesInput(
        ecosystem=ecosystem,
        packages=[packages[0], packages[0], packages[1]],
    )

    assert parsed.packages == packages


def test_install_tool_passes_typed_argv_data_without_shell_rendering():
    calls: list[tuple[str, list[str]]] = []

    def installer(ecosystem: str, packages: list[str]) -> ExecuteResponse:
        calls.append((ecosystem, packages))
        return ExecuteResponse(output="installed", exit_code=0)

    tool = create_install_packages_tool(installer)

    assert tool.invoke({"ecosystem": "python", "packages": ["requests==2.32.4"]}) == "installed"
    assert calls == [("python", ["requests==2.32.4"])]
