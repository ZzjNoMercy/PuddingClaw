"""Stable boundary between source files and downstream knowledge artifacts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

ProgressCallback = Callable[[str, int, str], Awaitable[None]]
CheckpointCallback = Callable[[dict[str, Any]], Awaitable[None]]


class ParserError(RuntimeError):
    """A parser could not produce a usable, portable result."""


@dataclass(frozen=True)
class ParserCapabilities:
    parser_id: str
    name: str
    description: str
    location: str
    supported_extensions: tuple[str, ...]
    supports_assets: bool = False
    supports_tables: bool = False
    requires_credential: bool = False
    credential_env: str = ""
    cloud_data_notice: str = ""
    version: str = "unknown"

    def supports(self, filename: str) -> bool:
        return Path(filename).suffix.lower() in self.supported_extensions


@dataclass(frozen=True)
class ParseRequest:
    filename: str
    content: bytes
    assets_dir: Path
    options: dict[str, Any] = field(default_factory=dict)
    remote_state: dict[str, Any] = field(default_factory=dict)
    checkpoint: CheckpointCallback | None = None


@dataclass(frozen=True)
class ParsedAsset:
    path: str
    relative_path: str
    name: str
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0
    aliases: tuple[str, ...] = ()
    original_relative_path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "relative_path": self.relative_path,
            "name": self.name,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "aliases": list(self.aliases),
            "original_relative_path": self.original_relative_path,
        }


@dataclass(frozen=True)
class ParseResult:
    markdown: str
    parser_id: str
    parser_version: str
    assets: tuple[ParsedAsset, ...] = ()
    structured_blocks: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()
    parser_metadata: dict[str, Any] = field(default_factory=dict)


class DocumentParser(Protocol):
    parser_id: str

    def capabilities(self) -> ParserCapabilities: ...

    async def health(self) -> tuple[bool, str]: ...

    async def parse(
        self,
        request: ParseRequest,
        on_progress: ProgressCallback | None = None,
    ) -> ParseResult: ...
