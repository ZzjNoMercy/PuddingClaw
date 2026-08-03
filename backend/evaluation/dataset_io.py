"""Safe, provider-neutral Dataset import/export formats."""

from __future__ import annotations

import csv
import io
import json
from typing import Literal

from .contracts import DatasetBundle, EvalCase, EvalDataset, new_id

MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_CASES = 10_000


def export_dataset(bundle: DatasetBundle, format: Literal["bundle", "jsonl", "csv"]) -> str:
    if format == "bundle":
        checked = bundle.model_copy(update={"checksum": bundle.content_checksum()})
        return checked.model_dump_json(indent=2)
    if format == "jsonl":
        dataset = bundle.dataset.model_copy(update={"cases": []})
        rows = [json.dumps({"type": "dataset", "dataset": dataset.model_dump(mode="json")}, ensure_ascii=False)]
        rows.extend(
            json.dumps({"type": "case", "case": case.model_dump(mode="json")}, ensure_ascii=False)
            for case in bundle.dataset.cases
        )
        return "\n".join(rows) + "\n"

    output = io.StringIO()
    fieldnames = [
        "case_json",
        "question",
        "answer",
        "case_type",
        "expected_tool",
        "case_id",
        "name",
        "description",
        "input_json",
        "setup_json",
        "expectations_json",
        "criticality",
        "tags_json",
        "metadata_json",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for case in bundle.dataset.cases:
        payload = case.model_dump(mode="json")
        writer.writerow(
            {
                "case_json": json.dumps(payload, ensure_ascii=False),
                "question": case.input.message or "",
                "answer": case.expectations.exact_output or case.expectations.reference_answer or "",
                "case_type": case.tags[0] if case.tags else "",
                "expected_tool": ",".join(case.expectations.required_tools),
                "case_id": case.case_id,
                "name": case.name,
                "description": case.description,
                "input_json": json.dumps(payload["input"], ensure_ascii=False),
                "setup_json": json.dumps(payload["setup"], ensure_ascii=False),
                "expectations_json": json.dumps(payload["expectations"], ensure_ascii=False),
                "criticality": case.criticality,
                "tags_json": json.dumps(case.tags, ensure_ascii=False),
                "metadata_json": json.dumps(case.metadata, ensure_ascii=False),
            }
        )
    return output.getvalue()


def import_dataset(raw: str, format: Literal["bundle", "jsonl", "csv"], *, name: str | None = None) -> EvalDataset:
    if len(raw.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ValueError("Dataset import exceeds 10 MiB limit")
    if format == "bundle":
        bundle = DatasetBundle.model_validate_json(raw)
        if bundle.checksum and bundle.checksum != bundle.content_checksum():
            raise ValueError("Dataset Bundle checksum mismatch")
        dataset = bundle.dataset
    elif format == "jsonl":
        dataset_payload = None
        cases: list[EvalCase] = []
        for line_number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("type") == "dataset":
                if dataset_payload is not None:
                    raise ValueError("JSONL contains multiple Dataset headers")
                dataset_payload = record["dataset"]
            elif record.get("type") == "case":
                cases.append(EvalCase.model_validate(record["case"]))
            else:
                raise ValueError(f"Unsupported JSONL record on line {line_number}")
        if dataset_payload is None:
            raise ValueError("JSONL Dataset header is missing")
        dataset = EvalDataset.model_validate({**dataset_payload, "cases": cases})
    else:
        cases = []
        for row in csv.DictReader(io.StringIO(raw)):
            complete = row.get("case_json")
            if complete:
                cases.append(EvalCase.model_validate(json.loads(complete)))
                continue
            question = row.get("question") or row.get("user_input") or ""
            answer = row.get("answer") or row.get("expected_output") or ""
            expected_tool = row.get("expected_tool") or ""
            case_type = row.get("case_type") or ""
            answer_candidates = [item.strip() for item in answer.split("|") if item.strip()]
            payload: dict[str, object] = {
                "name": row.get("name") or question[:80] or "Imported Case",
                "description": row.get("description") or "",
                "input": json.loads(row["input_json"]) if row.get("input_json") else {"message": question},
                "setup": json.loads(row.get("setup_json") or "{}"),
                "expectations": json.loads(row["expectations_json"])
                if row.get("expectations_json")
                else {
                    "reference_answer": answer or None,
                    "contains_any": answer_candidates,
                    "required_tools": [
                        item.strip() for item in expected_tool.split(",") if item.strip()
                    ],
                },
                "criticality": row.get("criticality") or "normal",
                "tags": json.loads(row["tags_json"])
                if row.get("tags_json")
                else ([case_type] if case_type else []),
                "metadata": json.loads(row["metadata_json"])
                if row.get("metadata_json")
                else ({"case_type": case_type} if case_type else {}),
            }
            if row.get("case_id"):
                payload["case_id"] = row["case_id"]
            cases.append(EvalCase.model_validate(payload))
        dataset = EvalDataset(name=name or "Imported Dataset", cases=cases)
    if len(dataset.cases) > MAX_CASES:
        raise ValueError(f"Dataset exceeds {MAX_CASES} Case limit")
    # Imports always create a new local identity and draft. Source identity is
    # retained only as inert metadata, never trusted as a primary key.
    source = {"dataset_id": dataset.dataset_id, "version": dataset.current_version}
    return EvalDataset(
        name=name or dataset.name,
        description=dataset.description,
        default_profile=dataset.default_profile,
        tags=dataset.tags,
        metadata={**dataset.metadata, "imported_from": source},
        cases=[
            case.model_copy(update={"case_id": new_id("case"), "revision_id": new_id("rev")}) for case in dataset.cases
        ],
    )
