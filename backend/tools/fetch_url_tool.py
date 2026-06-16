"""FetchURLTool — Fetch a URL and return cleaned Markdown content."""

from typing import Type

import html2text
import requests
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field


class FetchURLInput(BaseModel):
    url: str = Field(description="The URL to fetch content from")


class FetchURLTool(BaseTool):
    name: str = "fetch_url"
    description: str = (
        "Fetch the content of a web page and return it as cleaned Markdown text. "
        "Use this to retrieve information from the internet. "
        "Input should be a valid URL (starting with http:// or https://)."
    )
    args_schema: Type[BaseModel] = FetchURLInput
    risk_level: str = "safe"

    def _run(self, url: str) -> str:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; PuddingClaw/0.1)"
            }
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")

            # If JSON, return directly
            if "application/json" in content_type:
                text = resp.text
                if len(text) > 5000:
                    text = text[:5000] + "\n...[truncated]"
                return text

            # Convert HTML to Markdown
            converter = html2text.HTML2Text()
            converter.ignore_links = False
            converter.ignore_images = True
            converter.body_width = 0
            markdown = converter.handle(resp.text)

            if len(markdown) > 5000:
                markdown = markdown[:5000] + "\n...[truncated]"
            return markdown

        except requests.Timeout:
            return "❌ Request timed out (15s limit)"
        except requests.RequestException as e:
            return f"❌ Fetch error: {str(e)}"


def create_fetch_url_tool() -> FetchURLTool:
    return FetchURLTool()
