"""LlamaParse v2 adapter bounded to PDF -> portable Markdown/assets."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from knowledge.parsers.cloud_common import (
    api_url,
    download_bytes,
    extract_markdown,
    localize_remote_images,
    response_json,
)
from knowledge.parsers.contracts import ParserCapabilities, ParseRequest, ParserError, ParseResult, ProgressCallback

DEFAULT_LLAMA_CLOUD_BASE_URL = "https://api.cloud.llamaindex.ai"


def _number(options: dict[str, Any], key: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(options.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _status(payload: dict[str, Any]) -> str:
    for candidate in (payload, payload.get("parsing_job"), payload.get("job"), payload.get("data")):
        if isinstance(candidate, dict) and candidate.get("status"):
            return str(candidate["status"]).upper()
    return "PENDING"


def _find_download_url(payload: Any, *, parent: str = "") -> str:
    if isinstance(payload, list):
        for item in payload:
            found = _find_download_url(item, parent=parent)
            if found:
                return found
        return ""
    if not isinstance(payload, dict):
        return ""
    for key, value in payload.items():
        key_lower = str(key).lower()
        context = f"{parent}.{key_lower}" if parent else key_lower
        if key_lower in {"url", "download_url"} and isinstance(value, str) and "markdown" in parent:
            return value
        found = _find_download_url(value, parent=context)
        if found:
            return found
    return ""


async def _checkpoint(request: ParseRequest, **patch: Any) -> None:
    if request.checkpoint is not None:
        await request.checkpoint(patch)


class LlamaParseCloudParser:
    parser_id = "llama_parse_cloud"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_LLAMA_CLOUD_BASE_URL,
        api_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._injected_client = client

    def capabilities(self) -> ParserCapabilities:
        return ParserCapabilities(
            parser_id=self.parser_id,
            name="LlamaParse 云端 PDF 解析",
            description="仅将复杂 PDF 转为 Markdown 和本地图片；切片与索引仍由 PuddingClaw 统一执行。",
            location="cloud",
            supported_extensions=(".pdf",),
            supports_assets=True,
            supports_tables=True,
            requires_credential=True,
            credential_env="LLAMA_CLOUD_API_KEY",
            cloud_data_notice="原始 PDF 将发送到 LlamaCloud。",
            version="llamaparse-api-v2",
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    async def health(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "未配置 LlamaCloud API Key"
        try:
            if self._injected_client is not None:
                response = await self._injected_client.get(
                    api_url(self.base_url, "/api/v2/parse/versions"), headers=self._headers()
                )
                response_json(response, provider="LlamaCloud")
            else:
                async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                    response = await client.get(
                        api_url(self.base_url, "/api/v2/parse/versions"), headers=self._headers()
                    )
                    response_json(response, provider="LlamaCloud")
        except (httpx.HTTPError, ParserError) as exc:
            return False, f"LlamaCloud 不可用：{exc}"
        return True, "LlamaCloud API Key 可用"

    async def parse(self, request: ParseRequest, on_progress: ProgressCallback | None = None) -> ParseResult:
        if not self.api_key:
            raise ParserError("未配置 LlamaCloud API Key")
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
        file_id = str(remote.get("file_id") or "")
        job_id = str(remote.get("job_id") or "")

        if not file_id and not job_id:
            if on_progress:
                await on_progress("uploading_remote", 30, "上传 PDF 到 LlamaCloud")
            response = await client.post(
                api_url(self.base_url, "/api/v1/beta/files"),
                headers=self._headers(),
                data={"purpose": "parse"},
                files={"file": (request.filename, request.content, "application/pdf")},
            )
            body = response_json(response, provider="LlamaCloud")
            file_id = str(body.get("id") or body.get("file_id") or "")
            if not file_id:
                raise ParserError("LlamaCloud 上传响应缺少 file_id")
            await _checkpoint(request, parser_id=self.parser_id, provider="llama_cloud", file_id=file_id, phase="uploaded")

        if not job_id:
            if on_progress:
                await on_progress("waiting_remote", 40, "创建 LlamaParse 解析任务")
            payload: dict[str, Any] = {
                "file_id": file_id,
                "tier": str(options.get("tier") or "agentic"),
                "version": str(options.get("version") or "latest"),
                "output_options": {
                    "images_to_save": list(options.get("images_to_save") or ["embedded", "layout"]),
                    "markdown": {"inline_images": False},
                },
            }
            if options.get("target_pages"):
                payload["target_pages"] = str(options["target_pages"])
            if options.get("lang"):
                payload["lang"] = str(options["lang"])
            if options.get("custom_prompt"):
                payload["agentic_options"] = {"custom_prompt": str(options["custom_prompt"])[:4000]}
            response = await client.post(api_url(self.base_url, "/api/v2/parse"), headers=self._headers(), json=payload)
            body = response_json(response, provider="LlamaParse")
            job_id = str(body.get("id") or body.get("job_id") or "")
            if not job_id:
                raise ParserError("LlamaParse 创建响应缺少 job_id")
            await _checkpoint(
                request,
                parser_id=self.parser_id,
                provider="llama_cloud",
                file_id=file_id,
                job_id=job_id,
                phase="submitted",
            )

        interval = _number(options, "poll_interval_seconds", 3.0, minimum=0.0, maximum=30.0)
        deadline = time.monotonic() + _number(options, "max_wait_seconds", 1800.0, minimum=1.0, maximum=7200.0)
        body: dict[str, Any] = {}
        while time.monotonic() < deadline:
            response = await client.get(
                api_url(self.base_url, f"/api/v2/parse/{job_id}"),
                headers=self._headers(),
                params=[("expand", "markdown")],
            )
            body = response_json(response, provider="LlamaParse")
            state = _status(body)
            await _checkpoint(
                request,
                parser_id=self.parser_id,
                file_id=file_id,
                job_id=job_id,
                phase="polling",
                state=state,
            )
            if state == "COMPLETED":
                break
            if state in {"FAILED", "CANCELLED"}:
                error = body.get("error_message") or body.get("error") or "未知错误"
                raise ParserError(f"LlamaParse 解析失败：{error}")
            if on_progress:
                await on_progress("waiting_remote", 50, f"LlamaParse 状态：{state}")
            await asyncio.sleep(interval)
        else:
            raise ParserError(f"LlamaParse 任务等待超时，可使用 job_id {job_id} 重试续跑")

        if on_progress:
            await on_progress("downloading_result", 65, "下载并本地化 LlamaParse 结果")
        markdown = extract_markdown(body)
        if not markdown:
            markdown_url = _find_download_url(body)
            if markdown_url:
                raw = await download_bytes(client, markdown_url, provider="LlamaParse Markdown", limit=64 * 1024 * 1024)
                try:
                    markdown = raw.decode("utf-8-sig").strip()
                except UnicodeDecodeError as exc:
                    raise ParserError("LlamaParse Markdown 编码无效") from exc
        if not markdown:
            raise ParserError("LlamaParse 完成任务未返回 Markdown")
        assets = await localize_remote_images(client, markdown, request.assets_dir, provider="LlamaParse 图片")
        await _checkpoint(
            request,
            parser_id=self.parser_id,
            file_id=file_id,
            job_id=job_id,
            phase="downloaded",
            state="COMPLETED",
        )

        warnings: list[str] = []
        # Keep the remote source by default so a worker crash after this method
        # returns can still resume until the local document commit succeeds.
        # Deployments with stricter retention can opt into immediate cleanup.
        if bool(options.get("delete_remote_file", False)) and file_id:
            try:
                response = await client.delete(
                    api_url(self.base_url, f"/api/v1/beta/files/{file_id}"), headers=self._headers()
                )
                response.raise_for_status()
            except httpx.HTTPError:
                warnings.append("LlamaCloud 远端临时文件清理失败；本地结果不受影响")
        tier = str(body.get("tier") or options.get("tier") or "agentic")
        version = str(body.get("version") or options.get("version") or "latest")
        return ParseResult(
            markdown=markdown,
            parser_id=self.parser_id,
            parser_version=f"llamaparse-v2:{tier}:{version}",
            assets=assets,
            warnings=tuple(warnings),
            parser_metadata={"remote_task_id": job_id, "remote_file_id": file_id, "tier": tier, "version": version},
        )
