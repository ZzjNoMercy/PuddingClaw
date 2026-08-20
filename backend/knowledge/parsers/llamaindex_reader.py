"""Small local reader for portable text-like sources.

The name describes the implementation, not a promise that this parser creates
final LlamaIndex nodes. It only emits Markdown; the shared indexer owns nodes.
"""

from __future__ import annotations

from knowledge.parsers.contracts import (
    ParserCapabilities,
    ParseRequest,
    ParserError,
    ParseResult,
    ProgressCallback,
)


class LlamaIndexReaderParser:
    parser_id = "llamaindex_reader"

    def capabilities(self) -> ParserCapabilities:
        return ParserCapabilities(
            parser_id=self.parser_id,
            name="内置快速解析",
            description="快速读取 Markdown 与纯文本并生成可迁移 Markdown；不做 OCR 或版面恢复。",
            location="local",
            supported_extensions=(".md", ".markdown", ".txt"),
            version="builtin-text-v1",
        )

    async def health(self) -> tuple[bool, str]:
        return True, "内置快速解析可用"

    async def parse(
        self,
        request: ParseRequest,
        on_progress: ProgressCallback | None = None,
    ) -> ParseResult:
        if on_progress:
            await on_progress("parsing", 40, "读取文本内容")
        text = request.content.decode("utf-8", errors="replace").strip()
        if not text:
            raise ParserError("文件没有可解析的文本内容")
        return ParseResult(
            markdown=text,
            parser_id=self.parser_id,
            parser_version="builtin-text-v1",
        )
