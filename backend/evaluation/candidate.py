"""Resolve a browser request into an auditable Candidate snapshot."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from runtime_identity.paths import PuddingClawPaths

from .contracts import ExperimentCandidate

ISOLATED_WORKSPACE_CORE_TOOLS = (
    "edit_file",
    "glob",
    "grep",
    "ls",
    "read_file",
    "write_file",
    "update_todos",
)
ISOLATED_CAPABILITY_PROFILE = "isolated_workspace_core@1"
_CANDIDATE_SOURCE_ROOTS = [
    "graph",
    "harness",
    "llm",
    "tools",
    "prompts",
    "skills",
    "config.py",
    "provider_registry.py",
    "pyproject.toml",
    "uv.lock",
]
_SECRET_KEY = re.compile(r"(api[_-]?key|token|secret|password|authorization|cookie)", re.I)


def _without_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SECRET_KEY.search(str(key)) else _without_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_without_secrets(item) for item in value]
    return value


class CandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    llm_model_id: str | None = None
    thinking_level: Literal["low", "high", "max"] | None = None
    credential_name: str | None = None
    project_id: str | None = None
    analytics_model_id: str | None = None
    tool_allowlist: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def forbid_production_tool_expansion(self) -> CandidateRequest:
        if self.tool_allowlist:
            raise ValueError(
                "Phase 1 evaluation workers do not allow production custom tools; use workspace fixture tools only"
            )
        if self.project_id is not None:
            raise ValueError(
                "Phase 1 cannot evaluate a production project_id; use Dataset fixtures in an isolated workspace"
            )
        if self.config:
            raise ValueError("Phase 1 does not accept Candidate config overrides that cannot be executed")
        return self


def _git(base_dir: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=base_dir.parent, capture_output=True, text=True, timeout=3, check=True
        )
        return result.stdout.strip()
    except Exception:
        return None


def _tree_hash(base_dir: Path, roots: list[str]) -> str:
    digest = hashlib.sha256()
    for root_name in roots:
        root = base_dir / root_name
        if not root.exists():
            continue
        files = (
            [root]
            if root.is_file()
            else sorted(
                path
                for path in root.rglob("*")
                if path.is_file() and path.suffix in {".py", ".md", ".json", ".yaml", ".yml"}
            )
        )
        for path in files:
            try:
                relative = path.relative_to(base_dir)
                digest.update(str(relative).encode())
                digest.update(path.read_bytes())
            except OSError:
                continue
    return digest.hexdigest()


def _skill_tree_hash(root: Path) -> str:
    """Hash authored Skill contents, including scripts and binary resources."""

    digest = hashlib.sha256()
    ignored_parts = {".git", ".venv", "node_modules", "__pycache__"}
    ignored_names = {".DS_Store"}
    if not root.is_dir():
        return digest.hexdigest()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root)
        if ignored_parts.intersection(relative.parts) or path.name in ignored_names:
            continue
        try:
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
        except OSError:
            continue
    return digest.hexdigest()


def _skill_hashes(base_dir: Path) -> dict[str, str]:
    """Fingerprint both immutable bundled Skills and user-owned Home Skills."""

    bundled = _skill_tree_hash(base_dir / "skills")
    user_root = PuddingClawPaths.from_environment().user_skills()
    user = _skill_tree_hash(user_root)
    effective = hashlib.sha256(
        f"bundled:{bundled}\nuser:{user}".encode("utf-8")
    ).hexdigest()
    return {
        "bundled_skill_hash": bundled,
        "user_skill_hash": user,
        "skill_hash": effective,
    }


def resolve_candidate(base_dir: Path, request: CandidateRequest) -> ExperimentCandidate:
    import config

    git_sha = _git(base_dir, "rev-parse", "HEAD")
    dirty = _git(base_dir, "status", "--porcelain")
    resolution_kwargs: dict[str, Any] = {
        "model_id_override": request.llm_model_id,
        "thinking_level": request.thinking_level,
    }
    if request.credential_name:
        resolution_kwargs["credential_name"] = request.credential_name
    effective_llm = config.get_fallback_llm_config(**resolution_kwargs)
    effective_llm = _without_secrets(effective_llm)
    skill_hashes = _skill_hashes(base_dir)
    snapshots: dict[str, Any] = {
        "llm_model_id": request.llm_model_id,
        "thinking_level": request.thinking_level,
        "credential_name": request.credential_name,
        "analytics_model_id": request.analytics_model_id,
        "project_id": request.project_id,
        "effective_llm": effective_llm,
        "git_sha": git_sha,
        "git_dirty": bool(dirty) if dirty is not None else None,
        "prompt_hash": _tree_hash(base_dir, ["prompts"]),
        "tool_hash": _tree_hash(base_dir, ["tools"]),
        **skill_hashes,
        "runtime_hash": _tree_hash(base_dir, ["graph", "pyproject.toml"]),
        "source_manifest_hash": _tree_hash(base_dir, _CANDIDATE_SOURCE_ROOTS),
        "tool_allowlist": sorted(set(request.tool_allowlist)),
        "capability_profile": ISOLATED_CAPABILITY_PROFILE,
        "offered_tools": list(ISOLATED_WORKSPACE_CORE_TOOLS),
        "requested_config": request.config,
    }
    fingerprint = hashlib.sha256(json.dumps(snapshots, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]
    return ExperimentCandidate(
        name=request.name,
        llm_model_id=request.llm_model_id,
        thinking_level=request.thinking_level,
        credential_name=request.credential_name,
        project_id=request.project_id,
        analytics_model_id=request.analytics_model_id,
        config=snapshots,
        fingerprint=fingerprint,
        fingerprint_status="complete" if git_sha is not None else "partial",
    )


def verify_candidate_snapshot(base_dir: Path, candidate: ExperimentCandidate) -> list[str]:
    """Return changed snapshot components; an empty list is compatible."""

    import config

    expected = candidate.config
    resolution_kwargs: dict[str, Any] = {
        "model_id_override": candidate.llm_model_id,
        "thinking_level": candidate.thinking_level,
    }
    if candidate.credential_name:
        resolution_kwargs["credential_name"] = candidate.credential_name
    effective_llm = config.get_fallback_llm_config(**resolution_kwargs)
    effective_llm = _without_secrets(effective_llm)
    skill_hashes = _skill_hashes(base_dir)
    current = {
        "llm_model_id": candidate.llm_model_id,
        "thinking_level": candidate.thinking_level,
        "credential_name": candidate.credential_name,
        "analytics_model_id": candidate.analytics_model_id,
        "project_id": candidate.project_id,
        "effective_llm": effective_llm,
        "git_sha": _git(base_dir, "rev-parse", "HEAD"),
        "git_dirty": bool(_git(base_dir, "status", "--porcelain")),
        "prompt_hash": _tree_hash(base_dir, ["prompts"]),
        "tool_hash": _tree_hash(base_dir, ["tools"]),
        **skill_hashes,
        "runtime_hash": _tree_hash(base_dir, ["graph", "pyproject.toml"]),
        "source_manifest_hash": _tree_hash(base_dir, _CANDIDATE_SOURCE_ROOTS),
    }
    # Hand-authored/legacy protocol records may not contain every snapshot key.
    # Verify every value that was actually frozen, without manufacturing drift
    # from fields that never existed in that Candidate snapshot.
    return [key for key, value in current.items() if key in expected and expected[key] != value]
