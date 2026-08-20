"""Shared safety and localization helpers for cloud document parsers."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import stat
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from knowledge.parsers.contracts import ParsedAsset, ParserError

MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_FILES = 5000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024


def api_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def response_json(response: httpx.Response, *, provider: str) -> dict[str, Any]:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = response.text[:500].strip()
        raise ParserError(f"{provider} 请求失败（HTTP {response.status_code}）：{detail or '无响应正文'}") from exc
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise ParserError(f"{provider} 返回了无效 JSON") from exc
    if not isinstance(payload, dict):
        raise ParserError(f"{provider} 返回格式无效")
    if "code" in payload and payload.get("code") not in {0, "0", None}:
        raise ParserError(f"{provider} 请求失败：{payload.get('msg') or payload.get('message') or payload.get('code')}")
    return payload


async def download_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    provider: str,
    limit: int = MAX_DOWNLOAD_BYTES,
) -> bytes:
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ParserError(f"{provider} 返回了无效下载地址")
    chunks: list[bytes] = []
    size = 0
    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            declared = int(response.headers.get("content-length") or 0)
            if declared > limit:
                raise ParserError(f"{provider} 返回文件超过 {limit // (1024 * 1024)} MB 安全上限")
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise ParserError(f"{provider} 返回文件超过 {limit // (1024 * 1024)} MB 安全上限")
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise ParserError(f"下载 {provider} 结果失败：{exc}") from exc
    return b"".join(chunks)


def _safe_archive_path(name: str) -> Path | None:
    parts = [part for part in PurePosixPath(name.replace("\\", "/")).parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    clean = [re.sub(r"[^\w. -]", "-", part, flags=re.UNICODE).strip(" .") or "asset" for part in parts]
    return Path(*clean)


def extract_mineru_archive(content: bytes, assets_dir: Path) -> tuple[str, tuple[ParsedAsset, ...], tuple[dict[str, Any], ...]]:
    """Read MinerU output without trusting archive paths or compression sizes."""

    try:
        archive = zipfile.ZipFile(BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ParserError("MinerU 返回的结果不是有效 Zip") from exc
    members = [item for item in archive.infolist() if not item.is_dir()]
    if len(members) > MAX_ARCHIVE_FILES:
        raise ParserError("MinerU 结果文件数量超过安全上限")
    total = sum(max(0, item.file_size) for item in members)
    if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        raise ParserError("MinerU 解压结果超过安全上限")
    for item in members:
        mode = item.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ParserError("MinerU 结果包含不安全的符号链接")
        if _safe_archive_path(item.filename) is None:
            raise ParserError("MinerU 结果包含不安全的文件路径")

    markdown_members = [item for item in members if PurePosixPath(item.filename).name.lower() == "full.md"]
    if not markdown_members:
        markdown_members = [item for item in members if item.filename.lower().endswith(".md")]
    if not markdown_members:
        raise ParserError("MinerU 结果中没有 Markdown 文件")
    markdown_member = min(markdown_members, key=lambda item: (len(PurePosixPath(item.filename).parts), item.filename))
    try:
        markdown = archive.read(markdown_member).decode("utf-8-sig")
    except (UnicodeDecodeError, RuntimeError) as exc:
        raise ParserError("MinerU Markdown 无法读取") from exc
    if not markdown.strip():
        raise ParserError("MinerU 返回了空 Markdown")

    assets_dir.mkdir(parents=True, exist_ok=True)
    markdown_parent = PurePosixPath(markdown_member.filename).parent
    assets: list[ParsedAsset] = []
    structured_blocks: list[dict[str, Any]] = []
    for item in members:
        name = PurePosixPath(item.filename)
        lower_name = name.name.lower()
        if item == markdown_member:
            continue
        if lower_name.endswith("content_list.json") and item.file_size <= MAX_ASSET_BYTES:
            try:
                value = json.loads(archive.read(item))
                if isinstance(value, list):
                    structured_blocks.extend(block for block in value if isinstance(block, dict))
            except (ValueError, json.JSONDecodeError, RuntimeError):
                pass
            continue
        if name.suffix.lower() in {".json", ".md"}:
            continue
        if item.file_size > MAX_ASSET_BYTES:
            continue
        safe_relative = _safe_archive_path(item.filename)
        if safe_relative is None:
            continue
        target = assets_dir / safe_relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(item))
        aliases = [item.filename]
        try:
            aliases.append(name.relative_to(markdown_parent).as_posix())
        except ValueError:
            pass
        mime_type = mimetypes.guess_type(name.name)[0] or "application/octet-stream"
        assets.append(
            ParsedAsset(
                path=str(target),
                relative_path=safe_relative.as_posix(),
                name=name.name,
                mime_type=mime_type,
                size_bytes=target.stat().st_size,
                aliases=tuple(dict.fromkeys(aliases)),
                original_relative_path=item.filename,
            )
        )
    return markdown, tuple(assets), tuple(structured_blocks)


_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?P<url>https?://[^)\s]+)(?:\s+\"[^\"]*\")?\)", re.I)
_HTML_IMAGE_RE = re.compile(r"<img\b[^>]*?\bsrc=[\"'](?P<url>https?://.*?)[\"'][^>]*>", re.I)


async def localize_remote_images(
    client: httpx.AsyncClient,
    markdown: str,
    assets_dir: Path,
    *,
    provider: str,
) -> tuple[ParsedAsset, ...]:
    urls = [match.group("url") for match in _MARKDOWN_IMAGE_RE.finditer(markdown)]
    urls.extend(match.group("url") for match in _HTML_IMAGE_RE.finditer(markdown))
    assets: list[ParsedAsset] = []
    for index, url in enumerate(dict.fromkeys(urls)):
        if index >= 100:
            break
        raw = await download_bytes(client, url, provider=provider, limit=MAX_ASSET_BYTES)
        original_name = Path(unquote(urlparse(url).path)).name or f"image-{index + 1}.bin"
        suffix = Path(original_name).suffix[:12]
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
        filename = f"{Path(original_name).stem[:80] or 'image'}-{digest}{suffix}"
        target = assets_dir / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        assets.append(
            ParsedAsset(
                path=str(target),
                relative_path=filename,
                name=filename,
                mime_type=mimetypes.guess_type(original_name)[0] or "application/octet-stream",
                size_bytes=len(raw),
                aliases=(url, original_name),
                original_relative_path=url,
            )
        )
    return tuple(assets)


def extract_markdown(payload: Any) -> str:
    """Accept documented and SDK-shaped LlamaParse expanded result variants."""

    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, list):
        pages = [extract_markdown(item) for item in payload]
        return "\n\n".join(page for page in pages if page).strip()
    if not isinstance(payload, dict):
        return ""
    for key in ("markdown", "markdown_content", "result", "pages", "data"):
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, str) and key == "markdown":
            return value.strip()
        nested = extract_markdown(value)
        if nested:
            return nested
    for key in ("md", "text", "content"):
        value = payload.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""
