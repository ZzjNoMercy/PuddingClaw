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
    with pytest.raises(ValidationError, match="non-exact registry"):
        InstallPackagesInput(skill_id="demo", ecosystem=ecosystem, packages=[package])


@pytest.mark.parametrize(
    ("ecosystem", "packages"),
    [
        ("python", ["requests==2.32.4", "pandas[excel]==2.2.3"]),
        ("node", ["typescript@5.8.3", "@playwright/test@1.52.0"]),
    ],
)
def test_package_input_accepts_registry_names_and_deduplicates(ecosystem, packages):
    parsed = InstallPackagesInput(
        skill_id="demo",
        ecosystem=ecosystem,
        packages=[packages[0], packages[0], packages[1]],
    )

    assert parsed.packages == packages


def test_node_skill_can_declare_exact_bins_for_requested_package():
    parsed = InstallPackagesInput(
        skill_id="demo",
        ecosystem="node",
        packages=["prettier@3.6.2"],
        executables={"prettier": ["prettier"]},
    )

    assert parsed.executables == {"prettier": ["prettier"]}


def test_node_bin_declaration_cannot_reference_unrequested_package():
    with pytest.raises(ValidationError, match="must reference a requested"):
        InstallPackagesInput(
            skill_id="demo",
            ecosystem="node",
            packages=["prettier@3.6.2"],
            executables={"eslint": ["eslint"]},
        )


@pytest.mark.parametrize(
    ("ecosystem", "package"),
    [
        ("python", "pandas>=2.2.0"),
        ("python", "requests"),
        ("node", "typescript@^5.8.0"),
        ("node", "@playwright/test@latest"),
    ],
)
def test_package_input_rejects_unfrozen_registry_selectors(ecosystem, package):
    with pytest.raises(ValidationError, match="non-exact registry"):
        InstallPackagesInput(skill_id="demo", ecosystem=ecosystem, packages=[package])


def test_install_tool_passes_skill_owned_typed_data_without_shell_rendering(tmp_path):
    calls: list[tuple[str, str, str, list[str]]] = []
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    skill.joinpath("SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")

    def installer(skill_id: str, skill_version: str, ecosystem: str, packages: list[str]) -> ExecuteResponse:
        calls.append((skill_id, skill_version, ecosystem, packages))
        return ExecuteResponse(output="installed", exit_code=0)

    tool = create_install_packages_tool(installer, skills_dir=tmp_path / "skills")

    assert tool.invoke(
        {"skill_id": "demo", "ecosystem": "python", "packages": ["requests==2.32.4"]}
    ) == "installed"
    assert calls == [("demo", calls[0][1], "python", ["requests==2.32.4"])]
    assert calls[0][1].startswith("sha256-")


def test_install_tool_rejects_unknown_or_symlinked_skill(tmp_path):
    tool = create_install_packages_tool(
        lambda *_args: ExecuteResponse(output="unexpected", exit_code=0),
        skills_dir=tmp_path / "skills",
    )
    (tmp_path / "skills").mkdir()

    with pytest.raises(ValueError, match="not installed"):
        tool.invoke({"skill_id": "missing", "ecosystem": "node", "packages": ["demo@1.2.3"]})


def test_install_tool_surfaces_runtime_transaction_failure(tmp_path):
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    skill.joinpath("SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    tool = create_install_packages_tool(
        lambda *_args: ExecuteResponse(output="lock verification failed", exit_code=65),
        skills_dir=tmp_path / "skills",
    )

    with pytest.raises(RuntimeError, match="lock verification failed"):
        tool.invoke({"skill_id": "demo", "ecosystem": "node", "packages": ["demo@1.2.3"]})
