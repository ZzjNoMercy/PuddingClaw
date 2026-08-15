from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import ExecuteResponse

from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
from graph.session_manager import session_manager
from harness.coordinators import HarnessRunCoordinator
from harness.models import (
    RunStatus,
    ValidationArtifactRef,
    ValidationReceipt,
    VerificationActivation,
)
from harness.source_materialization import register_file_source_reference
from tools.filesystem.factory import VersionedPatchMiddleware
from tools.filesystem.schemas import ReplacementHunk


class _RegisteredValidatorBackend:
    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        del timeout
        assert command.startswith(
            ("node --check ", "python3 -m py_compile ", "python3 -m json.tool ")
        )
        return ExecuteResponse(output="registered validator passed", exit_code=0)


def _runtime(call_id: str, run) -> SimpleNamespace:
    return SimpleNamespace(
        tool_call_id=call_id,
        context={
            "session_id": run.session_id,
            "run_id": run.run_id,
            "query_id": run.query_id,
        },
    )


def test_v2_copy_patch_validate_pipeline_reuses_vendor_and_never_reads_full_body(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    report_dir = tmp_path / "reports"
    scratch = tmp_path / "scratch"
    for directory in (state, workspace, report_dir, scratch):
        directory.mkdir()
    html_v1 = report_dir / "product-config.html"
    js_v1 = report_dir / "product-config-charts.js"
    vendor = report_dir / "echarts.min.js"
    html_v1.write_text(
        """<!doctype html>
<html><body data-e2e-required-years="2021,2022,2023,2024,2025,2026" data-e2e-cutoff="2026-07-23">
<select id="year"><option>2020</option><option>2021</option><option>2022</option><option>2023</option><option>2024</option><option>2025</option><option>2026</option></select>
<div class="echart" id="chart"></div>
<div id="cutoff">2026-01-01</div>
<script src="echarts.min.js"></script>
<script src="product-config-charts.js"></script>
</body></html>
""",
        encoding="utf-8",
    )
    js_v1.write_text(
        "const years = [2020,2021,2022,2023,2024,2025,2026];\n"
        "const chartReady = years.length === 7;\n"
        "const configRows = /*{{SLOT:config_rows|js_array}}*/ [];\n"
        "const chart = echarts.init(document.getElementById('chart'));\n"
        "chart.setOption({xAxis:{data:years},series:[{type:'bar',data:years}]});\n",
        encoding="utf-8",
    )
    vendor.write_text(
        """(() => {
const instances = new WeakMap();
window.echarts = {
  init(element) {
    element.setAttribute('_echarts_instance_', 'test-instance');
    const canvas = document.createElement('canvas');
    element.appendChild(canvas);
    const instance = {setOption(option) { canvas.dataset.series = String(option.series.length); }};
    instances.set(element, instance);
    return instance;
  },
  getInstanceByDom(element) { return instances.get(element); }
};
})();
""",
        encoding="utf-8",
    )
    original_html = html_v1.read_bytes()
    original_js = js_v1.read_bytes()
    original_vendor = vendor.read_bytes()

    session_manager.initialize(state)
    session_manager.create_session("v2-pipeline-session")
    coordinator = HarnessRunCoordinator(session_manager)
    run, _goal = coordinator.start_run(
        session_id="v2-pipeline-session",
        query_id="query-v2-pipeline",
        objective=(
            f"参考 {html_v1} 创建新的 V2 版本，包含 HTML 和 JS；"
            "保留 ECharts vendor，年份范围改为 2021-2026。"
        ),
        goal_mode=False,
        verification_enabled=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    html_v2 = report_dir / "product-config-v2.html"
    js_v2 = report_dir / "product-config-charts-v2.js"
    assert set(run.declared_artifact_targets) == {
        str(html_v2),
        str(js_v2),
    }
    assert str(report_dir / "echarts.min-v2.js") not in run.declared_artifact_targets

    for source in (html_v1, js_v1):
        session_manager.add_permission_grant(
            run.session_id,
            grant_type="external_file_read",
            target_kind="exact_file",
            target=str(source),
            capabilities=["read", "external_path"],
            scope="session",
        )
    workspace_backend = FilesystemBackend(
        root_dir=workspace,
        virtual_mode=True,
    )
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id=run.session_id,
        run_id=run.run_id,
        query_id=run.query_id,
        workspace_root=workspace,
    )
    backend.execution_backend = _RegisteredValidatorBackend()
    backend.execution_scratch_host_path = str(scratch)
    tools = {
        tool.name: tool
        for tool in VersionedPatchMiddleware(backend).tools
    }

    copied_html = tools["copy_file"].func(
        source_path=str(html_v1),
        target_path=str(html_v2),
        runtime=_runtime("copy-html", run),
    )
    copied_js = tools["copy_file"].func(
        source_path=str(js_v1),
        target_path=str(js_v2),
        runtime=_runtime("copy-js", run),
    )
    copied_html_payload = json.loads(copied_html.content)
    copied_js_payload = json.loads(copied_js.content)
    assert copied_html.status == copied_js.status == "success"
    assert copied_html_payload["mutation_receipt_id"]
    assert copied_js_payload["mutation_receipt_id"]
    assert copied_html_payload["validation_receipt_ids"]
    assert copied_js_payload["validation_receipt_ids"]
    assert "<!doctype html>" not in copied_html.content
    assert "const years" not in copied_js.content

    patched_html = tools["patch_file"].func(
        file_path=str(html_v2),
        expected_sha256=copied_html_payload["target_sha256"],
        replacements=[
            ReplacementHunk(
                old_string="<option>2020</option>",
                new_string="",
            ),
            ReplacementHunk(
                old_string='src="product-config-charts.js"',
                new_string='src="product-config-charts-v2.js"',
            ),
            ReplacementHunk(
                old_string="2026-01-01",
                new_string="2026-07-23",
            ),
        ],
        runtime=_runtime("patch-html", run),
    )
    patched_js = tools["patch_file"].func(
        file_path=str(js_v2),
        expected_sha256=copied_js_payload["target_sha256"],
        replacements=[
            ReplacementHunk(
                old_string="[2020,2021,2022,2023,2024,2025,2026]",
                new_string="[2021,2022,2023,2024,2025,2026]",
            ),
            ReplacementHunk(
                old_string="years.length === 7",
                new_string="years.length === 6",
            ),
        ],
        runtime=_runtime("patch-js", run),
    )
    assert patched_html.status == "success", patched_html.content
    assert patched_js.status == "success", patched_js.content
    patched_html_payload = json.loads(patched_html.content)
    patched_js_payload = json.loads(patched_js.content)
    assert patched_html_payload["mutation_receipt_id"]
    assert patched_js_payload["mutation_receipt_id"]
    assert patched_html_payload["validation_receipt_ids"]
    assert patched_js_payload["validation_receipt_ids"]

    configuration_rows = [
        {
            "year": 2021 + (index % 6),
            "brand": f"配置品牌{index % 11}",
            "config_name": "空气悬架",
            "config_rate": round((index % 100) / 100, 2),
        }
        for index in range(337)
    ]
    query_result = tmp_path / "configuration-query-result.jsonl"
    query_result.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False) + "\n"
            for row in configuration_rows
        ),
        encoding="utf-8",
    )
    source = register_file_source_reference(
        session_id=run.session_id,
        kind="database_result",
        file_path=query_result,
        media_type="application/x-ndjson",
        schema_ref="schema:configuration-rate:v1",
        row_count=337,
        producer_receipt_ids=["sql-validation-config", "result-store:qr-config"],
        source_ref="source-db-qr-config",
        metadata={
            "columns": [
                "year",
                "brand",
                "config_name",
                "config_rate",
            ]
        },
    )
    materialized = tools["materialize_source_ref"].func(
        source_ref=source["source_ref"],
        destination={
            "kind": "slot",
            "template_path": str(js_v2),
            "template_sha256": patched_js_payload["target_sha256"],
            "slot_id": "config_rows",
            "output_path": str(js_v2),
            "output_mode": "replace",
            "expected_output_sha256": patched_js_payload["target_sha256"],
        },
        renderer="js_array",
        projection=["year", "brand", "config_name", "config_rate"],
        expected_schema_ref="schema:configuration-rate:v1",
        expected_item_count=337,
        runtime=_runtime("materialize-config-rows", run),
    )
    assert materialized.status == "success", materialized.content
    assert "配置品牌0" not in materialized.content
    materialized_payload = json.loads(materialized.content)
    assert materialized_payload["item_count"] == 337
    assert materialized_payload["materialization_receipt_id"]
    assert materialized_payload["mutation_receipt_id"]
    assert materialized_payload["validation_receipt_ids"]

    final_html = html_v2.read_text(encoding="utf-8")
    final_js = js_v2.read_text(encoding="utf-8")
    assert "<option>2020</option>" not in final_html
    assert "<option>2021</option>" in final_html
    assert "<option>2026</option>" in final_html
    assert 'src="echarts.min.js"' in final_html
    assert 'src="product-config-charts-v2.js"' in final_html
    assert "2026-07-23" in final_html
    assert "[2021,2022,2023,2024,2025,2026]" in final_js
    assert "/*{{SLOT:" not in final_js
    assert final_js.count('"config_name":"空气悬架"') == 337
    syntax = subprocess.run(
        ["node", "--check", str(js_v2)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr

    receipts = session_manager.list_external_mutation_receipts(
        run.session_id,
        run_id=run.run_id,
    )
    assert [item["operation"] for item in receipts] == [
        "copy",
        "copy",
        "patch",
        "patch",
        "materialize_replace",
    ]
    assert all(item.get("validation_receipt_id") for item in receipts)
    assert not any(item["operation"] == "delete" for item in receipts)
    assert html_v1.read_bytes() == original_html
    assert js_v1.read_bytes() == original_js
    assert vendor.read_bytes() == original_vendor
    materialization_receipts = session_manager.list_materialization_receipts(
        run.session_id,
        run_id=run.run_id,
    )
    assert len(materialization_receipts) == 1
    materialization_receipt = materialization_receipts[0]
    assert materialization_receipt["source_sha256"] == source["content_sha256"]
    assert materialization_receipt["item_count"] == 337
    assert materialization_receipt["target_sha256"] == materialized_payload[
        "target_sha256"
    ]

    if os.getenv("PUDDINGCLAW_RUN_BROWSER_E2E", "") != "1":
        pytest.skip(
            "set PUDDINGCLAW_RUN_BROWSER_E2E=1 to run Docker Chromium acceptance"
        )
    docker = shutil.which("docker")
    assert docker is not None, "Chromium E2E requires the configured Docker runtime"
    image = "puddingclaw/sandbox:python3.12-node22-chromium-v5"
    image_check = subprocess.run(
        [docker, "image", "inspect", image],
        check=False,
        capture_output=True,
        text=True,
    )
    assert image_check.returncode == 0, image_check.stderr
    validator = (
        Path(__file__).resolve().parents[2]
        / "harness"
        / "docker"
        / "validate-html-report-e2e.mjs"
    )
    browser_validation = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=128m",
            "-v",
            f"{report_dir}:/report:ro",
            "-v",
            (
                f"{validator}:"
                "/opt/puddingclaw/bin/validate-html-report-e2e.mjs:ro"
            ),
            image,
            "node",
            "/opt/puddingclaw/bin/validate-html-report-e2e.mjs",
            "/report/product-config-v2.html",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert browser_validation.returncode == 0, (
        browser_validation.stdout + browser_validation.stderr
    )
    browser_receipt = json.loads(
        browser_validation.stdout.strip().splitlines()[-1]
    )
    assert browser_receipt["passed"] is True
    assert browser_receipt["page"]["chartContainerCount"] == 1
    assert browser_receipt["page"]["initializedChartCount"] == 1
    assert browser_receipt["page"]["renderedChartCount"] == 1
    assert browser_receipt["page"]["selectorOptions"]["year"] == [
        "2021",
        "2022",
        "2023",
        "2024",
        "2025",
        "2026",
    ]
    assert browser_receipt["page"]["cutoffText"] == "2026-07-23"
    assert browser_receipt["runtimeErrors"] == []
    assert browser_receipt["consoleErrors"] == []
    assert browser_receipt["networkErrors"] == []
    artifact_hashes = {
        Path(item["path"]).name: item["content_sha256"]
        for item in browser_receipt["artifactHashes"]
    }
    for artifact_path in (html_v2, js_v2, vendor):
        expected = "sha256:" + hashlib.sha256(
            artifact_path.read_bytes()
        ).hexdigest()
        assert artifact_hashes[artifact_path.name] == expected

    artifact_refs = [
        ValidationArtifactRef(
            artifact_id=(
                "artifact-"
                + hashlib.sha256(
                    f"external\0{artifact_path.resolve()}".encode()
                ).hexdigest()[:20]
            ),
            path=str(artifact_path.resolve()),
            content_sha256=artifact_hashes[artifact_path.name],
        )
        for artifact_path in (html_v2, js_v2, vendor)
    ]
    receipt_seed = json.dumps(
        {
            "run_id": run.run_id,
            "validator": "docker-chromium-report/v1",
            "artifact_hashes": artifact_hashes,
            "page": browser_receipt["page"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    receipt = ValidationReceipt(
        validation_receipt_id=(
            "validation-browser-"
            + hashlib.sha256(receipt_seed.encode()).hexdigest()[:20]
        ),
        run_id=run.run_id,
        validator_kind="browser_runtime",
        validator_version="docker-chromium-report/v1",
        artifact_refs=artifact_refs,
        command_evidence_ref=(
            "sha256:"
            + hashlib.sha256(browser_validation.stdout.encode()).hexdigest()
        ),
        exit_code=0,
        checks_passed=8,
        checks_failed=0,
        status="passed",
        blocking=True,
        commit_authority=True,
        obligation_key="browser_runtime:synthetic-v2-report",
    )
    activation = VerificationActivation(
        activation_id=(
            "verification-activation-"
            + hashlib.sha256(receipt_seed.encode()).hexdigest()[:20]
        ),
        run_id=run.run_id,
        query_id=run.query_id,
        tool_call_id="docker-browser-v2-e2e",
        tool_name="execute",
        pack="code",
        status="succeeded",
        evidence_refs=[
            {
                "kind": "validation_receipt",
                **receipt.model_dump(mode="json"),
                "material": True,
            }
        ],
    )
    persisted_activation, created = (
        session_manager.append_run_verification_activation(
            run.session_id,
            run.run_id,
            activation.model_dump(mode="json"),
        )
    )
    assert created is True
    persisted_receipt = next(
        item
        for item in persisted_activation["evidence_refs"]
        if item.get("kind") == "validation_receipt"
    )
    assert (
        persisted_receipt["validation_receipt_id"]
        == receipt.validation_receipt_id
    )
    ledger_record = session_manager.get_evidence_record(
        run.session_id,
        "validation_receipt",
        receipt.validation_receipt_id,
    )
    assert ledger_record is not None
    assert ledger_record["payload"]["artifact_refs"] == [
        item.model_dump(mode="json") for item in artifact_refs
    ]
