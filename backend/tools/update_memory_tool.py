"""Controlled, scope-bound updates for the current Agent MEMORY.md."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Literal

from filelock import FileLock, Timeout as FileLockTimeout
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field


_MEMORY_WRITE_LOCK = threading.RLock()
_MAX_MEMORY_CHARS = 100_000
_MAX_ENTRY_CHARS = 4_000


class UpdateMemoryInput(BaseModel):
    operation: Literal["append", "replace", "remove"] = Field(
        description="append a durable entry, replace one exact stale passage, or remove one exact passage"
    )
    section: str | None = Field(
        default=None,
        description="Markdown section name for append, without heading markers",
    )
    content: str | None = Field(
        default=None,
        description="Durable memory entry for append, or replacement text for replace",
    )
    old_text: str | None = Field(
        default=None,
        description="Exact current passage for replace or remove",
    )


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _append_entry(document: str, section: str, content: str) -> tuple[str, bool]:
    section = section.strip().lstrip("#").strip()
    entry = content.strip()
    if not section or "\n" in section or len(section) > 120:
        raise ValueError("section must be one non-empty Markdown heading name")
    if not entry or len(entry) > _MAX_ENTRY_CHARS:
        raise ValueError(f"content must contain 1-{_MAX_ENTRY_CHARS} characters")
    bullet = entry if entry.startswith(("- ", "* ")) else f"- {entry}"
    if bullet in document.splitlines():
        return document, False

    header = f"## {section}"
    lines = document.rstrip().splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == header)
    except StopIteration:
        prefix = document.rstrip()
        updated = f"{prefix}\n\n{header}\n\n{bullet}\n" if prefix else f"# Memory\n\n{header}\n\n{bullet}\n"
        return updated, True

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    lines.insert(end, bullet)
    return "\n".join(lines).rstrip() + "\n", True


class UpdateMemoryTool(BaseTool):
    name: str = "update_memory"
    description: str = (
        "Update the current Run's durable MEMORY.md through a scope-bound atomic operation. "
        "Use append for one reusable fact or preference, replace for an exact stale passage, "
        "and remove only when the user asks to forget or a passage is proven obsolete."
    )
    args_schema: type[BaseModel] = UpdateMemoryInput
    risk_level: str = "moderate"
    memory_file: str = ""
    memory_root: str = ""

    def _resolve_scope(self) -> Path:
        if not self.memory_file or not self.memory_root:
            raise ValueError("memory_scope_unavailable")
        root = Path(self.memory_root).expanduser().absolute()
        path = Path(self.memory_file).expanduser().absolute()
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise ValueError("memory path is outside the bound Home memory root") from exc

        current = root
        for part in ("", *relative.parts):
            current = current if not part else current / part
            if current.exists() and current.is_symlink():
                raise ValueError(f"symbolic links are not allowed in the Memory scope: {current.name}")

        resolved_root = root.resolve(strict=False)
        resolved_path = path.resolve(strict=False)
        if not resolved_path.is_relative_to(resolved_root):
            raise ValueError("memory path resolves outside the bound Home memory root")
        return resolved_path

    def _run(
        self,
        operation: str,
        section: str | None = None,
        content: str | None = None,
        old_text: str | None = None,
    ) -> str:
        try:
            path = self._resolve_scope()
            lock = FileLock(str(path) + ".lock", timeout=10)
            with _MEMORY_WRITE_LOCK, lock:
                current = path.read_text(encoding="utf-8") if path.exists() else "# Memory\n"
                if operation == "append":
                    updated, changed = _append_entry(current, section or "Durable Facts", content or "")
                else:
                    exact = str(old_text or "")
                    if not exact:
                        raise ValueError("old_text is required for replace or remove")
                    if len(exact) > _MAX_ENTRY_CHARS:
                        raise ValueError(f"old_text must not exceed {_MAX_ENTRY_CHARS} characters")
                    occurrences = current.count(exact)
                    if occurrences != 1:
                        raise ValueError(f"old_text must match exactly once; matched {occurrences} times")
                    replacement = str(content or "") if operation == "replace" else ""
                    if len(replacement) > _MAX_ENTRY_CHARS:
                        raise ValueError(f"content must not exceed {_MAX_ENTRY_CHARS} characters")
                    updated = current.replace(exact, replacement, 1)
                    changed = updated != current
                if len(updated) > _MAX_MEMORY_CHARS:
                    raise ValueError(f"MEMORY.md exceeds {_MAX_MEMORY_CHARS} characters")
                if changed:
                    _atomic_write(path, updated)
                digest = "sha256:" + hashlib.sha256(updated.encode("utf-8")).hexdigest()
            return json.dumps(
                {
                    "ok": True,
                    "operation": operation,
                    "changed": changed,
                    "scope": "current_run",
                    "path": "MEMORY.md",
                    "content_sha256": digest,
                },
                ensure_ascii=False,
            )
        except (FileLockTimeout, OSError, UnicodeError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def create_update_memory_tool(_base_dir: Path | None = None) -> UpdateMemoryTool:
    return UpdateMemoryTool()


__all__ = ["UpdateMemoryTool", "create_update_memory_tool"]
