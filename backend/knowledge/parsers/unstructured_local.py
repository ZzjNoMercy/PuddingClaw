"""Optional local Unstructured adapter.

The dependency is intentionally optional because its OCR stack is large. The
registry keeps the parser visible and reports a precise availability reason.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from knowledge.parsers.contracts import (
    ParserCapabilities,
    ParseRequest,
    ParserError,
    ParseResult,
    ProgressCallback,
)


def _installed() -> bool:
    try:
        import unstructured  # noqa: F401

        return True
    except ImportError:
        return False


class UnstructuredLocalParser:
    parser_id = "unstructured_local"

    def capabilities(self) -> ParserCapabilities:
        return ParserCapabilities(
            parser_id=self.parser_id,
            name="Unstructured 本地解析",
            description="本地解析 Office、一般 PDF 与 OCR 文档；需要安装 Unstructured 可选依赖。",
            location="local",
            supported_extensions=(".pdf", ".docx", ".txt", ".md", ".markdown"),
            supports_tables=True,
            version="optional-partition-v1",
        )

    async def health(self) -> tuple[bool, str]:
        if not _installed():
            return False, "未安装 Unstructured 可选依赖"
        return True, "Unstructured 本地运行时可用"

    async def parse(
        self,
        request: ParseRequest,
        on_progress: ProgressCallback | None = None,
    ) -> ParseResult:
        if not _installed():
            raise ParserError("未安装 Unstructured 可选依赖")
        if on_progress:
            await on_progress("parsing", 35, "Unstructured 正在解析文档")

        def _partition() -> list[object]:
            from unstructured.partition.auto import partition

            suffix = Path(request.filename).suffix or ".bin"
            with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
                handle.write(request.content)
                handle.flush()
                return list(
                    partition(
                        filename=handle.name,
                        strategy=str(request.options.get("strategy") or "auto"),
                        infer_table_structure=bool(request.options.get("infer_table_structure", True)),
                    )
                )

        try:
            elements = await asyncio.to_thread(_partition)
        except Exception as exc:  # noqa: BLE001
            raise ParserError(f"Unstructured 解析失败：{exc}") from exc
        markdown = "\n\n".join(str(item).strip() for item in elements if str(item).strip()).strip()
        if not markdown:
            raise ParserError("Unstructured returned empty markdown")
        blocks = tuple({"type": type(item).__name__, "text": str(item)} for item in elements if str(item).strip())
        return ParseResult(
            markdown=markdown,
            parser_id=self.parser_id,
            parser_version="optional-partition-v1",
            structured_blocks=blocks,
            parser_metadata={"element_count": len(blocks)},
        )
