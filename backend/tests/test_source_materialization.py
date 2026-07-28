from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.backends import FilesystemBackend

from graph.session_manager import session_manager
from harness.source_materialization import (
    SourceMaterializationError,
    fill_typed_slot,
    persist_materialization_receipt,
    public_source_reference,
    register_file_source_reference,
    render_source,
    resolve_source_bytes,
)
from tools.filesystem.factory import VersionedPatchMiddleware


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _runtime(call_id: str, *, session_id: str, run_id: str):
    return SimpleNamespace(
        tool_call_id=call_id,
        context={
            "session_id": session_id,
            "run_id": run_id,
            "query_id": "query-source",
        },
    )


def _setup_source(tmp_path: Path, *, rows: int = 337) -> tuple[dict, list[dict]]:
    state = tmp_path / "state"
    state.mkdir()
    session_manager.initialize(state)
    session_manager.create_session("source-session")
    values = [
        {
            "year": 2021 + (index % 6),
            "brand": f"品牌{index % 9}",
            "config_rate": round((index % 100) / 100, 2),
        }
        for index in range(rows)
    ]
    payload = tmp_path / "configuration-result.jsonl"
    payload.write_text(
        "".join(
            json.dumps(item, ensure_ascii=False) + "\n"
            for item in values
        ),
        encoding="utf-8",
    )
    source = register_file_source_reference(
        session_id="source-session",
        kind="database_result",
        file_path=payload,
        media_type="application/x-ndjson",
        schema_ref="schema:configuration-rate:v1",
        row_count=len(values),
        producer_receipt_ids=["sql-validation-1", "result-store:qr-1"],
        source_ref="source-config-337",
        metadata={"columns": ["year", "brand", "config_rate"]},
    )
    return source, values


def test_source_reference_is_immutable_model_safe_and_hash_verified(tmp_path: Path) -> None:
    source, values = _setup_source(tmp_path)
    public = public_source_reference(source)

    assert "locator" not in public
    assert public["source_ref"] == "source-config-337"
    assert public["row_count"] == 337

    registered_again = register_file_source_reference(
        session_id="source-session",
        kind="database_result",
        file_path=tmp_path / "configuration-result.jsonl",
        media_type="application/x-ndjson",
        schema_ref="schema:configuration-rate:v1",
        row_count=337,
        producer_receipt_ids=["result-store:qr-1", "sql-validation-1"],
        source_ref="source-config-337",
        metadata={"columns": ["year", "brand", "config_rate"]},
    )
    assert registered_again == source

    resolved_source, content = resolve_source_bytes(
        "source-session",
        "source-config-337",
    )
    rendered, count = render_source(
        resolved_source,
        content,
        renderer="js_array",
        projection=["year", "config_rate"],
        expected_schema_ref="schema:configuration-rate:v1",
        expected_item_count=337,
    )
    assert count == 337
    assert json.loads(rendered) == [
        {"config_rate": item["config_rate"], "year": item["year"]}
        for item in values
    ]

    (tmp_path / "configuration-result.jsonl").write_text(
        '{"tampered":true}\n',
        encoding="utf-8",
    )
    with pytest.raises(SourceMaterializationError) as exc:
        resolve_source_bytes("source-session", "source-config-337")
    assert exc.value.code == "source_hash_mismatch"


def test_typed_slot_is_exact_and_renderer_bound() -> None:
    template = "const configRows = /*{{SLOT:config_rows|js_array}}*/ [];\n"
    filled = fill_typed_slot(
        template,
        slot_id="config_rows",
        renderer="js_array",
        rendered=b'[{"year":2026}]\n',
    )
    assert filled == 'const configRows = [{"year":2026}];\n'

    with pytest.raises(SourceMaterializationError, match="exactly once"):
        fill_typed_slot(
            template + template,
            slot_id="config_rows",
            renderer="js_array",
            rendered=b"[]",
        )
    with pytest.raises(SourceMaterializationError, match="requires js_array"):
        fill_typed_slot(
            template,
            slot_id="config_rows",
            renderer="json",
            rendered=b"[]",
        )


def test_materialization_receipt_replay_is_idempotent_and_mutation_bound(
    tmp_path: Path,
) -> None:
    source, _values = _setup_source(tmp_path)
    common = {
        "session_id": "source-session",
        "run_id": "run-source",
        "query_id": "query-source",
        "source": source,
        "renderer": "js_array",
        "target_path": "/external/product-config-v2.js",
        "target_sha256": "sha256:" + "1" * 64,
        "item_count": 337,
        "template_sha256": "sha256:" + "2" * 64,
        "slot_id": "config_rows",
        "validation_receipt_ids": ["validation-js"],
    }

    first = persist_materialization_receipt(
        **common,
        mutation_receipt_id="mutation-1",
    )
    replayed = persist_materialization_receipt(
        **common,
        mutation_receipt_id="mutation-1",
    )
    second_mutation = persist_materialization_receipt(
        **common,
        mutation_receipt_id="mutation-2",
    )

    assert replayed == first
    assert (
        second_mutation["materialization_receipt_id"]
        != first["materialization_receipt_id"]
    )
    assert len(
        session_manager.list_materialization_receipts("source-session")
    ) == 2


def test_js_array_renderer_escapes_inline_script_breakout(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    session_manager.initialize(state)
    session_manager.create_session("malicious-source-session")
    payload = tmp_path / "malicious.json"
    payload.write_text(
        json.dumps(
            [
                {
                    "label": "</script><script>alert(1)</script>",
                    "separator": "\u2028",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    source = register_file_source_reference(
        session_id="malicious-source-session",
        kind="api_response",
        file_path=payload,
        media_type="application/json",
        schema_ref="schema:malicious:v1",
        row_count=1,
    )
    resolved, content = resolve_source_bytes(
        "malicious-source-session",
        source["source_ref"],
    )

    rendered, count = render_source(
        resolved,
        content,
        renderer="js_array",
        expected_item_count=1,
    )

    text = rendered.decode("utf-8")
    assert count == 1
    assert "</script>" not in text
    assert "\\u003c/script\\u003e" in text
    assert "\\u2028" in text
    assert json.loads(text)[0]["label"] == "</script><script>alert(1)</script>"


def test_large_attachment_and_api_sources_remain_reference_only(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state.mkdir()
    workspace.mkdir()
    session_manager.initialize(state)
    session_manager.create_session("large-source-session")
    middleware = VersionedPatchMiddleware(
        FilesystemBackend(root_dir=workspace, virtual_mode=True)
    )
    tool = next(
        item for item in middleware.tools
        if item.name == "materialize_source_ref"
    )

    for kind in ("attachment_table", "api_response"):
        source_file = tmp_path / f"{kind}.jsonl"
        with source_file.open("w", encoding="utf-8") as handle:
            for index in range(100_000):
                handle.write(
                    json.dumps(
                        {"row": index, "value": f"{kind}-{index}"},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        source = register_file_source_reference(
            session_id="large-source-session",
            kind=kind,
            file_path=source_file,
            media_type="application/x-ndjson",
            schema_ref=f"schema:{kind}:v1",
            row_count=100_000,
            producer_receipt_ids=[f"producer:{kind}"],
        )
        result = tool.func(
            source_ref=source["source_ref"],
            destination={
                "kind": "file",
                "target_path": f"{kind}.jsonl",
                "mode": "create",
            },
            renderer="identity",
            expected_schema_ref=f"schema:{kind}:v1",
            expected_item_count=100_000,
            runtime=_runtime(
                f"materialize-{kind}",
                session_id="large-source-session",
                run_id="run-large-source",
            ),
        )

        assert result.status == "success"
        response = json.loads(result.content)
        assert response["item_count"] == 100_000
        assert f"{kind}-99999" not in result.content
        assert (workspace / f"{kind}.jsonl").read_bytes() == source_file.read_bytes()


def test_materialize_337_configuration_rows_to_file_and_slot_without_payload_in_result(
    tmp_path: Path,
) -> None:
    source, values = _setup_source(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    template = "const configRows = /*{{SLOT:config_rows|js_array}}*/ [];\n"
    (workspace / "template.js").write_text(template, encoding="utf-8")
    middleware = VersionedPatchMiddleware(
        FilesystemBackend(root_dir=workspace, virtual_mode=True)
    )
    tool = next(
        item for item in middleware.tools
        if item.name == "materialize_source_ref"
    )
    runtime = _runtime(
        "materialize-file",
        session_id="source-session",
        run_id="run-source",
    )

    file_result = tool.func(
        source_ref=source["source_ref"],
        destination={
            "kind": "file",
            "target_path": "config.json",
            "mode": "create",
        },
        renderer="json",
        projection=["year", "brand", "config_rate"],
        expected_schema_ref="schema:configuration-rate:v1",
        expected_item_count=337,
        runtime=runtime,
    )
    assert file_result.status == "success", file_result.content
    file_payload = json.loads(file_result.content)
    assert file_payload["item_count"] == 337
    assert "品牌0" not in file_result.content
    assert json.loads((workspace / "config.json").read_text(encoding="utf-8")) == values

    slot_result = tool.func(
        source_ref=source["source_ref"],
        destination={
            "kind": "slot",
            "template_path": "template.js",
            "template_sha256": _sha256(template),
            "slot_id": "config_rows",
            "output_path": "product-config-v2.js",
            "output_mode": "create",
        },
        renderer="js_array",
        projection=["year", "brand", "config_rate"],
        expected_item_count=337,
        runtime=_runtime(
            "materialize-slot",
            session_id="source-session",
            run_id="run-source",
        ),
    )
    assert slot_result.status == "success"
    assert "品牌0" not in slot_result.content
    output = (workspace / "product-config-v2.js").read_text(encoding="utf-8")
    assert output.startswith("const configRows = [")
    assert output.endswith("];\n")
    receipts = session_manager.list_materialization_receipts(
        "source-session",
        run_id="run-source",
    )
    assert len(receipts) == 2
    assert {item["item_count"] for item in receipts} == {337}
    assert all(
        item["source_sha256"] == source["content_sha256"]
        for item in receipts
    )

    replacement_values = values[:2]
    replacement_source_path = tmp_path / "configuration-replacement.json"
    replacement_source_path.write_text(
        json.dumps(replacement_values, ensure_ascii=False),
        encoding="utf-8",
    )
    replacement_source = register_file_source_reference(
        session_id="source-session",
        kind="api_response",
        file_path=replacement_source_path,
        media_type="application/json",
        schema_ref="schema:configuration-rate:v1",
        row_count=2,
        producer_receipt_ids=["api-response:replacement"],
    )
    previous_output = (workspace / "product-config-v2.js").read_text(
        encoding="utf-8"
    )
    replaced = tool.func(
        source_ref=replacement_source["source_ref"],
        destination={
            "kind": "file",
            "target_path": "product-config-v2.js",
            "mode": "replace",
            "expected_sha256": _sha256(previous_output),
        },
        renderer="js_array",
        expected_item_count=2,
        runtime=_runtime(
            "materialize-replace",
            session_id="source-session",
            run_id="run-source",
        ),
    )
    assert replaced.status == "success", replaced.content
    assert json.loads(
        (workspace / "product-config-v2.js").read_text(encoding="utf-8")
    ) == replacement_values
