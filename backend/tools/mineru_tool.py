"""MinerU PDF parsing tool.

Parses local PDF files into markdown by calling the MinerU service
running inside the Docker Compose stack.
"""

import os
from pathlib import Path
from typing import Type

import requests
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field


class MinerUParseInput(BaseModel):
    file_path: str = Field(
        description="Relative path of the PDF file to parse (relative to project root)"
    )


class MinerUParseTool(BaseTool):
    name: str = "parse_pdf"
    description: str = (
        "Parse a PDF file into markdown text. "
        "Use this when the user asks about the contents of a PDF document. "
        "Path is relative to the project root. "
        "Example: parse_pdf('workspace/report.pdf')"
    )
    args_schema: Type[BaseModel] = MinerUParseInput
    risk_level: str = "safe"
    root_dir: str = ""
    base_url: str = ""

    def _run(self, file_path: str) -> str:
        root = Path(self.root_dir or ".").resolve()
        normalized = file_path.replace("\\", "/").lstrip("./")
        full_path = (root / normalized).resolve()

        if not str(full_path).startswith(str(root)):
            return "❌ Access denied: path escapes project root"

        if not full_path.exists():
            return f"❌ File not found: {file_path}"

        if full_path.suffix.lower() != ".pdf":
            return f"❌ Not a PDF file: {file_path}"

        url = self.base_url or os.environ.get("MINERU_URL", "http://mineru:8002")
        try:
            with open(full_path, "rb") as f:
                response = requests.post(
                    f"{url}/parse",
                    files={"file": (full_path.name, f, "application/pdf")},
                    timeout=300,
                )
            response.raise_for_status()
            data = response.json()
            markdown = data.get("markdown", "")
            if not markdown:
                return f"⚠️ No markdown extracted. Response: {data}"
            return markdown
        except requests.exceptions.ConnectionError:
            return (
                "❌ Cannot connect to MinerU service. "
                "Make sure the mineru container is running (docker compose up -d mineru)."
            )
        except requests.exceptions.Timeout:
            return "❌ MinerU parsing timed out"
        except Exception as e:
            return f"❌ MinerU request failed: {str(e)}"

    async def _arun(self, file_path: str) -> str:
        import asyncio

        return await asyncio.to_thread(self._run, file_path)


def create_mineru_parse_tool(base_dir: Path) -> BaseTool:
    """Factory used by backend/tools/__init__.py auto-discovery."""
    return MinerUParseTool(root_dir=str(base_dir))
