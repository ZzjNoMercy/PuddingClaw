"""Deterministic read-later capture and promotion into the LLM Wiki pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import html2text
import yaml
from bs4 import BeautifulSoup, Tag
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge.import_jobs import create_llm_wiki_ingest_job, update_job_progress
from knowledge.models import (
    KnowledgeDocument,
    KnowledgeImportEvent,
    KnowledgeImportJob,
    KnowledgeSourceItem,
    ReadLaterItem,
    iso_utc,
    new_id,
)
from knowledge.queue_repository import require_current_lease
from knowledge.service import (
    DEFAULT_KNOWLEDGE_BASE_ID,
    KnowledgeService,
    KnowledgeServiceError,
    assert_writes_allowed_tolerant,
)
from tools.fetch_url_tool import MAX_REDIRECTS, FetchURLTool, _validated_url

READ_LATER_CAPTURE_KIND = "read_later_capture"
MAX_ARTICLE_IMAGES = 16
MIN_ARTICLE_IMAGE_BYTES = 256
_IMAGE_MEDIA_EXTENSIONS = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_MARKDOWN_IMAGE_PATTERN = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\((?P<url>[^)\s]+)(?P<suffix>\s+\"[^\"]*\")?\)"
)
_ARTICLE_ROOT_SELECTORS = (
    "[itemprop='articleBody']",
    "#article-body",
    ".article-content",
    ".article__body",
    ".post-content",
    ".entry-content",
    "article",
    "main",
)
_ARTICLE_NOISE_SELECTORS = (
    "script",
    "style",
    "noscript",
    "nav",
    "footer",
    "header",
    "aside",
    "form",
    "dialog",
    "[hidden]",
    "[aria-hidden='true']",
    ".advertisement",
    ".adsbygoogle",
    ".social-share",
    ".share-buttons",
    ".newsletter",
    ".subscribe",
    ".related-posts",
    "#comments",
    ".comments",
)
_LAZY_IMAGE_ATTRIBUTES = ("data-src", "data-original", "data-lazy-src", "data-actualsrc")
_WECHAT_HEADING_PATTERN = re.compile(r"^(?:\d+[.、．]|[一二三四五六七八九十]+[、.．])\s*\S+")
_X_ARTICLE_IMAGE_PATTERN = re.compile(
    r'original_img_url\s*:\s*"(?P<url>https:(?:\\/|/){2}pbs\.twimg\.com(?:\\/|/)media(?:\\/|/)[^"?]+(?:\?[^" ]*)?)"'
)
_TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "spm", "from",
}


def canonicalize_url(value: str) -> str:
    raw = value.strip()
    _validated_url(raw)
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    netloc = host if parsed.port in {None, default_port} else f"{host}:{parsed.port}"
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
    ]
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    canonical = urlunsplit((parsed.scheme.lower(), netloc, path, urlencode(sorted(query)), ""))
    if len(canonical.encode("utf-8")) > 1800:
        raise KnowledgeServiceError("链接过长，请改用不含临时签名或跟踪参数的原始文章地址")
    return canonical


def read_later_to_dict(item: ReadLaterItem, *, content: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": item.id,
        "knowledge_base_id": item.knowledge_base_id,
        "original_url": item.original_url,
        "canonical_url": item.canonical_url,
        "title": item.title,
        "site_name": item.site_name,
        "author": item.author,
        "description": item.description,
        "image_url": item.image_url,
        "virtual_path": item.virtual_path,
        "content_sha256": item.content_sha256,
        "parse_status": item.parse_status,
        "reading_status": item.reading_status,
        "error_message": item.error_message,
        "tags": item.tags or [],
        "note": item.note,
        "document_id": item.document_id,
        "source_connection_id": item.source_connection_id,
        "source_item_id": item.source_item_id,
        "raw_snapshot_path": item.raw_snapshot_path,
        "wiki_job_id": item.wiki_job_id,
        "fetched_at": iso_utc(item.fetched_at),
        "read_at": iso_utc(item.read_at),
        "created_at": iso_utc(item.created_at),
        "updated_at": iso_utc(item.updated_at),
    }
    if content is not None:
        payload["content"] = content
    return payload


async def create_read_later_item(
    session: AsyncSession,
    *,
    base_dir: Path,
    url: str,
    title: str = "",
    note: str = "",
    tags: list[str] | None = None,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
) -> tuple[ReadLaterItem, KnowledgeImportJob | None, bool]:
    await assert_writes_allowed_tolerant(session)
    canonical_url = canonicalize_url(url)
    service = KnowledgeService(base_dir)
    await service.ensure_default_knowledge_base(session)
    from knowledge.sources import ensure_builtin_source_connections, upsert_source_item

    sources = await ensure_builtin_source_connections(session, knowledge_base_id=knowledge_base_id)
    source = sources["web_capture"]
    existing = (
        await session.execute(
            select(ReadLaterItem).where(
                ReadLaterItem.knowledge_base_id == knowledge_base_id,
                ReadLaterItem.canonical_url == canonical_url,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, None, True

    item_id = new_id("later")
    source_item = await upsert_source_item(
        session,
        source=source,
        external_id=f"read-later:{item_id}",
        external_type="web_page",
        title=title.strip() or canonical_url,
        source_url=canonical_url,
        status="queued",
        metadata={"reading_status": "unread"},
    )
    item = ReadLaterItem(
        id=item_id,
        knowledge_base_id=knowledge_base_id,
        original_url=url.strip(),
        canonical_url=canonical_url,
        title=title.strip(),
        note=note.strip(),
        tags=list(dict.fromkeys(tag.strip() for tag in (tags or []) if tag.strip())),
        parse_status="queued",
        reading_status="unread",
        source_connection_id=source.id,
        source_item_id=source_item.id,
    )
    job = KnowledgeImportJob(
        id=new_id("job"),
        knowledge_base_id=knowledge_base_id,
        status="queued",
        file_name=title.strip() or urlsplit(canonical_url).hostname or "稍后读链接",
        file_type="url",
        file_size=0,
        source_path=canonical_url,
        source_sha256=hashlib.sha256(canonical_url.encode("utf-8")).hexdigest(),
        title=title.strip() or None,
        publish_targets=["read_later"],
        current_step="queued",
        progress=0,
        source_connection_id=source.id,
        source_item_id=source_item.id,
        job_metadata={"kind": READ_LATER_CAPTURE_KIND, "read_later_item_id": item.id},
    )
    session.add_all([item, job, KnowledgeImportEvent(job_id=job.id, level="info", message="链接已收藏，等待后台解析")])
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = (
            await session.execute(
                select(ReadLaterItem).where(
                    ReadLaterItem.knowledge_base_id == knowledge_base_id,
                    ReadLaterItem.canonical_url == canonical_url,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, None, True
        raise
    await session.refresh(item)
    await session.refresh(job)
    return item, job, False


def _meta(soup: BeautifulSoup, *selectors: tuple[str, str]) -> str:
    for attribute, value in selectors:
        node = soup.find("meta", attrs={attribute: value})
        if node and node.get("content"):
            return str(node.get("content")).strip()
    return ""


def _prepare_article_images(root: BeautifulSoup | Tag) -> None:
    for image in root.select("img"):
        lazy_source = next((str(image.get(attribute) or "").strip() for attribute in _LAZY_IMAGE_ATTRIBUTES if image.get(attribute)), "")
        if lazy_source:
            image["src"] = lazy_source
        if not image.get("alt"):
            caption = image.find_parent("figure")
            caption_node = caption.find("figcaption") if caption else None
            image["alt"] = caption_node.get_text(" ", strip=True) if caption_node else "文章图片"


def _prepare_wechat_article(root: BeautifulSoup | Tag) -> None:
    for node in root.select("[style*='display: none'], [style*='display:none']"):
        node.decompose()
    for block in root.find_all("section"):
        text = block.get_text(" ", strip=True)
        styled_heading = bool(
            block.find("strong")
            and (
                _WECHAT_HEADING_PATTERN.match(text)
                or text in {"写在最后", "结语", "总结"}
                or any(
                    re.search(r"font-size\s*:\s*(?:2[0-9]|[3-9][0-9])px", str(node.get("style") or ""), re.IGNORECASE)
                    for node in block.find_all(style=True)
                )
            )
            and len(text) <= 60
        )
        block.name = "h2" if styled_heading else "div"


def _append_x_article_images(markdown: str, *, source_html: str, page_url: str) -> str:
    """Recover X Article images stored in its server-rendered data stream.

    X renders the article body as HTML but keeps non-cover media in Relay data
    as ``original_img_url`` fields. Those images are not represented by ``img``
    elements until client-side hydration, so a deterministic HTTP capture must
    promote them into Markdown before the normal local image cache runs.
    """

    host = (urlsplit(page_url).hostname or "").lower()
    if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return markdown

    existing_urls = {
        urljoin(page_url, match.group("url").strip("<>"))
        for match in _MARKDOWN_IMAGE_PATTERN.finditer(markdown)
    }
    supplemental_urls: list[str] = []
    for match in _X_ARTICLE_IMAGE_PATTERN.finditer(source_html):
        image_url = match.group("url").replace("\\/", "/")
        if image_url in existing_urls or image_url in supplemental_urls:
            continue
        supplemental_urls.append(image_url)
    if not supplemental_urls:
        return markdown

    images = "\n\n".join(
        f"![原文图片 {index}]({image_url})"
        for index, image_url in enumerate(supplemental_urls, start=1)
    )
    return f"{markdown.rstrip()}\n\n## 原文图片\n\n{images}"


def _extract_markdown(html: str, url: str) -> tuple[dict[str, str], str]:
    soup = BeautifulSoup(html, "lxml")
    title = _meta(soup, ("property", "og:title"), ("name", "twitter:title"))
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    if not title:
        heading = soup.find("h1")
        title = heading.get_text(" ", strip=True) if heading else ""
    metadata = {
        "title": title,
        "author": _meta(soup, ("name", "author"), ("property", "article:author")),
        "site_name": _meta(soup, ("property", "og:site_name")),
        "description": _meta(soup, ("name", "description"), ("property", "og:description")),
        "image_url": _meta(soup, ("property", "og:image"), ("name", "twitter:image")),
    }
    if metadata["image_url"]:
        metadata["image_url"] = urljoin(url, metadata["image_url"])
    host = (urlsplit(url).hostname or "").lower()
    is_wechat = host == "mp.weixin.qq.com" or host.endswith(".mp.weixin.qq.com")
    if is_wechat:
        root = soup.select_one("#js_content")
        author_node = soup.select_one("#js_name, .rich_media_meta_nickname")
        if not metadata["author"] and author_node:
            metadata["author"] = author_node.get_text(" ", strip=True)
        metadata["site_name"] = metadata["site_name"] or "微信公众平台"
    else:
        root = next((soup.select_one(selector) for selector in _ARTICLE_ROOT_SELECTORS if soup.select_one(selector)), None)
    root = root or soup.body or soup
    for tag in root.select(",".join(_ARTICLE_NOISE_SELECTORS)):
        tag.decompose()
    _prepare_article_images(root)
    if is_wechat:
        _prepare_wechat_article(root)
    else:
        # html2text does not treat HTML5 section as a block and otherwise
        # concatenates adjacent paragraphs into one unreadable line.
        for section in root.find_all("section"):
            section.name = "div"
    converter = html2text.HTML2Text()
    converter.ignore_links = False
    # Images are preserved here, then downloaded into the local knowledge
    # assets directory before the Markdown is persisted. The reader never
    # contacts the third-party image host directly.
    converter.ignore_images = False
    converter.body_width = 0
    converter.baseurl = url
    markdown = converter.handle(str(root)).strip()
    markdown = re.sub(r"^(#{1,6})\s+\*\*(.+)\*\*\s*$", r"\1 \2", markdown, flags=re.MULTILINE)
    markdown = re.sub(r"^(#{1,6}\s+)(\d+)\\\.", r"\1\2.", markdown, flags=re.MULTILINE)
    markdown = re.sub(r"[ \t]+\n", "\n", markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    markdown = _append_x_article_images(markdown, source_html=html, page_url=url)
    return metadata, markdown


def _fetch_image(url: str) -> tuple[bytes, str]:
    current = url
    for redirect_count in range(MAX_REDIRECTS + 1):
        response = FetchURLTool._request_once(current)
        if response.status in {301, 302, 303, 307, 308}:
            location = response.headers.get("location", "").strip()
            if not location or redirect_count >= MAX_REDIRECTS:
                raise KnowledgeServiceError("图片重定向异常")
            current = urljoin(current, location)
            _validated_url(current)
            continue
        if not 200 <= response.status < 300:
            raise KnowledgeServiceError(f"图片返回 HTTP {response.status}")
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        extension = _IMAGE_MEDIA_EXTENSIONS.get(media_type)
        if not extension:
            raise KnowledgeServiceError(f"不支持的图片类型：{media_type or 'unknown'}")
        if len(response.body) < MIN_ARTICLE_IMAGE_BYTES:
            raise KnowledgeServiceError("图片过小，可能是跟踪像素")
        return response.body, extension
    raise KnowledgeServiceError("图片重定向次数过多")


def _cache_article_images(
    markdown: str,
    *,
    metadata: dict[str, str],
    page_url: str,
    knowledge_dir: Path,
    item_id: str,
) -> tuple[str, str]:
    """Replace remote Markdown images with local, SSRF-safe asset copies."""

    cover_url = metadata.get("image_url", "").strip()
    source = markdown
    if cover_url and cover_url not in source:
        source = f"![文章封面]({cover_url})\n\n{source}"

    assets_dir = knowledge_dir / "assets" / "read-later" / item_id
    virtual_prefix = f"/knowledge/assets/read-later/{item_id}"
    cached: dict[str, str] = {}
    cached_cover = ""
    image_index = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal cached_cover, image_index
        alt = match.group("alt").strip()
        raw_url = match.group("url").strip("<>")
        resolved_url = urljoin(page_url, raw_url)
        if resolved_url in cached:
            virtual_path = cached[resolved_url]
            return f"![{alt}]({virtual_path}{match.group('suffix') or ''})"
        if image_index >= MAX_ARTICLE_IMAGES or not resolved_url.lower().startswith(("http://", "https://")):
            return f"*图片未保存：{alt or '无标题图片'}*"
        try:
            image_bytes, extension = _fetch_image(resolved_url)
        except Exception:  # noqa: BLE001 - a failed image must not fail the article
            return f"*图片未保存：{alt or '无标题图片'}*"
        image_index += 1
        assets_dir.mkdir(parents=True, exist_ok=True)
        filename = f"image-{image_index:02d}{extension}"
        (assets_dir / filename).write_bytes(image_bytes)
        virtual_path = f"{virtual_prefix}/{filename}"
        cached[resolved_url] = virtual_path
        if cover_url and resolved_url == urljoin(page_url, cover_url):
            cached_cover = virtual_path
        return f"![{alt}]({virtual_path}{match.group('suffix') or ''})"

    rewritten = _MARKDOWN_IMAGE_PATTERN.sub(replace, source)
    return rewritten, cached_cover


def _fetch_and_parse(url: str) -> tuple[dict[str, str], str, str]:
    current = url
    for redirect_count in range(MAX_REDIRECTS + 1):
        response = FetchURLTool._request_once(current)
        if response.status in {301, 302, 303, 307, 308}:
            location = response.headers.get("location", "").strip()
            if not location or redirect_count >= MAX_REDIRECTS:
                raise KnowledgeServiceError("网页重定向异常")
            current = urljoin(current, location)
            _validated_url(current)
            continue
        if not 200 <= response.status < 300:
            raise KnowledgeServiceError(f"网页返回 HTTP {response.status}")
        content_type = response.headers.get("content-type", "").lower()
        media_type = content_type.split(";", 1)[0].strip()
        if media_type and not (media_type.startswith("text/") or media_type in {"application/xhtml+xml"}):
            raise KnowledgeServiceError(f"暂不支持解析 {media_type or '该资源'}")
        text = FetchURLTool._decode(response.body, content_type)
        if media_type == "text/plain":
            return {"title": "", "author": "", "site_name": "", "description": "", "image_url": ""}, text.strip(), current
        metadata, markdown = _extract_markdown(text, current)
        return metadata, markdown, current
    raise KnowledgeServiceError("网页重定向次数过多")


async def process_read_later_capture_job(
    session: AsyncSession, *, base_dir: Path, job: KnowledgeImportJob
) -> KnowledgeImportJob:
    item_id = str((job.job_metadata or {}).get("read_later_item_id") or "")
    item = await session.get(ReadLaterItem, item_id)
    if item is None:
        raise KnowledgeServiceError("稍后读收藏记录不存在")
    item.parse_status = "processing"
    await update_job_progress(session, job, step="fetching", progress=20, message="安全抓取网页正文")
    try:
        metadata, body, final_url = await asyncio.to_thread(_fetch_and_parse, item.original_url)
    except Exception as exc:  # extraction or safety failure keeps the bookmark usable as a link
        await require_current_lease(session, KnowledgeImportJob, job.id)
        item.parse_status = "link_only"
        item.error_message = str(exc)
        item.fetched_at = datetime.now(timezone.utc)
        job.status = "succeeded"
        job.current_step = "link_only"
        job.progress = 100
        job.finished_at = datetime.now(timezone.utc)
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        job.job_metadata = {**(job.job_metadata or {}), "parse_status": "link_only"}
        if item.source_item_id:
            source_item = await session.get(KnowledgeSourceItem, item.source_item_id)
            if source_item is not None:
                source_item.status = "link_only"
                source_item.metadata_json = {
                    **(source_item.metadata_json or {}),
                    "parse_error": str(exc),
                    "reading_status": item.reading_status,
                }
        session.add(KnowledgeImportEvent(job_id=job.id, level="warning", message=f"正文未解析，已保留链接：{exc}"))
        await session.commit()
        await session.refresh(job)
        return job

    await update_job_progress(session, job, step="extracting", progress=60, message="提取标题与 Markdown 正文")
    visible_length = len(re.sub(r"[#>*_`\[\]()\-\s]", "", body))
    if visible_length < 80:
        item.parse_status = "link_only"
        item.error_message = "网页正文过短，可能需要登录或由脚本动态加载"
    else:
        service = KnowledgeService(base_dir)
        body, cached_cover = await asyncio.to_thread(
            _cache_article_images,
            body,
            metadata=metadata,
            page_url=final_url,
            knowledge_dir=service.knowledge_dir,
            item_id=item.id,
        )
        frontmatter = {
            "title": item.title or metadata["title"] or urlsplit(final_url).hostname or "稍后读",
            "source_url": item.original_url,
            "canonical_url": canonicalize_url(final_url),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "site": metadata["site_name"],
            "author": metadata["author"],
        }
        markdown = f"---\n{yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()}\n---\n\n{body}\n"
        markdown_bytes = markdown.encode("utf-8")
        existing_document = await session.get(KnowledgeDocument, item.document_id) if item.document_id else None
        owns_existing_document = bool(
            existing_document
            and existing_document.source_type == "read_later"
            and str((existing_document.doc_metadata or {}).get("read_later_item_id") or "") == item.id
        )
        if owns_existing_document and existing_document is not None:
            document = service.replace_markdown_document_content(
                document=existing_document,
                content=markdown_bytes,
                title=str(frontmatter["title"]),
                publish_targets=["local_markdown", "read_later"],
            )
        else:
            document, _ = await service.ingest_markdown_upload(
                session,
                filename=f"{frontmatter['title']}.md",
                content=markdown_bytes,
                title=str(frontmatter["title"]),
                knowledge_base_id=item.knowledge_base_id,
                publish_targets=["local_markdown", "read_later"],
                publish_vector_now=False,
                source_connection_id=item.source_connection_id,
                source_item_id=item.source_item_id,
                origin_url=item.canonical_url,
            )
        document.source_type = "read_later"
        document.source_path = item.original_url
        document.source_connection_id = item.source_connection_id
        document.source_item_id = item.source_item_id
        document.origin_url = str(frontmatter["canonical_url"])
        document.doc_metadata = {
            **(document.doc_metadata or {}),
            "read_later_item_id": item.id,
            "canonical_url": frontmatter["canonical_url"],
        }
        item.title = str(frontmatter["title"])
        item.site_name = metadata["site_name"]
        item.author = metadata["author"]
        item.description = metadata["description"]
        item.image_url = cached_cover
        item.storage_path = document.storage_path
        item.virtual_path = document.virtual_path
        item.content_sha256 = document.content_sha256
        item.document_id = document.id
        item.parse_status = "ready"
        item.error_message = ""
        if item.source_item_id:
            source_item = await session.get(KnowledgeSourceItem, item.source_item_id)
            if source_item is not None:
                source_item.title = item.title
                source_item.source_url = item.canonical_url
                source_item.content_sha256 = document.content_sha256
                source_item.document_id = document.id
                source_item.status = "ready"
                source_item.metadata_json = {
                    **(source_item.metadata_json or {}),
                    "reading_status": item.reading_status,
                    "site_name": item.site_name,
                    "author": item.author,
                }

    item.fetched_at = datetime.now(timezone.utc)
    await require_current_lease(session, KnowledgeImportJob, job.id)
    job.status = "succeeded"
    job.current_step = "done" if item.parse_status == "ready" else "link_only"
    job.progress = 100
    job.document_id = item.document_id
    job.title = item.title or job.title
    job.finished_at = datetime.now(timezone.utc)
    job.lease_owner = None
    job.lease_expires_at = None
    job.heartbeat_at = None
    job.job_metadata = {**(job.job_metadata or {}), "parse_status": item.parse_status}
    session.add(KnowledgeImportEvent(job_id=job.id, level="info", message="网页已整理为 Markdown" if item.parse_status == "ready" else item.error_message))
    await session.commit()
    await session.refresh(job)
    return job


async def list_read_later_items(
    session: AsyncSession,
    *,
    base_dir: Path,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    reading_status: str = "all",
    parse_status: str = "all",
    search: str = "",
    limit: int = 200,
) -> list[ReadLaterItem]:
    stmt = select(ReadLaterItem).where(ReadLaterItem.knowledge_base_id == knowledge_base_id)
    if reading_status != "all":
        stmt = stmt.where(ReadLaterItem.reading_status == reading_status)
    if parse_status != "all":
        stmt = stmt.where(ReadLaterItem.parse_status == parse_status)
    result_limit = max(1, min(limit, 500))
    candidate_limit = 500 if search.strip() else result_limit
    result = await session.execute(stmt.order_by(ReadLaterItem.created_at.desc()).limit(candidate_limit))
    items = list(result.scalars())
    needle = search.strip().casefold()
    if not needle:
        return items

    knowledge_root = KnowledgeService(base_dir).knowledge_dir.resolve()

    def matches(item: ReadLaterItem) -> bool:
        metadata = " ".join(
            [
                item.title,
                item.site_name,
                item.author,
                item.description,
                item.original_url,
                item.canonical_url,
                item.note,
                " ".join(item.tags or []),
            ]
        ).casefold()
        if needle in metadata:
            return True
        if not item.storage_path:
            return False
        try:
            path = Path(item.storage_path).resolve()
            if not path.is_relative_to(knowledge_root) or not path.is_file():
                return False
            return needle in path.read_text(encoding="utf-8", errors="replace").casefold()
        except OSError:
            return False

    return [item for item in items if matches(item)][:result_limit]


async def delete_read_later_item(
    session: AsyncSession,
    *,
    base_dir: Path,
    item: ReadLaterItem,
) -> dict[str, bool]:
    """Delete one bookmark and only its owned local capture artifacts.

    Raw snapshots, published Wiki pages, GBrain data and task history intentionally remain.
    """

    await assert_writes_allowed_tolerant(session)
    service = KnowledgeService(base_dir)
    document = await session.get(KnowledgeDocument, item.document_id) if item.document_id else None
    source_item = await session.get(KnowledgeSourceItem, item.source_item_id) if item.source_item_id else None
    owns_document = bool(
        document
        and document.source_type == "read_later"
        and str((document.doc_metadata or {}).get("read_later_item_id") or "") == item.id
    )

    job_conditions = [KnowledgeImportJob.source_path == item.canonical_url]
    if item.document_id:
        job_conditions.append(KnowledgeImportJob.document_id == item.document_id)
    related_jobs = list(
        (
            await session.execute(
                select(KnowledgeImportJob).where(or_(*job_conditions))
            )
        ).scalars()
    )
    for job in related_jobs:
        if item.document_id and job.document_id == item.document_id:
            job.document_id = None
        if (
            job.status in {"queued", "running"}
            and str((job.job_metadata or {}).get("read_later_item_id") or "") == item.id
        ):
            job.status = "cancelled"
            job.current_step = "cancelled"
            job.finished_at = datetime.now(timezone.utc)
        if source_item is not None and job.source_item_id == source_item.id:
            job.source_item_id = None

    markdown_path = Path(document.storage_path).resolve() if owns_document and document else None
    assets_dir = (service.knowledge_dir / "assets" / "read-later" / item.id).resolve()
    knowledge_root = service.knowledge_dir.resolve()

    await session.delete(item)
    if source_item is not None:
        await session.delete(source_item)
    if owns_document and document is not None:
        await session.delete(document)
    await session.commit()

    markdown_deleted = False
    if markdown_path and markdown_path.is_relative_to(knowledge_root) and markdown_path.is_file():
        try:
            markdown_path.unlink()
            markdown_deleted = True
        except OSError:
            # The database deletion is authoritative. A transient filesystem
            # cleanup failure must not turn a completed delete into an API 500.
            pass
    assets_deleted = False
    if assets_dir.is_relative_to(knowledge_root) and assets_dir.is_dir():
        try:
            shutil.rmtree(assets_dir)
            assets_deleted = True
        except OSError:
            pass

    return {
        "record_deleted": True,
        "document_deleted": bool(owns_document),
        "markdown_deleted": markdown_deleted,
        "assets_deleted": assets_deleted,
    }


async def retry_read_later_item(
    session: AsyncSession,
    *,
    item: ReadLaterItem,
) -> KnowledgeImportJob:
    if item.parse_status in {"queued", "processing"}:
        raise KnowledgeServiceError("这条收藏已经在解析中")
    item.parse_status = "queued"
    item.error_message = ""
    job = KnowledgeImportJob(
        id=new_id("job"),
        knowledge_base_id=item.knowledge_base_id,
        status="queued",
        file_name=item.title or urlsplit(item.canonical_url).hostname or "稍后读链接",
        file_type="url",
        file_size=0,
        source_path=item.canonical_url,
        source_sha256=hashlib.sha256(item.canonical_url.encode("utf-8")).hexdigest(),
        title=item.title or None,
        publish_targets=["read_later"],
        current_step="queued",
        progress=0,
        job_metadata={"kind": READ_LATER_CAPTURE_KIND, "read_later_item_id": item.id},
    )
    session.add_all([job, KnowledgeImportEvent(job_id=job.id, level="info", message="链接已重新加入解析队列")])
    await session.commit()
    await session.refresh(job)
    return job


async def promote_read_later_to_wiki(
    session: AsyncSession,
    *,
    base_dir: Path,
    item_ids: list[str],
    import_gbrain: bool,
) -> KnowledgeImportJob:
    items = list(
        (
            await session.execute(
                select(ReadLaterItem).where(ReadLaterItem.id.in_(list(dict.fromkeys(item_ids))))
            )
        ).scalars()
    )
    if len(items) != len(set(item_ids)):
        raise KnowledgeServiceError("部分稍后读记录不存在")
    if any(item.parse_status != "ready" or not item.storage_path for item in items):
        raise KnowledgeServiceError("只有已成功解析正文的收藏才能编译 Wiki")
    from knowledge.llm_wiki import get_llm_wiki_service

    wiki = get_llm_wiki_service(base_dir)
    raw_paths: list[str] = []
    for item in items:
        path = Path(item.storage_path)
        record = await asyncio.to_thread(
            wiki.snapshot_raw_file,
            source_id="read-later",
            asset_id=item.id,
            title=item.title,
            path=path,
            source_path=item.virtual_path or item.original_url,
        )
        snapshot_path = str(record.get("snapshot_path") or "")
        item.raw_snapshot_path = snapshot_path
        raw_paths.append(snapshot_path)
    job = await create_llm_wiki_ingest_job(
        session,
        base_dir=base_dir,
        raw_paths=raw_paths,
        import_gbrain=import_gbrain,
    )
    for item in items:
        item.wiki_job_id = job.id
    await session.commit()
    await session.refresh(job)
    return job
