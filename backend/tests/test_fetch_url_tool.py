"""FetchURLTool should leave large-result eviction to DeepAgents."""

from __future__ import annotations

from tools.fetch_url_tool import FetchURLTool


class _Response:
    def __init__(self, text: str, content_type: str) -> None:
        self.text = text
        self.headers = {"content-type": content_type}
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    def raise_for_status(self) -> None:
        return None


def test_fetch_url_returns_complete_json_for_filesystem_middleware(monkeypatch) -> None:
    payload = '{"items":[' + ('{"title":"完整结果"},' * 1000) + "{}]}"
    monkeypatch.setattr(
        "tools.fetch_url_tool.requests.get",
        lambda *_args, **_kwargs: _Response(payload, "application/json"),
    )

    result = FetchURLTool()._run("https://example.com/api")

    assert result == payload
    assert "...[truncated]" not in result


def test_fetch_url_returns_complete_html_markdown(monkeypatch) -> None:
    body = "<html><body><p>" + ("完整正文" * 2000) + "</p></body></html>"
    monkeypatch.setattr(
        "tools.fetch_url_tool.requests.get",
        lambda *_args, **_kwargs: _Response(body, "text/html"),
    )

    result = FetchURLTool()._run("https://example.com/page")

    assert result.count("完整正文") == 2000
    assert "...[truncated]" not in result
