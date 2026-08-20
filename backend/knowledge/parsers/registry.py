"""Parser catalog, configuration and credential-safe adapter construction."""

from __future__ import annotations

import asyncio
import os
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from config import get_knowledge_mineru_config, load_config, save_config
from knowledge.parsers.contracts import DocumentParser, ParseRequest, ParserError, ParseResult, ProgressCallback
from knowledge.parsers.dependency_installer import install_status
from knowledge.parsers.llama_parse_cloud import LlamaParseCloudParser
from knowledge.parsers.llamaindex_reader import LlamaIndexReaderParser
from knowledge.parsers.mineru_cloud import MinerUCloudLightParser, MinerUCloudPreciseParser
from knowledge.parsers.mineru_local import MinerULocalParser
from knowledge.parsers.native_file import NativeFileImportParser
from knowledge.parsers.unstructured_local import UnstructuredLocalParser
from provider_registry import LocalCredentialStore

DEFAULT_PARSERS: dict[str, dict[str, Any]] = {
    "mineru_local": {"enabled": True, "priority": 10},
    "llamaindex_reader": {"enabled": True, "priority": 20},
    "native_file": {"enabled": True, "priority": 25},
    "unstructured_local": {"enabled": False, "priority": 30},
    "mineru_cloud_precise": {
        "enabled": False,
        "priority": 40,
        "base_url": "https://mineru.net",
        "credential_ref": "env://MINERU_API_TOKEN",
    },
    "mineru_cloud_light": {"enabled": False, "priority": 50, "base_url": "https://mineru.net"},
    "llama_parse_cloud": {
        "enabled": False,
        "priority": 60,
        "base_url": "https://api.cloud.llamaindex.ai",
        "credential_ref": "env://LLAMA_CLOUD_API_KEY",
    },
}

def _parser_config() -> dict[str, dict[str, Any]]:
    raw = load_config().get("knowledge", {}).get("parsers", {})
    configured = raw.get("items", raw) if isinstance(raw, dict) else {}
    result = deepcopy(DEFAULT_PARSERS)
    if isinstance(configured, dict):
        for parser_id, value in configured.items():
            if parser_id in result and isinstance(value, dict):
                result[parser_id].update(value)
    return result


def _resolve_credential(reference: str) -> str:
    reference = str(reference or "")
    if reference.startswith("env://"):
        return os.getenv(reference.removeprefix("env://"), "")
    return LocalCredentialStore().get(reference)


class DocumentParserRegistry:
    def __init__(self) -> None:
        self._health_cache: dict[str, tuple[float, bool, str]] = {}

    def configuration(self) -> dict[str, dict[str, Any]]:
        return _parser_config()

    def invalidate_health(self, parser_id: str | None = None) -> None:
        if parser_id is None:
            self._health_cache.clear()
        else:
            self._health_cache.pop(parser_id, None)

    def _build(
        self,
        parser_id: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> DocumentParser:
        config = self.configuration().get(parser_id)
        if config is None:
            raise ParserError(f"未知解析器：{parser_id}")
        if parser_id == "mineru_local":
            resolved_base_url = base_url if base_url is not None else str(config.get("base_url") or "")
            return MinerULocalParser(base_url=resolved_base_url or None)
        if parser_id == "llamaindex_reader":
            return LlamaIndexReaderParser()
        if parser_id == "native_file":
            return NativeFileImportParser()
        if parser_id == "unstructured_local":
            return UnstructuredLocalParser()
        if parser_id == "mineru_cloud_precise":
            return MinerUCloudPreciseParser(
                base_url=base_url if base_url is not None else str(config.get("base_url") or "https://mineru.net"),
                api_key=api_key if api_key is not None else _resolve_credential(str(config.get("credential_ref") or "")),
            )
        if parser_id == "mineru_cloud_light":
            return MinerUCloudLightParser(
                base_url=base_url if base_url is not None else str(config.get("base_url") or "https://mineru.net")
            )
        if parser_id == "llama_parse_cloud":
            return LlamaParseCloudParser(
                base_url=base_url
                if base_url is not None
                else str(config.get("base_url") or "https://api.cloud.llamaindex.ai"),
                api_key=api_key if api_key is not None else _resolve_credential(str(config.get("credential_ref") or "")),
            )
        raise ParserError(f"解析器尚未实现：{parser_id}")

    async def probe(
        self,
        parser_id: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> tuple[bool, str]:
        """Test draft connection values without saving or polluting the health cache."""

        parser = self._build(parser_id, base_url=base_url, api_key=api_key)
        return await parser.health()

    async def _health(self, parser: DocumentParser) -> tuple[bool, str]:
        cached = self._health_cache.get(parser.parser_id)
        now = time.monotonic()
        if cached and now - cached[0] < 15.0:
            return cached[1], cached[2]
        healthy, message = await parser.health()
        self._health_cache[parser.parser_id] = (now, healthy, message)
        return healthy, message

    def update(self, parser_id: str, patch: dict[str, Any], *, api_key: str = "") -> dict[str, Any]:
        if parser_id not in DEFAULT_PARSERS:
            raise ParserError(f"未知解析器：{parser_id}")
        allowed = {"enabled", "priority", "base_url", "credential_ref", "default_options"}
        item_patch = {key: value for key, value in patch.items() if key in allowed}
        config = load_config()
        knowledge = config.setdefault("knowledge", {})
        parsers = knowledge.setdefault("parsers", {})
        items = parsers.setdefault("items", {})
        current = dict(items.get(parser_id) or {})
        if api_key.strip():
            reference = LocalCredentialStore().put(f"parser-{parser_id}", api_key.strip())
            item_patch["credential_ref"] = reference
        current.update(item_patch)
        items[parser_id] = current
        save_config(config)
        self._health_cache.pop(parser_id, None)
        return current

    async def catalog(
        self,
        *,
        filename: str = "",
        file_size: int | None = None,
        page_count: int | None = None,
    ) -> list[dict[str, Any]]:
        config = self.configuration()
        suffix = Path(filename).suffix.lower() if filename else ""
        rows: list[dict[str, Any]] = []
        health_tasks: list[tuple[int, asyncio.Task[tuple[bool, str]]]] = []
        for parser_id, item in config.items():
            parser = self._build(parser_id)
            caps = parser.capabilities()
            credential_ref = str(item.get("credential_ref") or "")
            row = {
                "id": parser_id,
                "name": caps.name,
                "description": caps.description,
                "location": caps.location,
                "supported_extensions": list(caps.supported_extensions),
                "supports_assets": caps.supports_assets,
                "supports_tables": caps.supports_tables,
                "requires_credential": caps.requires_credential,
                "credential_env": caps.credential_env,
                "cloud_data_notice": caps.cloud_data_notice,
                "version": caps.version,
                "implementation_available": True,
                "enabled": bool(item.get("enabled", False)),
                "priority": int(item.get("priority") or 100),
                "credential_configured": bool(_resolve_credential(credential_ref))
                if caps.requires_credential
                else True,
                "credential_source": "environment"
                if credential_ref.startswith("env://")
                else "vault"
                if credential_ref
                else "none",
                "base_url": str(
                    item.get("base_url")
                    or (get_knowledge_mineru_config().get("base_url") if parser_id == "mineru_local" else "")
                ),
            }
            if parser_id == "unstructured_local":
                row["dependency_install"] = install_status(parser_id)
                row["dependency_extra"] = "unstructured"
            rows.append(row)
            if not suffix or suffix in caps.supported_extensions:
                health_tasks.append((len(rows) - 1, asyncio.create_task(self._health(parser))))
            else:
                row["available"] = False
                row["healthy"] = False
                row["health_message"] = f"不支持 {suffix} 文件"
        for index, task in health_tasks:
            try:
                healthy, message = await task
            except Exception as exc:  # noqa: BLE001
                healthy, message = False, f"健康检查失败：{type(exc).__name__}"
            rows[index]["available"] = bool(
                rows[index]["enabled"]
                and rows[index]["implementation_available"]
                and rows[index]["credential_configured"]
                and healthy
            )
            rows[index]["healthy"] = healthy
            rows[index]["health_message"] = message
        for row in rows:
            compatible = not suffix or suffix in row["supported_extensions"]
            if row["id"] == "mineru_cloud_light" and file_size is not None and file_size > 10 * 1024 * 1024:
                compatible = False
                row["constraint_reason"] = "文件超过 MinerU 轻量 API 的 10 MB 限制"
            if row["id"] == "mineru_cloud_light" and page_count is not None and page_count > 20:
                compatible = False
                row["constraint_reason"] = "PDF 超过 MinerU 轻量 API 的 20 页限制"
            if row["id"] == "mineru_cloud_precise" and file_size is not None and file_size > 200 * 1024 * 1024:
                compatible = False
                row["constraint_reason"] = "文件超过 MinerU 精准 API 的 200 MB 限制"
            row["compatible"] = compatible
            row["selectable"] = bool(row["available"] and compatible)
        rows.sort(key=lambda row: (int(row["priority"]), str(row["id"])))
        local_selectable = [row for row in rows if row["selectable"] and row["location"] == "local"]
        preferred = (
            local_selectable[0]["id"]
            if local_selectable
            else next((row["id"] for row in rows if row["selectable"]), "")
        )
        for row in rows:
            row["recommended"] = row["id"] == preferred
            if not row["compatible"]:
                row["reason"] = str(row.get("constraint_reason") or f"不支持 {suffix} 文件")
            elif row["recommended"]:
                row["reason"] = (
                    "优先选择可用的本地解析器"
                    if row["location"] == "local"
                    else "当前首选的可用解析器"
                )
            else:
                row["reason"] = row.get("health_message") or row["description"]
        return rows

    async def parse(
        self,
        parser_id: str,
        request: ParseRequest,
        on_progress: ProgressCallback | None = None,
    ) -> ParseResult:
        item = self.configuration().get(parser_id)
        if not item or not bool(item.get("enabled", False)):
            raise ParserError(f"解析器未启用：{parser_id}")
        parser = self._build(parser_id)
        if not parser.capabilities().supports(request.filename):
            raise ParserError(f"{parser_id} 不支持文件：{request.filename}")
        healthy, message = await parser.health()
        if not healthy:
            raise ParserError(message)
        return await parser.parse(request, on_progress=on_progress)


_REGISTRY: DocumentParserRegistry | None = None


def get_document_parser_registry() -> DocumentParserRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = DocumentParserRegistry()
    return _REGISTRY
