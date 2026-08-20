from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

import httpx
import pytest

from knowledge.parsers.contracts import ParseRequest, ParserError
from knowledge.parsers.llama_parse_cloud import LlamaParseCloudParser
from knowledge.parsers.mineru_cloud import MinerUCloudLightParser, MinerUCloudPreciseParser
from knowledge.service import _rewrite_markdown_asset_links


def _mineru_zip(*, unsafe: bool = False) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("result/full.md", "# 云端结果\n\n![图](images/chart.png)\n")
        archive.writestr("result/images/chart.png", b"png-data")
        archive.writestr("result/demo_content_list.json", '[{"type":"text","text":"云端结果"}]')
        if unsafe:
            archive.writestr("../escape.png", b"escape")
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_mineru_precise_uploads_polls_and_localizes_zip(tmp_path: Path):
    archive = _mineru_zip()
    checkpoints: list[dict] = []
    calls: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/api/v4/file-urls/batch":
            assert request.headers["authorization"] == "Bearer token"
            return httpx.Response(200, json={"code": 0, "data": {"batch_id": "batch-1", "file_urls": ["https://upload.test/one"]}})
        if request.method == "PUT" and request.url.host == "upload.test":
            assert await request.aread() == b"%PDF test"
            return httpx.Response(200)
        if request.method == "GET" and request.url.path == "/api/v4/extract-results/batch/batch-1":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {"file_name": "report.pdf", "data_id": "pc-971bb9ca", "state": "done", "full_zip_url": "https://cdn.test/result.zip"}
                        ]
                    },
                },
            )
        if request.method == "GET" and request.url.host == "cdn.test":
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        parser = MinerUCloudPreciseParser(base_url="https://mineru.test", api_key="token", client=client)
        result = await parser.parse(
            ParseRequest(
                filename="report.pdf",
                content=b"%PDF test",
                assets_dir=tmp_path / "assets",
                options={"poll_interval_seconds": 0},
                checkpoint=lambda patch: _append(checkpoints, patch),
            )
        )

    assert result.markdown.startswith("# 云端结果")
    assert len(result.assets) == 1
    assert Path(result.assets[0].path).read_bytes() == b"png-data"
    assert result.structured_blocks[0]["type"] == "text"
    assert any(item.get("batch_id") == "batch-1" and item.get("phase") == "uploaded" for item in checkpoints)
    assert calls.count(("POST", "/api/v4/file-urls/batch")) == 1


@pytest.mark.asyncio
async def test_mineru_light_resumes_existing_task_without_reupload_and_localizes_images(tmp_path: Path):
    calls: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/v1/agent/parse/task-1":
            return httpx.Response(200, json={"code": 0, "data": {"state": "done", "markdown_url": "https://cdn.test/full.md"}})
        if request.url.path == "/full.md":
            return httpx.Response(200, text="# Light\n\n![x](https://cdn.test/picture.png)")
        if request.url.path == "/picture.png":
            return httpx.Response(200, content=b"picture")
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        parser = MinerUCloudLightParser(base_url="https://mineru.test", client=client)
        result = await parser.parse(
            ParseRequest(
                filename="report.pdf",
                content=b"%PDF test",
                assets_dir=tmp_path / "assets",
                options={"poll_interval_seconds": 0},
                remote_state={"parser_id": "mineru_cloud_light", "task_id": "task-1", "phase": "uploaded"},
            )
        )

    assert result.markdown.startswith("# Light")
    assert Path(result.assets[0].path).read_bytes() == b"picture"
    assert all(method not in {"POST", "PUT"} for method, _ in calls)


@pytest.mark.asyncio
async def test_llamaparse_v2_stops_at_markdown_and_cleans_remote_file(tmp_path: Path):
    checkpoints: list[dict] = []
    calls: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.host == "llama.test":
            assert request.headers.get("authorization") == "Bearer llama-key"
        if request.method == "POST" and request.url.path == "/api/v1/beta/files":
            assert b'name="purpose"' in await request.aread()
            return httpx.Response(200, json={"id": "file-1", "name": "report.pdf"})
        if request.method == "POST" and request.url.path == "/api/v2/parse":
            return httpx.Response(200, json={"id": "parse-1", "status": "PENDING"})
        if request.method == "GET" and request.url.path == "/api/v2/parse/parse-1":
            assert request.url.params.get("expand") == "markdown"
            return httpx.Response(
                200,
                json={
                    "id": "parse-1",
                    "status": "COMPLETED",
                    "tier": "agentic",
                    "version": "2026-07-24",
                    "markdown": {"pages": [{"md": "# LlamaParse\n\n![figure](https://assets.test/figure.png)"}]},
                },
            )
        if request.method == "GET" and request.url.host == "assets.test":
            return httpx.Response(200, content=b"figure")
        if request.method == "DELETE" and request.url.path == "/api/v1/beta/files/file-1":
            return httpx.Response(204)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        parser = LlamaParseCloudParser(base_url="https://llama.test", api_key="llama-key", client=client)
        result = await parser.parse(
            ParseRequest(
                filename="report.pdf",
                content=b"%PDF test",
                assets_dir=tmp_path / "assets",
                options={"poll_interval_seconds": 0, "delete_remote_file": True},
                checkpoint=lambda patch: _append(checkpoints, patch),
            )
        )

    assert result.markdown.startswith("# LlamaParse")
    assert result.parser_version == "llamaparse-v2:agentic:2026-07-24"
    assert Path(result.assets[0].path).read_bytes() == b"figure"
    assert ("DELETE", "/api/v1/beta/files/file-1") in calls
    assert any(item.get("job_id") == "parse-1" and item.get("phase") == "submitted" for item in checkpoints)


@pytest.mark.asyncio
async def test_mineru_archive_rejects_traversal_member(tmp_path: Path):
    archive = _mineru_zip(unsafe=True)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v4/extract-results/batch/batch-unsafe":
            return httpx.Response(200, json={"code": 0, "data": {"extract_result": [{"state": "done", "full_zip_url": "https://cdn.test/result.zip"}]}})
        if request.url.host == "cdn.test":
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        parser = MinerUCloudPreciseParser(base_url="https://mineru.test", api_key="token", client=client)
        with pytest.raises(ParserError, match="Markdown|安全|结果"):
            await parser.parse(
                ParseRequest(
                    filename="report.pdf",
                    content=b"%PDF",
                    assets_dir=tmp_path / "assets",
                    options={"poll_interval_seconds": 0},
                    remote_state={"parser_id": "mineru_cloud_precise", "batch_id": "batch-unsafe"},
                )
            )
    assert not (tmp_path / "escape.png").exists()


async def _append(target: list[dict], patch: dict) -> None:
    target.append(dict(patch))


def test_localized_cloud_image_url_is_rewritten_for_offline_markdown(tmp_path: Path):
    source = tmp_path / "downloaded.png"
    source.write_bytes(b"image")
    markdown, assets = _rewrite_markdown_asset_links(
        "![remote](https://cdn.example/temporary.png)",
        assets=[
            {
                "path": str(source),
                "relative_path": "downloaded.png",
                "name": "downloaded.png",
                "aliases": ["https://cdn.example/temporary.png"],
                "original_relative_path": "https://cdn.example/temporary.png",
            }
        ],
        assets_virtual_prefix="/knowledge/assets/test",
        markdown_asset_prefix="../assets/test",
        assets_dir=tmp_path / "assets",
    )
    assert markdown == "![remote](../assets/test/images/downloaded.png)"
    assert Path(assets[0]["path"]).read_bytes() == b"image"
