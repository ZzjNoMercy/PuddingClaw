"""FetchURLTool — Fetch a URL and return cleaned Markdown content."""

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
    args_schema: type[BaseModel] = FetchURLInput
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
                # Return the complete payload. DeepAgents FilesystemMiddleware
                # owns context-size eviction and will persist genuinely large
                # tool results under /large_tool_results with an exact path.
                return resp.text

            # requests falls back to ISO-8859-1 for some HTML responses even
            # when the body is UTF-8/GBK. Prefer detected encoding in that case
            # so error pages and Chinese snippets do not become mojibake.
            if not resp.encoding or resp.encoding.lower() in {
                "iso-8859-1", "latin-1", "ascii"
            }:
                resp.encoding = resp.apparent_encoding or "utf-8"

            # Convert HTML to Markdown
            converter = html2text.HTML2Text()
            converter.ignore_links = False
            converter.ignore_images = True
            converter.body_width = 0
            markdown = converter.handle(resp.text)

            return markdown

        except requests.Timeout:
            return "❌ Request timed out (15s limit)"
        except requests.RequestException as e:
            return f"❌ Fetch error: {str(e)}"


def create_fetch_url_tool() -> FetchURLTool:
    return FetchURLTool()
