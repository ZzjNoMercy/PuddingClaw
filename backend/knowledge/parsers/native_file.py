"""Compatibility adapter for files handled by existing native import paths.

This catalog entry is intentionally not a document-to-Markdown parser.  It
keeps spreadsheet and Office uploads on their established storage/tooling
pipeline while the two-phase upload UI presents one consistent choice model.
"""

from __future__ import annotations

from knowledge.parsers.contracts import (
    ParserCapabilities,
    ParseRequest,
    ParserError,
    ParseResult,
    ProgressCallback,
)


class NativeFileImportParser:
    parser_id = "native_file"

    def capabilities(self) -> ParserCapabilities:
        return ParserCapabilities(
            parser_id=self.parser_id,
            name="内置文件导入",
            description="保留表格与 Office 原文件，继续交给现有 Pandas 和文件工具处理。",
            location="local",
            supported_extensions=(".xlsx", ".xls", ".csv", ".tsv", ".docx"),
            supports_tables=True,
            version="native-import-v1",
        )

    async def health(self) -> tuple[bool, str]:
        return True, "内置文件导入可用"

    async def parse(
        self,
        request: ParseRequest,
        on_progress: ProgressCallback | None = None,
    ) -> ParseResult:
        raise ParserError("内置文件导入由原有文件流水线处理，不生成 Markdown 解析结果。")
