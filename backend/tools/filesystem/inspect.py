"""Shared inspection primitives for filesystem tools."""

import hashlib
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

from tools.filesystem.schemas import InspectFileVersionInput


def read_all(backend: Any, file_path: str) -> tuple[str | None, str | None]:
    chunks: list[str] = []
    offset = 0
    while True:
        result = backend.read(file_path, offset=offset, limit=2000)
        if result.error:
            if chunks and "exceeds file length" in result.error:
                break
            return None, result.error
        data = result.file_data or {}
        if data.get("encoding") != "utf-8":
            return None, f"Versioned patch only supports UTF-8 text files: {file_path}"
        chunk = str(data.get("content") or "")
        chunks.append(chunk)
        line_count = len(chunk.splitlines())
        if line_count < 2000:
            break
        offset += line_count
    return "".join(chunks), None


def digest(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def build_inspect_tools(backend: Any) -> list[StructuredTool]:
    """Build metadata/content inspection tools bound to one workspace backend."""

    def inspect_file_version(
        file_path: str,
        runtime: ToolRuntime[Any, Any],
        include_content: bool = True,
    ) -> ToolMessage:
        content, error = read_all(backend, file_path)
        if error is not None or content is None:
            return ToolMessage(
                content=f"Error: {error or 'unable to read file'}",
                name="inspect_file_version",
                tool_call_id=runtime.tool_call_id,
                status="error",
            )
        response = (
            f"file_path: {file_path}\nversion: {digest(content)}\n"
            f"size_chars: {len(content)}"
        )
        if include_content:
            response = f"{response}\ncontent:\n{content}"
        return ToolMessage(
            content=response,
            name="inspect_file_version",
            tool_call_id=runtime.tool_call_id,
            status="success",
        )

    return [
        StructuredTool.from_function(
            name="inspect_file_version",
            description=(
                "Return a UTF-8 file's sha256 version and size. By default the full "
                "content is also returned for compatibility. When the content needed "
                "to plan the edit is already present in context and only "
                "expected_sha256 must be obtained or refreshed, set "
                "include_content=false to avoid reinjecting the file into model "
                "context. Always call this before patch_file."
            ),
            func=inspect_file_version,
            args_schema=InspectFileVersionInput,
            infer_schema=False,
        )
    ]
