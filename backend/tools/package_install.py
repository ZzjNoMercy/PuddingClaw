"""Typed installation of declarative, user-global Skill dependencies."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from deepagents.backends.protocol import ExecuteResponse
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from runtime_identity.software_runtime import (
    parse_exact_node_distribution,
    parse_exact_python_requirement,
    skill_content_version,
)

_SKILL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_BIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class InstallPackagesInput(BaseModel):
    skill_id: str = Field(description="依赖所属的已安装 Skill ID；Project 不是依赖所有者")
    ecosystem: Literal["python", "node"] = Field(description="依赖生态：python 或 node")
    packages: list[str] = Field(
        min_length=0,
        max_length=20,
        description="本次新发现的顶层依赖；Backend 与该 Skill 当前 desired set 合并后声明式重建",
    )
    executables: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Node 顶层包需要暴露给该 Skill 的确切 npm bin 名；Python 必须为空",
    )

    @model_validator(mode="after")
    def validate_packages(self) -> InstallPackagesInput:
        self.skill_id = self.skill_id.strip()
        if _SKILL_ID.fullmatch(self.skill_id) is None:
            raise ValueError("invalid Skill id")
        normalized: list[str] = []
        for value in self.packages:
            package = value.strip()
            try:
                if self.ecosystem == "python":
                    parse_exact_python_requirement(package)
                else:
                    parse_exact_node_distribution(package)
            except ValueError as exc:
                raise ValueError(f"non-exact registry {self.ecosystem} package reference: {value!r}") from exc
            normalized.append(package)
        self.packages = list(dict.fromkeys(normalized))
        package_names = {
            parse_exact_node_distribution(item)[0]
            for item in self.packages
            if self.ecosystem == "node"
        }
        if self.ecosystem != "node" and self.executables:
            raise ValueError("executable declarations are supported only for Node packages")
        normalized_bins: dict[str, list[str]] = {}
        for package_name, bins in self.executables.items():
            if package_name not in package_names:
                raise ValueError("executable declaration must reference a requested Node package")
            if not bins or any(_BIN_NAME.fullmatch(str(item)) is None for item in bins):
                raise ValueError("Node executable declaration contains an invalid bin name")
            normalized_bins[package_name] = list(dict.fromkeys(str(item) for item in bins))
        self.executables = normalized_bins
        return self


class InstallPackagesTool(BaseTool):
    """Add newly discovered exact dependencies to one Skill's desired set."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = "install_packages"
    description: str = (
        "Install newly discovered exact-version Python or Node dependencies for an installed Skill. "
        "The Backend merges them with that Skill content version's current desired set and rebuilds declaratively. "
        "The Backend resolves and publishes it in the PuddingClaw-owned host runtime under macOS Seatbelt. "
        "This is lazy Skill setup, never a Project dependency install or a Docker fallback."
    )
    args_schema: type[BaseModel] = InstallPackagesInput
    risk_level: str = "moderate"
    installer: Callable[..., ExecuteResponse] = Field(
        exclude=True,
        repr=False,
    )
    skills_dir: str = Field(exclude=True, repr=False)

    def _skill_version(self, skill_id: str) -> str:
        skills_root = Path(self.skills_dir).resolve(strict=True)
        skill_root = skills_root / skill_id
        if skill_root.is_symlink() or not skill_root.is_dir():
            raise ValueError(f"Skill is not installed: {skill_id}")
        canonical = skill_root.resolve(strict=True)
        canonical.relative_to(skills_root)
        skill_md = canonical / "SKILL.md"
        if skill_md.is_symlink() or not skill_md.is_file():
            raise ValueError(f"Skill manifest is unavailable: {skill_id}")
        return skill_content_version(canonical)

    def _run(
        self,
        skill_id: str,
        ecosystem: str,
        packages: list[str],
        executables: dict[str, list[str]] | None = None,
    ) -> str:
        skill_version = self._skill_version(skill_id)
        normalized_bins = executables or {}
        response = (
            self.installer(skill_id, skill_version, ecosystem, packages, normalized_bins)
            if normalized_bins
            else self.installer(skill_id, skill_version, ecosystem, packages)
        )
        if int(response.exit_code or 0) != 0:
            raise RuntimeError(response.output or "Skill dependency transaction failed")
        return response.output


def create_install_packages_tool(
    installer: Callable[[str, str, str, list[str]], ExecuteResponse],
    *,
    skills_dir: Path,
) -> InstallPackagesTool:
    return InstallPackagesTool(installer=installer, skills_dir=str(skills_dir))
