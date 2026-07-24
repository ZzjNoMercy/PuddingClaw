"""Copy and source-reference materialization filesystem tools."""

import json
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

from tools.filesystem.inspect import digest, read_all
from tools.filesystem.schemas import (
    CopyFileInput,
    MaterializeDestination,
    MaterializeSourceRefInput,
)


def copy_external_file(
    backend: Any,
    source_path: str,
    target_path: str,
    *,
    expected_source_sha256: str | None,
) -> dict[str, Any]:
    copy = getattr(backend, "copy_external_file", None)
    if not callable(copy):
        return {
            "status": "io_error",
            "error_code": "copy_not_supported",
            "next_action": "report_infrastructure_error",
        }
    return copy(
        source_path,
        target_path,
        expected_source_sha256=expected_source_sha256,
    )


def build_copy_tools(backend: Any) -> list[StructuredTool]:
    def copy_file(
        source_path: str,
        target_path: str,
        runtime: ToolRuntime[Any, Any],
        expected_source_sha256: str | None = None,
    ) -> ToolMessage:
        result = copy_external_file(
            backend,
            source_path,
            target_path,
            expected_source_sha256=expected_source_sha256,
        )
        status = str(result.get("status") or "io_error")
        return ToolMessage(
            content=json.dumps(result, ensure_ascii=False, sort_keys=True),
            name="copy_file",
            tool_call_id=runtime.tool_call_id,
            status="success" if status == "completed" else "error",
        )

    def materialize_source_ref(
        source_ref: str,
        destination: MaterializeDestination,
        renderer: str,
        runtime: ToolRuntime[Any, Any],
        projection: list[str] | None = None,
        expected_schema_ref: str | None = None,
        expected_item_count: int | None = None,
    ) -> ToolMessage:
        from harness.source_materialization import (
            SourceMaterializationError,
            fill_typed_slot,
            persist_materialization_receipt,
            public_source_reference,
            render_source,
            resolve_source_bytes,
        )

        context = runtime.context if isinstance(runtime.context, dict) else {}
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        query_id = str(context.get("query_id") or "")
        if not session_id or not run_id:
            return ToolMessage(
                content=json.dumps(
                    {
                        "status": "error",
                        "error_code": "active_run_required",
                        "next_action": "retry_in_active_run",
                    },
                    sort_keys=True,
                ),
                name="materialize_source_ref",
                tool_call_id=runtime.tool_call_id,
                status="error",
            )
        try:
            if isinstance(destination, dict):
                destination = MaterializeDestination.model_validate(destination)
            source, source_bytes = resolve_source_bytes(
                session_id,
                source_ref,
            )
            rendered, item_count = render_source(
                source,
                source_bytes,
                renderer=renderer,
                projection=list(projection or []),
                expected_schema_ref=expected_schema_ref,
                expected_item_count=expected_item_count,
            )
            template_sha256: str | None = None
            slot_id: str | None = None
            if destination.kind == "slot":
                template, template_error = read_all(
                    backend,
                    str(destination.template_path),
                )
                if template_error is not None or template is None:
                    raise SourceMaterializationError(
                        "template_unavailable",
                        template_error or "unable to read template",
                        next_action="inspect_template",
                    )
                template_sha256 = digest(template)
                if template_sha256 != destination.template_sha256:
                    raise SourceMaterializationError(
                        "template_version_changed",
                        (
                            f"expected {destination.template_sha256}, "
                            f"current {template_sha256}"
                        ),
                        next_action="inspect_template",
                    )
                slot_id = str(destination.slot_id)
                output_text = fill_typed_slot(
                    template,
                    slot_id=slot_id,
                    renderer=renderer,
                    rendered=rendered,
                )
                candidate = output_text.encode("utf-8")
                target_path = str(destination.output_path)
                mode = destination.output_mode
                expected_target = destination.expected_output_sha256
            else:
                candidate = rendered
                target_path = str(destination.target_path)
                mode = destination.mode
                expected_target = destination.expected_sha256

            virtual_target = target_path.replace("\\", "/").startswith(
                ("/workspace/", "/scratch/")
            ) or not Path(target_path).is_absolute()
            if virtual_target:
                try:
                    candidate_text = candidate.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise SourceMaterializationError(
                        "virtual_binary_materialization_unsupported",
                        "binary identity materialization requires an external target",
                        next_action="choose_external_target",
                    ) from exc
                if mode == "create":
                    write_result = backend.write(
                        target_path,
                        candidate_text,
                    )
                else:
                    current, current_error = read_all(
                        backend,
                        target_path,
                    )
                    if current_error is not None or current is None:
                        raise SourceMaterializationError(
                            "target_unavailable",
                            current_error or "unable to read target",
                            next_action="inspect_target",
                        )
                    current_sha256 = digest(current)
                    if current_sha256 != expected_target:
                        raise SourceMaterializationError(
                            "target_version_changed",
                            (
                                f"expected {expected_target}, "
                                f"current {current_sha256}"
                            ),
                            next_action="inspect_target",
                        )
                    write_result = backend.edit(
                        target_path,
                        current,
                        candidate_text,
                        replace_all=False,
                    )
                if write_result.error:
                    raise SourceMaterializationError(
                        "materialization_commit_failed",
                        str(write_result.error),
                        next_action="inspect_target",
                    )
                commit_result = {
                    "status": "completed",
                    "target_path": str(write_result.path or target_path),
                    "target_sha256": digest(candidate_text),
                    "receipt_id": "",
                    "mutation_receipt_id": "",
                    "validation_receipt": None,
                    "validation_receipt_ids": [],
                }
            else:
                if mode == "create":
                    create = getattr(backend, "create_external_file", None)
                    if not callable(create):
                        raise SourceMaterializationError(
                            "materialization_create_unsupported",
                            "Backend does not support external byte creation",
                            next_action="report_infrastructure_error",
                        )
                    commit_result = create(
                        target_path,
                        candidate,
                        operation="materialize_create",
                    )
                else:
                    replace = getattr(backend, "replace_external_file", None)
                    if not callable(replace):
                        raise SourceMaterializationError(
                            "materialization_replace_unsupported",
                            "Backend does not support external byte replacement",
                            next_action="report_infrastructure_error",
                        )
                    commit_result = replace(
                        target_path,
                        candidate,
                        expected_sha256=str(expected_target),
                        operation="materialize_replace",
                    )
                if str(commit_result.get("status") or "") != "completed":
                    return ToolMessage(
                        content=json.dumps(
                            commit_result,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        name="materialize_source_ref",
                        tool_call_id=runtime.tool_call_id,
                        status="error",
                    )

            validation_receipt = commit_result.get("validation_receipt")
            validation_ids = [
                str(item)
                for item in commit_result.get("validation_receipt_ids") or []
                if str(item)
            ]
            if (
                not validation_ids
                and isinstance(validation_receipt, dict)
                and validation_receipt.get("validation_receipt_id")
            ):
                validation_ids = [
                    str(validation_receipt.get("validation_receipt_id"))
                ]
            receipt = persist_materialization_receipt(
                session_id=session_id,
                run_id=run_id,
                query_id=query_id,
                source=source,
                renderer=renderer,
                target_path=str(commit_result.get("target_path") or target_path),
                target_sha256=str(commit_result.get("target_sha256") or ""),
                item_count=item_count,
                mutation_receipt_id=str(
                    commit_result.get("mutation_receipt_id")
                    or commit_result.get("receipt_id")
                    or ""
                ),
                template_sha256=template_sha256,
                slot_id=slot_id,
                validation_receipt_ids=validation_ids,
            )
            return ToolMessage(
                content=json.dumps(
                    {
                        "status": "completed",
                        "source": public_source_reference(source),
                        "renderer": f"{renderer}/v1",
                        "item_count": item_count,
                        "target_path": receipt["target_path"],
                        "target_sha256": receipt["target_sha256"],
                        "materialization_receipt_id": receipt[
                            "materialization_receipt_id"
                        ],
                        "mutation_receipt_id": receipt[
                            "mutation_receipt_id"
                        ],
                        "validation_receipt_ids": validation_ids,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                name="materialize_source_ref",
                tool_call_id=runtime.tool_call_id,
                status="success",
                artifact={"materialization_receipt": receipt},
            )
        except SourceMaterializationError as exc:
            return ToolMessage(
                content=json.dumps(
                    exc.as_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                name="materialize_source_ref",
                tool_call_id=runtime.tool_call_id,
                status="error",
            )
    return [
        StructuredTool.from_function(
            name="copy_file",
            description=(
                "Create one UTF-8 file from an authorized absolute Host source without "
                "streaming its body through model context. A /workspace target uses the "
                "existing workspace boundary and needs no external write Grant; an absolute "
                "Host target still requires exact Host write authority. Records source/target "
                "hashes and never overwrites an existing target."
            ),
            func=copy_file,
            args_schema=CopyFileInput,
            infer_schema=False,
        ),
        StructuredTool.from_function(
            name="materialize_source_ref",
            description=(
                "Materialize an immutable server SourceReference directly into a file "
                "or one typed template slot through a deterministic renderer. Full payload "
                "bytes never enter model context. The commit is permission-checked, atomic, "
                "validated, and returns a MaterializationReceipt."
            ),
            func=materialize_source_ref,
            args_schema=MaterializeSourceRefInput,
            infer_schema=False,
        ),
    ]
