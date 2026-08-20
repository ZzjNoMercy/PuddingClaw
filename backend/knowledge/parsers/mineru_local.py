"""Adapter for the existing local MinerU HTTP runtime."""

from __future__ import annotations

from pathlib import Path

import httpx

from knowledge.mineru_client import MinerUClient, MinerUClientError
from knowledge.parsers.contracts import (
    ParsedAsset,
    ParserCapabilities,
    ParseRequest,
    ParserError,
    ParseResult,
    ProgressCallback,
)


def _asset(payload: dict) -> ParsedAsset:
    path = str(payload.get("path") or "")
    relative = str(payload.get("relative_path") or Path(path).name)
    aliases = payload.get("aliases") if isinstance(payload.get("aliases"), list) else []
    return ParsedAsset(
        path=path,
        relative_path=relative,
        name=str(payload.get("name") or Path(path).name),
        mime_type=str(payload.get("mime_type") or "application/octet-stream"),
        size_bytes=int(payload.get("size_bytes") or 0),
        aliases=tuple(str(item) for item in aliases),
        original_relative_path=str(payload.get("original_relative_path") or relative),
    )


class MinerULocalParser:
    parser_id = "mineru_local"

    def __init__(self, *, base_url: str | None = None) -> None:
        self.client = MinerUClient(base_url=base_url)

    def capabilities(self) -> ParserCapabilities:
        return ParserCapabilities(
            parser_id=self.parser_id,
            name="MinerU 本地解析",
            description="适合中文 PDF、公式、表格和图片；文件不离开本机。",
            location="local",
            supported_extensions=(".pdf",),
            supports_assets=True,
            supports_tables=True,
            version="local-http-v1",
        )

    async def health(self) -> tuple[bool, str]:
        base_url = self.client.base_url
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
                response = await client.get(f"{base_url}/docs")
            if response.status_code < 500:
                return True, f"本地 MinerU 可用：{base_url}"
            return False, f"本地 MinerU 返回 HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"本地 MinerU 不可达：{type(exc).__name__}"

    async def parse(
        self,
        request: ParseRequest,
        on_progress: ProgressCallback | None = None,
    ) -> ParseResult:
        if on_progress:
            await on_progress("parsing", 35, "MinerU 正在恢复 PDF 版面")
        try:
            result = await self.client.parse_pdf_bytes(
                filename=request.filename,
                content=request.content,
                assets_dir=request.assets_dir,
            )
        except MinerUClientError as exc:
            raise ParserError(str(exc)) from exc
        markdown = result.markdown.strip()
        if not markdown:
            raise ParserError("MinerU returned empty markdown")
        return ParseResult(
            markdown=markdown,
            parser_id=self.parser_id,
            parser_version="local-http-v1",
            assets=tuple(_asset(item) for item in (result.assets or []) if isinstance(item, dict)),
            parser_metadata={"response": result.raw_response},
        )
