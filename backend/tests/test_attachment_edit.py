import io
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import threading

from deepagents.backends import CompositeBackend, FilesystemBackend
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from langchain_core.messages import ToolMessage
from PIL import Image

from api.attachments import (
    MAX_ATTACHMENT_REQUEST_BYTES,
    download_attachment,
    preview_attachment,
    router as attachments_router,
)
from graph.attachment_store import attachment_store
from graph.middlewares.attachment_edit import (
    MAX_EDITABLE_ATTACHMENT_BYTES,
    AttachmentEditMiddleware,
)
from graph.session_manager import session_manager
from harness.verification_activations import (
    build_verification_activations,
    verification_packs_for_tool,
)


def _runtime(call_id: str, **context):
    return SimpleNamespace(tool_call_id=call_id, context=context)


def _setup(tmp_path):
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    state.mkdir()
    workspace.mkdir()
    scratch.mkdir()
    session_manager.initialize(state)
    session_manager.create_session("attachment-session")
    attachment_store.initialize(state)
    source = attachment_store.save_bytes(
        session_id="attachment-session",
        filename="原始 report.bin",
        mime_type="application/octet-stream",
        data=b"\x00source\xff",
        source="upload",
    )
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = CompositeBackend(
        default=workspace_backend,
        routes={
            "/workspace/": workspace_backend,
            "/scratch/": FilesystemBackend(root_dir=scratch, virtual_mode=True),
        },
    )
    middleware = AttachmentEditMiddleware(backend)
    prepare = next(tool for tool in middleware.tools if tool.name == "prepare_attachment_edit")
    publish = next(tool for tool in middleware.tools if tool.name == "publish_attachment")
    context = {
        "session_id": "attachment-session",
        "run_id": "run-1",
        "query_id": "query-1",
        "goal_id": "goal-1",
        "goal_revision": 3,
    }
    return source, scratch, backend, prepare, publish, context


def test_attachment_edit_stages_exact_bytes_and_publishes_new_immutable_attachment(tmp_path):
    source, scratch, backend, prepare, publish, context = _setup(tmp_path)
    source_internal = attachment_store.get("attachment-session", source["id"])
    assert source_internal is not None
    source_path = Path(source_internal["path"])

    staged = prepare.func(
        attachment_id=source["id"],
        runtime=_runtime("prepare-1", **context),
    )
    assert staged.status == "success"
    lease = staged.artifact["attachment_edit_lease"]
    staged_host = scratch / lease["staged_path"].removeprefix("/scratch/")
    assert staged_host.read_bytes() == b"\x00source\xff"

    edited = b"\x00derived\xfe"
    response = backend.upload_files([(lease["staged_path"], edited)])
    assert response[0].error is None
    published = publish.func(
        lease_id=lease["lease_id"],
        output_path=lease["staged_path"],
        output_name="修改版.bin",
        mime_type="application/octet-stream",
        runtime=_runtime("publish-1", **context),
    )
    assert published.status == "success"
    result = published.artifact["published_attachment"]
    assert result["id"] != source["id"]
    assert result["source"] == "generated"
    assert result["derived_from"] == source["id"]
    assert result["created_by_run_id"] == "run-1"
    assert result["download_url"].startswith("/api/attachments/")
    assert source_path.read_bytes() == b"\x00source\xff"
    generated = attachment_store.get("attachment-session", result["id"])
    assert generated is not None
    assert Path(generated["path"]).read_bytes() == edited
    assert published.artifact["artifact_reference"]["scope"] == "attachment"
    assert published.artifact["artifact_reference"]["role"] == "target"

    replay = publish.func(
        lease_id=lease["lease_id"],
        output_path=lease["staged_path"],
        output_name="修改版.bin",
        mime_type="application/octet-stream",
        runtime=_runtime("publish-replay", **context),
    )
    assert replay.status == "success"
    assert replay.artifact["published_attachment"]["id"] == result["id"]


def test_attachment_edit_is_session_run_revision_and_path_scoped(tmp_path):
    source, _, _, prepare, publish, context = _setup(tmp_path)
    session_manager.create_session("other-session")

    foreign = prepare.func(
        attachment_id=source["id"],
        runtime=_runtime(
            "prepare-foreign",
            session_id="other-session",
            run_id="run-1",
            query_id="query-1",
            goal_id="goal-1",
            goal_revision=3,
        ),
    )
    assert foreign.status == "error"
    assert "current Session" in foreign.content

    staged = prepare.func(
        attachment_id=source["id"],
        runtime=_runtime("prepare-owned", **context),
    )
    lease = staged.artifact["attachment_edit_lease"]
    traversal = publish.func(
        lease_id=lease["lease_id"],
        output_path=f"{lease['staged_dir']}/../other.txt",
        output_name="other.txt",
        mime_type="text/plain",
        runtime=_runtime("publish-traversal", **context),
    )
    assert traversal.status == "error"
    assert "normalized file inside" in traversal.content

    other_run = dict(context, run_id="run-2")
    denied = publish.func(
        lease_id=lease["lease_id"],
        output_path=lease["staged_path"],
        output_name="result.bin",
        mime_type="application/octet-stream",
        runtime=_runtime("publish-other-run", **other_run),
    )
    assert denied.status == "error"
    assert "different Run" in denied.content

    other_revision = dict(context, goal_revision=4)
    denied_revision = publish.func(
        lease_id=lease["lease_id"],
        output_path=lease["staged_path"],
        output_name="result.bin",
        mime_type="application/octet-stream",
        runtime=_runtime("publish-other-revision", **other_revision),
    )
    assert denied_revision.status == "error"


def test_attachment_publish_fails_closed_when_immutable_source_changes(tmp_path):
    source, _, _, prepare, publish, context = _setup(tmp_path)
    staged = prepare.func(
        attachment_id=source["id"],
        runtime=_runtime("prepare-source-conflict", **context),
    )
    lease = staged.artifact["attachment_edit_lease"]
    source_internal = attachment_store.get("attachment-session", source["id"])
    assert source_internal is not None
    Path(source_internal["path"]).write_bytes(b"tampered")

    denied = publish.func(
        lease_id=lease["lease_id"],
        output_path=lease["staged_path"],
        output_name="result.bin",
        mime_type="application/octet-stream",
        runtime=_runtime("publish-source-conflict", **context),
    )
    assert denied.status == "error"
    assert "source attachment changed" in denied.content


def test_published_attachment_receipt_is_material_artifact_evidence(tmp_path):
    source, _, backend, prepare, publish, context = _setup(tmp_path)
    staged = prepare.func(
        attachment_id=source["id"],
        runtime=_runtime("prepare-evidence", **context),
    )
    lease = staged.artifact["attachment_edit_lease"]
    backend.upload_files([(lease["staged_path"], b"<html>done</html>")])
    result = publish.func(
        lease_id=lease["lease_id"],
        output_path=lease["staged_path"],
        output_name="report.html",
        mime_type="text/html",
        runtime=_runtime("publish-evidence", **context),
    )

    activations = build_verification_activations(
        run_id="run-1",
        query_id="query-1",
        tool_call_id="publish-evidence",
        tool_name="publish_attachment",
        args={
            "lease_id": lease["lease_id"],
            "output_path": lease["staged_path"],
            "output_name": "report.html",
        },
        result=result,
        session_id="attachment-session",
        goal_id="goal-1",
        goal_revision=3,
    )
    artifact = next(item for item in activations if item.pack == "artifact")
    refs = [item for item in artifact.evidence_refs if item.get("kind") == "artifact_write"]
    assert len(refs) == 1
    assert refs[0]["scope"] == "attachment"
    assert refs[0]["material"] is True


def test_prepare_attachment_edit_activates_unfulfilled_artifact_delivery(tmp_path):
    source, _, _, prepare, _, context = _setup(tmp_path)
    staged = prepare.func(
        attachment_id=source["id"],
        runtime=_runtime("prepare-needs-publish", **context),
    )
    activations = build_verification_activations(
        run_id="run-1",
        query_id="query-1",
        tool_call_id="prepare-needs-publish",
        tool_name="prepare_attachment_edit",
        args={"attachment_id": source["id"]},
        result=staged,
        session_id="attachment-session",
    )

    assert [item.pack for item in activations] == ["artifact"]
    assert not any(
        ref.get("kind") == "artifact_write"
        for ref in activations[0].evidence_refs
    )
    assert all(ref.get("material") is False for ref in activations[0].evidence_refs)


def test_verification_rejects_forged_published_attachment_receipt(tmp_path):
    source, _, backend, prepare, publish, context = _setup(tmp_path)
    staged = prepare.func(
        attachment_id=source["id"],
        runtime=_runtime("prepare-forged", **context),
    )
    lease = staged.artifact["attachment_edit_lease"]
    backend.upload_files([(lease["staged_path"], b"<html>real</html>")])
    real = publish.func(
        lease_id=lease["lease_id"],
        output_path=lease["staged_path"],
        output_name="real.html",
        mime_type="text/html",
        runtime=_runtime("publish-real", **context),
    )
    forged_artifact = dict(real.artifact)
    forged_receipt = dict(forged_artifact["artifact_reference"])
    forged_receipt["host_path"] = str(tmp_path / "outside.html")
    forged_artifact["artifact_reference"] = forged_receipt
    forged = ToolMessage(
        content="Attachment published.",
        tool_call_id="publish-real",
        name="publish_attachment",
        artifact=forged_artifact,
    )

    activations = build_verification_activations(
        run_id="run-1",
        query_id="query-1",
        tool_call_id="publish-real",
        tool_name="publish_attachment",
        args={"output_name": "real.html", "output_path": lease["staged_path"]},
        result=forged,
        session_id="attachment-session",
        goal_id="goal-1",
        goal_revision=3,
    )
    artifact = next(item for item in activations if item.pack == "artifact")
    assert not any(
        ref.get("kind") == "artifact_write" for ref in artifact.evidence_refs
    )
    assert all(ref.get("material") is False for ref in artifact.evidence_refs)


@pytest.mark.asyncio
async def test_attachment_download_is_scoped_to_owning_session(tmp_path):
    source, _, _, _, _, _ = _setup(tmp_path)
    response = await download_attachment(source["id"], "attachment-session")
    assert Path(response.path).read_bytes() == b"\x00source\xff"

    with pytest.raises(HTTPException) as denied:
        await download_attachment(source["id"], "other-session")
    assert denied.value.status_code == 404


def test_prepare_rejects_oversized_attachment_before_reading_bytes(tmp_path, monkeypatch):
    source, _, _, prepare, _, context = _setup(tmp_path)
    internal = attachment_store.get("attachment-session", source["id"])
    assert internal is not None
    source_path = Path(internal["path"])
    with source_path.open("r+b") as stream:
        stream.truncate(MAX_EDITABLE_ATTACHMENT_BYTES + 1)
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path):
        if path == source_path:
            raise AssertionError("oversized attachment must be rejected by stat before read_bytes")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    denied = prepare.func(
        attachment_id=source["id"],
        runtime=_runtime("prepare-oversized", **context),
    )
    assert denied.status == "error"
    assert "exceeds" in denied.content


def test_publish_rejects_oversized_output_before_backend_download(tmp_path, monkeypatch):
    source, scratch, backend, prepare, publish, context = _setup(tmp_path)
    staged = prepare.func(
        attachment_id=source["id"],
        runtime=_runtime("prepare-large-output", **context),
    )
    lease = staged.artifact["attachment_edit_lease"]
    staged_host = scratch / lease["staged_path"].removeprefix("/scratch/")
    with staged_host.open("r+b") as stream:
        stream.truncate(MAX_EDITABLE_ATTACHMENT_BYTES + 1)

    def forbidden_download(_paths):
        raise AssertionError("oversized output must not reach download_files")

    monkeypatch.setattr(backend, "download_files", forbidden_download)
    denied = publish.func(
        lease_id=lease["lease_id"],
        output_path=lease["staged_path"],
        output_name="large.bin",
        mime_type="application/octet-stream",
        runtime=_runtime("publish-large-output", **context),
    )
    assert denied.status == "error"
    assert "publish limit" in denied.content


@pytest.mark.parametrize("name", ["result.bin", "archive.zip", "image.png", "photo.jpg"])
def test_publish_any_file_type_activates_only_artifact_delivery(name):
    assert verification_packs_for_tool(
        "publish_attachment",
        {"output_name": name, "output_path": f"/scratch/attachments/lease/{name}"},
    ) == ["artifact"]


def test_publish_html_cannot_impersonate_code_validation():
    assert verification_packs_for_tool(
        "publish_attachment",
        {"output_name": "report.html", "output_path": "/scratch/attachments/lease/report.html"},
    ) == ["artifact"]


def test_concurrent_publish_allows_only_one_lease_branch(tmp_path, monkeypatch):
    _, _, backend, prepare, publish, context = _setup(tmp_path)
    staged = prepare.func(
        attachment_id=attachment_store.save_bytes(
            session_id="attachment-session",
            filename="branch-source.bin",
            mime_type="application/octet-stream",
            data=b"source",
            source="upload",
        )["id"],
        runtime=_runtime("prepare-concurrent", **context),
    )
    lease = staged.artifact["attachment_edit_lease"]
    path_a = f"{lease['staged_dir']}/a.bin"
    path_b = f"{lease['staged_dir']}/b.bin"
    backend.upload_files([(path_a, b"a"), (path_b, b"b")])

    original_claim = session_manager.claim_attachment_publish
    barrier = threading.Barrier(2)

    def synchronized_claim(*args, **kwargs):
        barrier.wait(timeout=5)
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(session_manager, "claim_attachment_publish", synchronized_claim)

    def invoke(call_id, path, name):
        return publish.func(
            lease_id=lease["lease_id"],
            output_path=path,
            output_name=name,
            mime_type="application/octet-stream",
            runtime=_runtime(call_id, **context),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda item: invoke(*item),
                [("publish-a", path_a, "a.bin"), ("publish-b", path_b, "b.bin")],
            )
        )
    assert sorted(result.status for result in results) == ["error", "success"]
    committed = session_manager.get_attachment_edit_lease(
        "attachment-session", lease["lease_id"]
    )
    assert committed is not None and committed["status"] == "published"
    assert committed["published_name"] in {"a.bin", "b.bin"}
    deliveries = session_manager.list_attachment_deliveries("attachment-session", "query-1")
    assert len(deliveries) == 1


@pytest.mark.parametrize("manifest_content", [None, "{broken-json"])
def test_deterministic_attachment_replay_recovers_half_commit(tmp_path, manifest_content):
    _setup(tmp_path)
    attachment_id = "att_deterministic1"
    orphan = attachment_store.root_dir / "attachment-session" / attachment_id
    orphan.mkdir(parents=True)
    (orphan / "result.bin").write_bytes(b"partial")
    if manifest_content is not None:
        (orphan / "manifest.json").write_text(manifest_content, encoding="utf-8")

    saved = attachment_store.save_bytes(
        session_id="attachment-session",
        filename="result.bin",
        mime_type="application/octet-stream",
        data=b"complete",
        source="generated",
        attachment_id=attachment_id,
    )
    assert saved["id"] == attachment_id
    resolved = attachment_store.get("attachment-session", attachment_id)
    assert resolved is not None
    assert Path(resolved["path"]).read_bytes() == b"complete"


def test_publish_outbox_survives_before_stream_manager_consumes_tool_message(tmp_path):
    source, _, backend, prepare, publish, context = _setup(tmp_path)
    staged = prepare.func(
        attachment_id=source["id"],
        runtime=_runtime("prepare-outbox", **context),
    )
    lease = staged.artifact["attachment_edit_lease"]
    backend.upload_files([(lease["staged_path"], b"durable-output")])
    result = publish.func(
        lease_id=lease["lease_id"],
        output_path=lease["staged_path"],
        output_name="durable.bin",
        mime_type="application/octet-stream",
        runtime=_runtime("publish-outbox", **context),
    )
    assert result.status == "success"

    raw = session_manager.get_raw_messages("attachment-session")
    delivery_message = next(
        item
        for item in raw["messages"]
        if item.get("role") == "assistant" and item.get("query_id") == "query-1"
    )
    assert delivery_message["output_attachments"][0]["name"] == "durable.bin"


@pytest.mark.asyncio
async def test_deleted_session_removes_attachment_and_invalidates_download(tmp_path):
    source, _, _, _, _, _ = _setup(tmp_path)
    attachment_dir = attachment_store.root_dir / "attachment-session"
    assert attachment_dir.is_dir()
    session_manager.delete_session("attachment-session")
    assert not attachment_dir.exists()
    with pytest.raises(HTTPException) as denied:
        await download_attachment(source["id"], "attachment-session")
    assert denied.value.status_code == 404


def _png_bytes(size=(24, 18)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", size, color=(0, 47, 167)).save(stream, format="PNG")
    return stream.getvalue()


def test_image_preview_is_session_scoped_verified_and_inline(tmp_path):
    state = tmp_path / "preview-state"
    state.mkdir()
    session_manager.initialize(state)
    attachment_store.initialize(state)
    session_manager.create_session("preview-session")
    session_manager.create_session("other-session")

    image = attachment_store.save_bytes(
        session_id="preview-session",
        filename="二维码.png",
        mime_type="application/octet-stream",
        data=_png_bytes(),
        source="generated",
    )
    assert image["preview_url"].endswith(
        f"/{image['id']}/preview?session_id=preview-session"
    )
    assert image["preview_mime_type"] == "image/png"
    assert (image["width"], image["height"]) == (24, 18)

    app = FastAPI()
    app.include_router(attachments_router, prefix="/api")
    client = TestClient(app)
    response = client.get(image["preview_url"])
    assert response.status_code == 200
    assert response.content == _png_bytes()
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"

    foreign = client.get(
        f"/api/attachments/{image['id']}/preview?session_id=other-session"
    )
    assert foreign.status_code == 404


def test_image_preview_rejects_spoofed_or_active_image_content(tmp_path):
    state = tmp_path / "spoof-state"
    state.mkdir()
    session_manager.initialize(state)
    attachment_store.initialize(state)
    session_manager.create_session("preview-session")
    app = FastAPI()
    app.include_router(attachments_router, prefix="/api")
    client = TestClient(app)

    spoofed = attachment_store.save_bytes(
        session_id="preview-session",
        filename="not-really.png",
        mime_type="image/png",
        data=b"<html><script>alert(1)</script></html>",
    )
    assert "preview_url" not in spoofed
    denied = client.get(
        f"/api/attachments/{spoofed['id']}/preview?session_id=preview-session"
    )
    assert denied.status_code == 415

    svg = attachment_store.save_bytes(
        session_id="preview-session",
        filename="active.svg",
        mime_type="image/svg+xml",
        data=b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    )
    assert "preview_url" not in svg
    denied_svg = client.get(
        f"/api/attachments/{svg['id']}/preview?session_id=preview-session"
    )
    assert denied_svg.status_code == 415

    animated_stream = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(
        animated_stream,
        format="PNG",
        save_all=True,
        append_images=[Image.new("RGB", (4, 4), "blue")],
        duration=100,
        loop=0,
    )
    animated = attachment_store.save_bytes(
        session_id="preview-session",
        filename="animated.png",
        mime_type="image/png",
        data=animated_stream.getvalue(),
    )
    assert "preview_url" not in animated
    denied_animated = client.get(
        f"/api/attachments/{animated['id']}/preview?session_id=preview-session"
    )
    assert denied_animated.status_code == 415


def test_upload_requires_existing_session_and_rejects_excess_parts(tmp_path):
    state = tmp_path / "upload-state"
    state.mkdir()
    session_manager.initialize(state)
    attachment_store.initialize(state)
    app = FastAPI()
    app.include_router(attachments_router)
    client = TestClient(app)

    missing = client.post(
        "/attachments",
        data={"session_id": "does-not-exist"},
        files={"files": ("a.txt", b"a", "text/plain")},
    )
    assert missing.status_code == 404

    session_manager.create_session("upload-session")
    too_many = client.post(
        "/attachments",
        data={"session_id": "upload-session"},
        files=[("files", (f"{index}.txt", b"x", "text/plain")) for index in range(9)],
    )
    assert too_many.status_code == 413

    oversized_header = client.post(
        "/attachments",
        data={"session_id": "upload-session"},
        files={"files": ("small.txt", b"small", "text/plain")},
        headers={"content-length": str(MAX_ATTACHMENT_REQUEST_BYTES + 1)},
    )
    assert oversized_header.status_code == 413
