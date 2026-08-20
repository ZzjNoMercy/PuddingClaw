"""Official MinerU precision and Agent-light cloud API adapters."""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

import httpx

from knowledge.parsers.cloud_common import (
    api_url,
    download_bytes,
    extract_mineru_archive,
    localize_remote_images,
    response_json,
)
from knowledge.parsers.contracts import ParserCapabilities, ParseRequest, ParserError, ParseResult, ProgressCallback

DEFAULT_MINERU_BASE_URL = "https://mineru.net"


def _number(options: dict[str, Any], key: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(options.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


async def _checkpoint(request: ParseRequest, **patch: Any) -> None:
    if request.checkpoint is not None:
        await request.checkpoint(patch)


class MinerUCloudPreciseParser:
    parser_id = "mineru_cloud_precise"

    def __init__(self, *, base_url: str = DEFAULT_MINERU_BASE_URL, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._injected_client = client

    def capabilities(self) -> ParserCapabilities:
        return ParserCapabilities(
            parser_id=self.parser_id,
            name="MinerU 云端精准解析",
            description="MinerU 官方高质量版面解析，返回可迁移的 Markdown、图片和结构数据。",
            location="cloud",
            supported_extensions=(".pdf",),
            supports_assets=True,
            supports_tables=True,
            requires_credential=True,
            credential_env="MINERU_API_TOKEN",
            cloud_data_notice="原始文件将发送到 MinerU 云端。",
            version="mineru-api-v4",
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    async def health(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "未配置 MinerU Token"
        try:
            if self._injected_client is not None:
                response = await self._injected_client.get(api_url(self.base_url, "/api/v4/quota"), headers=self._headers())
                response_json(response, provider="MinerU")
            else:
                async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                    response = await client.get(api_url(self.base_url, "/api/v4/quota"), headers=self._headers())
                    response_json(response, provider="MinerU")
        except (httpx.HTTPError, ParserError) as exc:
            return False, f"MinerU 精准 API 不可用：{exc}"
        return True, "MinerU 精准 API 与 Token 可用"

    async def parse(self, request: ParseRequest, on_progress: ProgressCallback | None = None) -> ParseResult:
        if len(request.content) > 200 * 1024 * 1024:
            raise ParserError("MinerU 精准 API 单文件不能超过 200 MB")
        if not self.api_key:
            raise ParserError("未配置 MinerU Token")
        if self._injected_client is not None:
            return await self._parse(self._injected_client, request, on_progress)
        timeout = httpx.Timeout(connect=20.0, read=120.0, write=120.0, pool=20.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            return await self._parse(client, request, on_progress)

    async def _parse(
        self,
        client: httpx.AsyncClient,
        request: ParseRequest,
        on_progress: ProgressCallback | None,
    ) -> ParseResult:
        options = request.options
        remote = request.remote_state if request.remote_state.get("parser_id") == self.parser_id else {}
        batch_id = str(remote.get("batch_id") or "")
        data_id = str(remote.get("data_id") or f"pc-{hashlib.sha256(request.content).hexdigest()[:32]}")
        if not batch_id:
            if on_progress:
                await on_progress("uploading_remote", 30, "向 MinerU 申请加密上传地址")
            file_options: dict[str, Any] = {"name": request.filename, "data_id": data_id}
            for source, target in (("is_ocr", "is_ocr"), ("page_ranges", "page_ranges")):
                if source in options:
                    file_options[target] = options[source]
            payload: dict[str, Any] = {
                "files": [file_options],
                "model_version": str(options.get("model_version") or "vlm"),
                "enable_table": bool(options.get("enable_table", True)),
                "enable_formula": bool(options.get("enable_formula", True)),
                "language": str(options.get("language") or "ch"),
            }
            response = await client.post(
                api_url(self.base_url, "/api/v4/file-urls/batch"),
                headers=self._headers(),
                json=payload,
            )
            body = response_json(response, provider="MinerU")
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            batch_id = str(data.get("batch_id") or "")
            file_urls = data.get("file_urls") if isinstance(data.get("file_urls"), list) else []
            if not batch_id or not file_urls:
                raise ParserError("MinerU 未返回 batch_id 或上传地址")
            upload = await client.put(str(file_urls[0]), content=request.content)
            try:
                upload.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ParserError(f"上传文件到 MinerU 失败（HTTP {upload.status_code}）") from exc
            await _checkpoint(
                request,
                parser_id=self.parser_id,
                provider="mineru",
                batch_id=batch_id,
                data_id=data_id,
                phase="uploaded",
            )

        interval = _number(options, "poll_interval_seconds", 3.0, minimum=0.0, maximum=30.0)
        deadline = time.monotonic() + _number(options, "max_wait_seconds", 1800.0, minimum=1.0, maximum=7200.0)
        result: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            response = await client.get(
                api_url(self.base_url, f"/api/v4/extract-results/batch/{batch_id}"), headers=self._headers()
            )
            body = response_json(response, provider="MinerU")
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            rows = data.get("extract_result") if isinstance(data.get("extract_result"), list) else []
            result = next(
                (
                    row
                    for row in rows
                    if isinstance(row, dict)
                    and (str(row.get("data_id") or "") == data_id or str(row.get("file_name") or "") == request.filename)
                ),
                rows[0] if rows and isinstance(rows[0], dict) else None,
            )
            state = str((result or {}).get("state") or "pending").lower()
            await _checkpoint(request, parser_id=self.parser_id, batch_id=batch_id, data_id=data_id, phase="polling", state=state)
            if state == "done":
                break
            if state == "failed":
                raise ParserError(f"MinerU 解析失败：{(result or {}).get('err_msg') or '未知错误'}")
            if on_progress:
                progress = (result or {}).get("extract_progress")
                message = "MinerU 云端正在解析"
                if isinstance(progress, dict) and progress.get("total_pages"):
                    message += f"（{progress.get('extracted_pages', 0)}/{progress['total_pages']} 页）"
                await on_progress("waiting_remote", 50, message)
            await asyncio.sleep(interval)
        else:
            raise ParserError(f"MinerU 云端任务等待超时，可使用 batch_id {batch_id} 重试续跑")

        zip_url = str((result or {}).get("full_zip_url") or "")
        if not zip_url:
            raise ParserError("MinerU 完成任务未返回结果 Zip 地址")
        if on_progress:
            await on_progress("downloading_result", 65, "下载并本地化 MinerU 结果")
        archive = await download_bytes(client, zip_url, provider="MinerU")
        markdown, assets, blocks = extract_mineru_archive(archive, request.assets_dir)
        await _checkpoint(request, parser_id=self.parser_id, batch_id=batch_id, data_id=data_id, phase="downloaded", state="done")
        return ParseResult(
            markdown=markdown,
            parser_id=self.parser_id,
            parser_version="mineru-api-v4",
            assets=assets,
            structured_blocks=blocks,
            parser_metadata={"remote_task_id": batch_id, "data_id": data_id, "model_version": options.get("model_version", "vlm")},
        )


class MinerUCloudLightParser:
    parser_id = "mineru_cloud_light"

    def __init__(self, *, base_url: str = DEFAULT_MINERU_BASE_URL, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._injected_client = client

    def capabilities(self) -> ParserCapabilities:
        return ParserCapabilities(
            parser_id=self.parser_id,
            name="MinerU 云端快速解析",
            description="免 Token 的 MinerU Agent 轻量解析，适合 10 MB、20 页以内的 PDF。",
            location="cloud",
            supported_extensions=(".pdf",),
            supports_assets=True,
            supports_tables=True,
            cloud_data_notice="原始文件将发送到 MinerU 云端；接口按 IP 限频。",
            version="mineru-agent-api-v1",
        )

    async def health(self) -> tuple[bool, str]:
        return True, "MinerU 轻量 API 无需 Token；可用性在提交时验证"

    async def parse(self, request: ParseRequest, on_progress: ProgressCallback | None = None) -> ParseResult:
        if len(request.content) > 10 * 1024 * 1024:
            raise ParserError("MinerU 轻量 API 单文件不能超过 10 MB，请改用精准解析")
        if self._injected_client is not None:
            return await self._parse(self._injected_client, request, on_progress)
        timeout = httpx.Timeout(connect=20.0, read=120.0, write=120.0, pool=20.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            return await self._parse(client, request, on_progress)

    async def _parse(
        self,
        client: httpx.AsyncClient,
        request: ParseRequest,
        on_progress: ProgressCallback | None,
    ) -> ParseResult:
        options = request.options
        remote = request.remote_state if request.remote_state.get("parser_id") == self.parser_id else {}
        task_id = str(remote.get("task_id") or "")
        if not task_id:
            if on_progress:
                await on_progress("uploading_remote", 30, "向 MinerU 轻量 API 申请上传地址")
            payload: dict[str, Any] = {
                "file_name": request.filename,
                "language": str(options.get("language") or "ch"),
                "enable_table": bool(options.get("enable_table", True)),
                "is_ocr": bool(options.get("is_ocr", False)),
                "enable_formula": bool(options.get("enable_formula", True)),
            }
            if options.get("page_range"):
                payload["page_range"] = str(options["page_range"])
            response = await client.post(api_url(self.base_url, "/api/v1/agent/parse/file"), json=payload)
            body = response_json(response, provider="MinerU 轻量 API")
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            task_id = str(data.get("task_id") or "")
            file_url = str(data.get("file_url") or "")
            if not task_id or not file_url:
                raise ParserError("MinerU 轻量 API 未返回 task_id 或上传地址")
            upload = await client.put(file_url, content=request.content)
            try:
                upload.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ParserError(f"上传文件到 MinerU 轻量 API 失败（HTTP {upload.status_code}）") from exc
            await _checkpoint(request, parser_id=self.parser_id, provider="mineru", task_id=task_id, phase="uploaded")

        interval = _number(options, "poll_interval_seconds", 3.0, minimum=0.0, maximum=30.0)
        deadline = time.monotonic() + _number(options, "max_wait_seconds", 600.0, minimum=1.0, maximum=1800.0)
        data: dict[str, Any] = {}
        while time.monotonic() < deadline:
            response = await client.get(api_url(self.base_url, f"/api/v1/agent/parse/{task_id}"))
            body = response_json(response, provider="MinerU 轻量 API")
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            state = str(data.get("state") or "pending").lower()
            await _checkpoint(request, parser_id=self.parser_id, task_id=task_id, phase="polling", state=state)
            if state == "done":
                break
            if state == "failed":
                raise ParserError(f"MinerU 轻量解析失败：{data.get('err_msg') or '未知错误'}")
            if on_progress:
                await on_progress("waiting_remote", 50, f"MinerU 轻量 API 状态：{state}")
            await asyncio.sleep(interval)
        else:
            raise ParserError(f"MinerU 轻量任务等待超时，可使用 task_id {task_id} 重试续跑")

        markdown_url = str(data.get("markdown_url") or "")
        if not markdown_url:
            raise ParserError("MinerU 轻量任务未返回 Markdown 地址")
        if on_progress:
            await on_progress("downloading_result", 65, "下载并本地化 MinerU Markdown")
        markdown_bytes = await download_bytes(client, markdown_url, provider="MinerU 轻量 Markdown", limit=64 * 1024 * 1024)
        try:
            markdown = markdown_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ParserError("MinerU 轻量 Markdown 编码无效") from exc
        if not markdown.strip():
            raise ParserError("MinerU 轻量 API 返回了空 Markdown")
        assets = await localize_remote_images(client, markdown, request.assets_dir, provider="MinerU 图片")
        await _checkpoint(request, parser_id=self.parser_id, task_id=task_id, phase="downloaded", state="done")
        return ParseResult(
            markdown=markdown,
            parser_id=self.parser_id,
            parser_version="mineru-agent-api-v1",
            assets=assets,
            parser_metadata={"remote_task_id": task_id, "rate_limit_scope": "ip"},
        )
