"""Main-Agent Tool for saving a public URL to the deterministic read-later queue."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from db import get_sessionmaker
from knowledge.read_later import create_read_later_item as enqueue_read_later_item
from knowledge.read_later import read_later_to_dict


class ReadLaterSaveInput(BaseModel):
    url: str = Field(max_length=1800, description="The exact public HTTP(S) URL the user wants to save for later.")
    title: str = Field(default="", max_length=500, description="Optional user-provided title; do not invent one.")
    note: str = Field(default="", max_length=4000, description="Optional user-provided note; do not summarize the page.")
    tags: list[str] = Field(default_factory=list, max_length=30, description="Optional explicit user tags.")


class ReadLaterSaveUrlTool(BaseTool):
    name: str = "read_later_save_url"
    description: str = (
        "Save one user-authorized public URL to PuddingClaw 稍后读. The backend safely fetches and converts the page "
        "to Markdown in a background task; if extraction is impossible it keeps the link. This tool only saves the "
        "URL. It does not compile Wiki or import GBrain."
    )
    args_schema: type[BaseModel] = ReadLaterSaveInput
    risk_level: str = "moderate"
    base_dir: Path

    def _run(self, url: str, title: str = "", note: str = "", tags: list[str] | None = None) -> str:
        return asyncio.run(self._arun(url=url, title=title, note=note, tags=tags))

    async def _arun(self, url: str, title: str = "", note: str = "", tags: list[str] | None = None) -> str:
        try:
            async with get_sessionmaker()() as session:
                item, job, deduplicated = await enqueue_read_later_item(
                    session,
                    base_dir=self.base_dir,
                    url=url,
                    title=title,
                    note=note,
                    tags=tags or [],
                )
            return json.dumps(
                {
                    "ok": True,
                    "deduplicated": deduplicated,
                    "item": read_later_to_dict(item),
                    "capture_job_id": job.id if job else None,
                    "read_later_path": "/knowledge/read-later",
                },
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001 - tool errors are model-visible
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def create_read_later_tool(base_dir: Path) -> ReadLaterSaveUrlTool:
    return ReadLaterSaveUrlTool(base_dir=base_dir.resolve())
