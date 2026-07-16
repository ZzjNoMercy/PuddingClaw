"""Detect project dependency manifests and build a controlled install plan."""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        ".puddingclaw",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
    }
)


@dataclass(frozen=True)
class DependencyInstallStep:
    ecosystem: str
    working_directory: str
    manifests: tuple[str, ...]
    command: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ecosystem": self.ecosystem,
            "working_directory": self.working_directory,
            "manifests": list(self.manifests),
            "command": self.command,
        }


@dataclass(frozen=True)
class DependencyRuntimeMount:
    ecosystem: str
    working_directory: str
    target_name: str

    def to_dict(self) -> dict[str, str]:
        return {
            "ecosystem": self.ecosystem,
            "working_directory": self.working_directory,
            "target_name": self.target_name,
        }


@dataclass(frozen=True)
class WorkspaceDependencyPlan:
    fingerprint: str
    steps: tuple[DependencyInstallStep, ...]
    runtime_mounts: tuple[DependencyRuntimeMount, ...]
    marker_path: str
    install_command: str
    installed: bool
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "steps": [item.to_dict() for item in self.steps],
            "runtime_mounts": [item.to_dict() for item in self.runtime_mounts],
            "marker_path": self.marker_path,
            "install_command": self.install_command,
            "installed": self.installed,
            "enabled": self.enabled,
            "requires_approval": True,
            "requires_network": True,
            "policy": "package_install",
        }


def _candidate_directories(workspace: Path, max_depth: int = 2) -> list[Path]:
    candidates = [workspace]
    frontier = [(workspace, 0)]
    while frontier:
        current, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for child in children:
            if (
                not child.is_dir()
                or child.name in _IGNORED_DIRS
                or (child.name.startswith(".") and child.name != ".config")
            ):
                continue
            candidates.append(child)
            frontier.append((child, depth + 1))
    return candidates


def _relative_directory(workspace: Path, directory: Path) -> str:
    relative = directory.relative_to(workspace).as_posix()
    return "." if relative == "." else relative


def _container_directory(relative: str) -> str:
    return "/workspace" if relative == "." else f"/workspace/{relative}"


def _hash_file(path: Path, digest: Any) -> None:
    digest.update(path.name.encode("utf-8"))
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        digest.update(b"<unreadable>")


def _node_install_command(directory: Path) -> tuple[str, tuple[str, ...]] | None:
    package_json = directory / "package.json"
    if not package_json.exists():
        return None
    manifests = ["package.json"]
    if (directory / "pnpm-lock.yaml").exists():
        manifests.append("pnpm-lock.yaml")
        return "pnpm install --frozen-lockfile", tuple(manifests)
    if (directory / "package-lock.json").exists():
        manifests.append("package-lock.json")
        return "npm ci", tuple(manifests)
    if (directory / "yarn.lock").exists():
        manifests.append("yarn.lock")
        command = "yarn install --frozen-lockfile"
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
            package_manager = str(payload.get("packageManager") or "")
            if package_manager.startswith("yarn@"):
                major = int(package_manager.removeprefix("yarn@").split(".", 1)[0])
                if major >= 2:
                    command = "yarn install --immutable"
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        return command, tuple(manifests)
    return "npm install", tuple(manifests)


def _python_install_command(directory: Path) -> tuple[str, tuple[str, ...]] | None:
    pyproject = directory / "pyproject.toml"
    requirements = directory / "requirements.txt"
    if pyproject.exists() and (directory / "uv.lock").exists():
        return (
            "python3 -m pip install --user uv && uv sync --frozen",
            ("pyproject.toml", "uv.lock"),
        )
    if pyproject.exists() and (directory / "poetry.lock").exists():
        return (
            "python -m pip install --disable-pip-version-check poetry "
            "&& POETRY_VIRTUALENVS_IN_PROJECT=true "
            "poetry install --no-interaction --no-ansi",
            ("pyproject.toml", "poetry.lock"),
        )
    if (directory / "Pipfile").exists() and (directory / "Pipfile.lock").exists():
        return (
            "python -m pip install --disable-pip-version-check pipenv "
            "&& PIPENV_VENV_IN_PROJECT=1 pipenv sync --dev",
            ("Pipfile", "Pipfile.lock"),
        )
    if requirements.exists():
        return (
            "python3 -m venv .venv "
            "&& .venv/bin/python -m pip install "
            "--disable-pip-version-check -r requirements.txt",
            ("requirements.txt",),
        )
    if pyproject.exists():
        return "python3 -m pip install --user uv && uv sync", ("pyproject.toml",)
    return None


def detect_workspace_dependency_plan(
    workspace_path: Path,
    *,
    enabled: bool = True,
    max_depth: int = 2,
) -> WorkspaceDependencyPlan | None:
    """Return a manifest-derived install plan without executing it."""

    if not enabled:
        return None
    workspace = workspace_path.expanduser().resolve()
    steps: list[DependencyInstallStep] = []
    mounts: list[DependencyRuntimeMount] = []
    digest = hashlib.sha256(b"puddingclaw-dependency-plan-v1\0")
    for directory in _candidate_directories(workspace, max_depth=max_depth):
        relative = _relative_directory(workspace, directory)
        python_plan = _python_install_command(directory)
        if python_plan is not None:
            command, manifests = python_plan
            steps.append(
                DependencyInstallStep(
                    ecosystem="python",
                    working_directory=relative,
                    manifests=manifests,
                    command=command,
                )
            )
            mounts.append(
                DependencyRuntimeMount(
                    ecosystem="python",
                    working_directory=relative,
                    target_name=".venv",
                )
            )
            digest.update(f"python:{relative}:{command}\0".encode())
            for manifest in manifests:
                _hash_file(directory / manifest, digest)
        node_plan = _node_install_command(directory)
        if node_plan is not None:
            command, manifests = node_plan
            steps.append(
                DependencyInstallStep(
                    ecosystem="node",
                    working_directory=relative,
                    manifests=manifests,
                    command=command,
                )
            )
            mounts.append(
                DependencyRuntimeMount(
                    ecosystem="node",
                    working_directory=relative,
                    target_name="node_modules",
                )
            )
            digest.update(f"node:{relative}:{command}\0".encode())
            for manifest in manifests:
                _hash_file(directory / manifest, digest)
    if not steps:
        return None

    fingerprint = digest.hexdigest()[:24]
    marker_relative = f".puddingclaw/runtime/dependencies-{fingerprint}.done"
    marker_path = f"/workspace/{marker_relative}"
    commands: list[str] = []
    for step in steps:
        commands.extend(
            [
                f"cd {shlex.quote(_container_directory(step.working_directory))}",
                step.command,
            ]
        )
    commands.extend(
        [
            "mkdir -p /workspace/.puddingclaw/runtime",
            f"printf '%s\\n' {shlex.quote(fingerprint)} > {shlex.quote(marker_path)}",
        ]
    )
    return WorkspaceDependencyPlan(
        fingerprint=fingerprint,
        steps=tuple(steps),
        runtime_mounts=tuple(mounts),
        marker_path=marker_path,
        install_command=" && ".join(commands),
        installed=(workspace / marker_relative).is_file(),
        enabled=enabled,
    )


def dependency_plan_prompt(plan: WorkspaceDependencyPlan | None) -> str:
    if plan is None or not plan.enabled:
        return ""
    lines = [
        "## Docker workspace dependencies",
        "The Docker image already provides Python and Node.js.",
    ]
    if plan.installed:
        lines.append(
            f"Project dependencies are installed for manifest fingerprint `{plan.fingerprint}`."
        )
    else:
        lines.extend(
            [
                "Project dependency manifests were detected, but their container-specific "
                "environment has not been installed yet.",
                "Before running project tests/builds that require dependencies, call `execute` "
                "with the exact command below. It will trigger package/network approval; never "
                "silently replace it with an unreviewed install command.",
                "",
                f"```sh\n{plan.install_command}\n```",
            ]
        )
    for step in plan.steps:
        runtime_path = (
            f"{_container_directory(step.working_directory)}/"
            f"{'.venv' if step.ecosystem == 'python' else 'node_modules'}"
        )
        lines.append(
            f"- {step.ecosystem} `{step.working_directory}`: isolated runtime at `{runtime_path}`"
        )
    return "\n".join(lines)
