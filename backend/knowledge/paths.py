"""Knowledge base filesystem path helpers."""

from __future__ import annotations

import os
from pathlib import Path


def _configured_knowledge_root() -> tuple[str, str]:
    env_root = os.getenv("PUDDINGCLAW_KNOWLEDGE_DIR", "").strip()
    if env_root:
        return env_root, "env"
    try:
        from config import get_knowledge_root_config

        root = str(get_knowledge_root_config().get("root_dir") or "").strip()
        if root:
            return root, "config"
    except Exception:
        return "", "default"
    return "", "default"


def get_knowledge_root(base_dir: Path) -> Path:
    """Return the physical root mapped to DeepAgents `/knowledge/`.

    Default is `backend/knowledge` for a zero-config local checkout. Users can
    move the actual knowledge store outside the project from Settings
    (`knowledge.root_dir`) or by setting `PUDDINGCLAW_KNOWLEDGE_DIR`, for example:

        PUDDINGCLAW_KNOWLEDGE_DIR=/Users/pet/Documents/PuddingClawKnowledge

    All imported Markdown, MinerU image assets, glob/grep scans, and LlamaIndex
    indexing use this same root, so DeepAgents sees a coherent `/knowledge/`.
    """

    configured, _source = _configured_knowledge_root()
    if configured:
        return Path(configured).expanduser().resolve()
    return base_dir / "knowledge"


def get_knowledge_originals_dir(base_dir: Path, knowledge_root: Path | None = None) -> Path:
    """Return where original uploaded files are stored.

    When the knowledge root is user-configured, keep originals inside that root
    so the knowledge base is portable as one folder. Otherwise preserve the
    previous project-local data location.
    """

    configured, _source = _configured_knowledge_root()
    if configured:
        return (knowledge_root or get_knowledge_root(base_dir)) / "originals"
    return base_dir / "data" / "knowledge" / "originals"
