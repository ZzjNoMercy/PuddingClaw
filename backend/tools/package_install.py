"""Typed, Docker-only package installation for third-party Skill dependencies."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal

from deepagents.backends.protocol import ExecuteResponse
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, model_validator

_PYTHON_REGISTRY_PACKAGE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:\[[A-Za-z0-9._,-]+\])?"
    r"(?:(?:===|==|!=|~=|<=|>=|<|>)[A-Za-z0-9][A-Za-z0-9.*+!_-]*"
    r"(?:,(?:===|==|!=|~=|<=|>=|<|>)[A-Za-z0-9][A-Za-z0-9.*+!_-]*)*)?$"
)
_NODE_REGISTRY_PACKAGE = re.compile(
    r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*"
    r"(?:@[A-Za-z0-9~^<>=][A-Za-z0-9._*+~^<>=-]*)?$"
)


class InstallPackagesInput(BaseModel):
    ecosystem: Literal["python", "node"] = Field(description="依赖生态：python 或 node")
    packages: list[str] = Field(
        min_length=1,
        max_length=20,
        description="要从官方包仓库安装的包名及可选版本约束",
    )

    @model_validator(mode="after")
    def validate_packages(self) -> InstallPackagesInput:
        pattern = _PYTHON_REGISTRY_PACKAGE if self.ecosystem == "python" else _NODE_REGISTRY_PACKAGE
        normalized: list[str] = []
        for value in self.packages:
            package = value.strip()
            if not pattern.fullmatch(package):
                raise ValueError(f"non-registry {self.ecosystem} package reference: {value!r}")
            normalized.append(package)
        self.packages = list(dict.fromkeys(normalized))
        return self


class InstallPackagesTool(BaseTool):
    """Install registry packages into the persistent sandbox runtime volume."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = "install_packages"
    description: str = (
        "Install missing Python or Node.js packages required by an active Skill. "
        "This is not for installing all project dependencies. It runs in a temporary "
        "networked installer container and requires package/network approval."
    )
    args_schema: type[BaseModel] = InstallPackagesInput
    risk_level: str = "moderate"
    installer: Callable[[str, list[str]], ExecuteResponse] = Field(
        exclude=True,
        repr=False,
    )

    def _run(self, ecosystem: str, packages: list[str]) -> str:
        response = self.installer(ecosystem, packages)
        return response.output


def create_install_packages_tool(
    installer: Callable[[str, list[str]], ExecuteResponse],
) -> InstallPackagesTool:
    return InstallPackagesTool(installer=installer)
