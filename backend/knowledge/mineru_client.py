"""MinerU HTTP client for PDF ingestion."""

from __future__ import annotations

import io
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from config import get_knowledge_mineru_config
from runtime_identity.paths import PuddingClawPaths

DEFAULT_MINERU_URL = "http://localhost:8002"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


class MinerUClientError(RuntimeError):
    pass


@dataclass
class MinerUParseResult:
    markdown: str
    raw_response: dict[str, Any]
    assets: list[dict[str, Any]] | None = None


def _find_markdown(payload: Any) -> str:
    """Best-effort extraction across MinerU API response variants."""

    if isinstance(payload, str):
        return payload if payload.lstrip().startswith("#") or "\n" in payload else ""
    if isinstance(payload, dict):
        for key in (
            "markdown",
            "md",
            "md_content",
            "content",
            "text",
            "result",
        ):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for value in payload.values():
            found = _find_markdown(value)
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = _find_markdown(item)
            if found:
                return found
    return ""


def _safe_asset_name(name: str) -> str:
    return "/".join(part for part in name.replace("\\", "/").split("/") if part and part not in {".", ".."})


def _flatten_asset_relative_path(path: str | Path) -> str:
    """Keep final knowledge assets shallow and readable."""

    return f"images/{Path(str(path)).name}"


def _markdown_image_refs(markdown: str) -> list[str]:
    refs: list[str] = []
    for match in re.finditer(r"!\[[^\]]*\]\((?P<url>[^)\s]+)(?:\s+\"[^\"]*\")?\)", markdown or ""):
        refs.append(match.group("url"))
    for match in re.finditer(r"<img\b[^>]*?\bsrc=[\"'](?P<url>.*?)[\"']", markdown or "", flags=re.IGNORECASE):
        refs.append(match.group("url"))

    clean: list[str] = []
    for ref in refs:
        normalized = ref.replace("\\", "/").strip()
        if not normalized or normalized.startswith(("/", "http://", "https://", "data:")):
            continue
        safe = _safe_asset_name(normalized)
        if safe:
            clean.append(safe)
    return clean


def _new_output_children(output_dir: Path, before: set[Path]) -> list[Path]:
    if not output_dir.exists() or not output_dir.is_dir():
        return []
    output_root = output_dir.resolve()
    children: list[Path] = []
    for child in output_dir.iterdir():
        resolved = child.resolve()
        if resolved in before:
            continue
        if resolved == output_root or output_root not in resolved.parents:
            continue
        children.append(child)
    return children


def _copy_runtime_assets_from_markdown_refs(
    *,
    markdown: str,
    output_dir: Path,
    before: set[Path],
    assets_dir: Path | None,
) -> list[dict[str, Any]]:
    """Copy MinerU runtime images when API returns JSON markdown, not a zip."""

    if assets_dir is None:
        return []
    refs = _markdown_image_refs(markdown)
    if not refs:
        return []

    image_files: list[Path] = []
    for child in _new_output_children(output_dir, before):
        if child.is_file() and child.suffix.lower() in IMAGE_SUFFIXES:
            image_files.append(child)
        elif child.is_dir():
            image_files.extend(path for path in child.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES)

    if not image_files:
        return []

    assets: list[dict[str, Any]] = []
    copied: set[Path] = set()
    for ref in refs:
        ref_path = Path(ref)
        ref_parts = ref_path.parts
        matched: Path | None = None
        for image_path in image_files:
            image_parts = image_path.parts
            if image_path.name != ref_path.name:
                continue
            if len(ref_parts) == 1 or tuple(image_parts[-len(ref_parts):]) == ref_parts:
                matched = image_path
                break
        if matched is None or matched in copied:
            continue
        copied.add(matched)
        target_relative_path = _flatten_asset_relative_path(ref)
        target = assets_dir / target_relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(matched, target)
        assets.append(
            {
                "name": target.name,
                "relative_path": target_relative_path,
                "original_relative_path": ref,
                "aliases": [ref],
                "path": str(target),
                "mime_type": _mime_from_suffix(target.suffix),
                "size_bytes": target.stat().st_size,
                "source": "mineru_runtime_output",
            }
        )
    return assets


def _markdown_from_zip(content: bytes, assets_dir: Path | None = None) -> tuple[str, list[dict[str, Any]]]:
    assets: list[dict[str, Any]] = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        if assets_dir is not None:
            assets_dir.mkdir(parents=True, exist_ok=True)
            for name in names:
                lower = name.lower()
                if not lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")):
                    continue
                safe_name = _safe_asset_name(name)
                if not safe_name:
                    continue
                target_relative_path = _flatten_asset_relative_path(safe_name)
                target = assets_dir / target_relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
                assets.append(
                    {
                        "name": Path(safe_name).name,
                        "relative_path": target_relative_path,
                        "original_relative_path": safe_name,
                        "aliases": [safe_name],
                        "path": str(target),
                        "mime_type": _mime_from_suffix(target.suffix),
                        "size_bytes": target.stat().st_size,
                    }
                )
        preferred = [
            name for name in names
            if name.endswith("full.md") or name.endswith("/full.md") or name.endswith(".md")
        ]
        if not preferred:
            return "", assets
        # Prefer full.md, otherwise the largest markdown file.
        preferred.sort(
            key=lambda name: (
                0 if name.endswith("full.md") or name.endswith("/full.md") else 1,
                -archive.getinfo(name).file_size,
            )
        )
        return archive.read(preferred[0]).decode("utf-8", errors="replace"), assets


def _mime_from_suffix(suffix: str) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }.get(suffix.lower(), "application/octet-stream")


def _mineru_runtime_config() -> tuple[Path, bool]:
    config = get_knowledge_mineru_config()
    paths = PuddingClawPaths.from_environment()
    configured = str(config.get("runtime_output_dir") or "").strip()
    output_dir = Path(configured).expanduser() if configured else paths.temporary() / "mineru-runtime" / "output"
    if configured and not output_dir.is_absolute():
        output_dir = paths.root / output_dir
    return output_dir, bool(config.get("keep_runtime_output", False))


def _snapshot_output_children(output_dir: Path) -> set[Path]:
    if not output_dir.exists() or not output_dir.is_dir():
        return set()
    return {child.resolve() for child in output_dir.iterdir()}


def _cleanup_created_output_children(output_dir: Path, before: set[Path], *, keep_output: bool) -> list[str]:
    """Remove MinerU server-side scratch output created by this request.

    PuddingClaw copies final assets into the user knowledge directory
    (`originals/`, `imported/`, `assets/`). MinerU's own `output/<task_id>`
    directory is only a local runtime scratch directory when we start MinerU
    from `scripts/setup-mineru.py`, so successful imports should not keep a
    third copy forever. Failed imports are intentionally left on disk for
    debugging because they often contain useful intermediate state.
    """

    if keep_output:
        return []
    if not output_dir.exists() or not output_dir.is_dir():
        return []

    removed: list[str] = []
    output_root = output_dir.resolve()
    for child in output_dir.iterdir():
        resolved = child.resolve()
        if resolved in before:
            continue
        if resolved == output_root or output_root not in resolved.parents:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
        removed.append(str(child))
    return removed


class MinerUClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        config = get_knowledge_mineru_config()
        self.base_url = (base_url or config.get("base_url") or DEFAULT_MINERU_URL).rstrip("/")
        connect_timeout = float(config.get("connect_timeout_seconds") or 10)
        read_timeout = float(timeout or config.get("read_timeout_seconds") or 1800)
        self.timeout = httpx.Timeout(
            timeout=None,
            connect=connect_timeout,
            read=read_timeout,
            write=connect_timeout,
            pool=connect_timeout,
        )

    async def parse_pdf_bytes(
        self,
        *,
        filename: str,
        content: bytes,
        assets_dir: Path | None = None,
    ) -> MinerUParseResult:
        if not content:
            raise MinerUClientError("PDF file is empty")

        output_dir, keep_output = _mineru_runtime_config()
        before = _snapshot_output_children(output_dir)
        try:
            result = await self._parse_with_endpoint(
                "/file_parse", filename=filename, content=content, assets_dir=assets_dir
            )
            if result.markdown and not result.assets:
                runtime_assets = _copy_runtime_assets_from_markdown_refs(
                    markdown=result.markdown,
                    output_dir=output_dir,
                    before=before,
                    assets_dir=assets_dir,
                )
                if runtime_assets:
                    result.assets = runtime_assets
                    result.raw_response.setdefault("runtime_assets", {})["copied"] = len(runtime_assets)
            removed = _cleanup_created_output_children(output_dir, before, keep_output=keep_output)
            if removed:
                result.raw_response.setdefault("runtime_cleanup", {})["removed"] = removed
            return result
        except httpx.ReadTimeout as exc:
            read_timeout = getattr(self.timeout, "read", None)
            raise MinerUClientError(
                f"/file_parse: MinerU parsing timed out after {read_timeout:g} seconds. "
                "For very large PDFs, increase knowledge.mineru.read_timeout_seconds in config.json."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise MinerUClientError(f"/file_parse: {type(exc).__name__}: {exc}") from exc

    async def _parse_with_endpoint(
        self,
        endpoint: str,
        *,
        filename: str,
        content: bytes,
        assets_dir: Path | None = None,
    ) -> MinerUParseResult:
        url = f"{self.base_url}{endpoint}"
        files = {"file": (filename, content, "application/pdf")}
        data: dict[str, str | list[str]] = {}
        if endpoint == "/file_parse":
            # MinerU 3.x FastAPI endpoint expects the upload field to be
            # `files` (plural). Sending `file` makes FastAPI reject the
            # request with 422 before MinerU starts parsing.
            files = {"files": (filename, content, "application/pdf")}
            data = {
                "lang_list": ["ch"],
                "backend": "pipeline",
                "parse_method": "auto",
                "formula_enable": "true",
                "table_enable": "true",
                "return_md": "true",
                "return_middle_json": "false",
                "return_model_output": "false",
                "return_content_list": "false",
                "return_images": "true",
                "response_format_zip": "true",
                "return_original_file": "false",
                "start_page_id": "0",
                "end_page_id": "99999",
            }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, files=files, data=data)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text[:1000] if response.text else str(exc)
            raise MinerUClientError(f"{exc.response.status_code} from {endpoint}: {detail}") from exc

        content_type = response.headers.get("content-type", "")
        if "application/zip" in content_type or response.content[:2] == b"PK":
            markdown, assets = _markdown_from_zip(response.content, assets_dir=assets_dir)
            return MinerUParseResult(
                markdown=markdown,
                raw_response={"content_type": content_type, "endpoint": endpoint, "asset_count": len(assets)},
                assets=assets,
            )

        payload = response.json()
        markdown = _find_markdown(payload)
        raw = payload if isinstance(payload, dict) else {"response": payload}
        raw.setdefault("endpoint", endpoint)
        return MinerUParseResult(markdown=markdown, raw_response=raw, assets=[])
