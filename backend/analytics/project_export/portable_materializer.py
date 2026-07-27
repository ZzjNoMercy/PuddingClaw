"""Generate the platform-independent logical-dataset materializer."""

from __future__ import annotations

PORTABLE_LOGICAL_MATERIALIZER = r'''#!/usr/bin/env python3
"""Materialize an exported logical dataset without PuddingClaw runtime APIs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]


def _read_table(binding):
    path = Path(str(binding["path"]))
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(path, sheet_name=binding.get("sheet_name"))
    if suffix == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path, sep="\t" if suffix == ".tsv" else ",")


def materialize(binding_id, bindings, stack=()):
    if binding_id in stack:
        raise ValueError("logical dataset cycle: " + " -> ".join((*stack, binding_id)))
    binding = bindings[binding_id]
    if binding.get("kind") == "spreadsheet":
        frame = _read_table(binding)
        frame["_pc_source_asset_id"] = binding_id
        return frame
    if binding.get("kind") != "logical_dataset_descriptor":
        raise ValueError(f"binding {binding_id} is not a file-backed dataset")
    source_ids = [str(item) for item in binding.get("source_asset_ids") or []]
    if not source_ids:
        raise ValueError(f"logical dataset {binding_id} has no sources")
    frames = [materialize(source_id, bindings, (*stack, binding_id)) for source_id in source_ids]
    mode = str(binding.get("schema_mode") or "strict")
    if mode == "strict":
        baseline = list(frames[0].columns)
        if any(list(frame.columns) != baseline for frame in frames[1:]):
            raise ValueError(f"logical dataset {binding_id} has schema drift in strict mode")
    canonical = [str(item) for item in binding.get("canonical_columns") or []]
    if canonical:
        for frame in frames:
            for column in canonical:
                if column not in frame.columns:
                    frame[column] = pd.NA
            if mode == "baseline_fill_missing":
                frame.drop(columns=[column for column in frame.columns if column not in canonical and not column.startswith("_pc_")], inplace=True)
    return pd.concat(frames, ignore_index=True, copy=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binding_id")
    parser.add_argument("--bindings", type=Path, default=ROOT / "bindings.local.yaml")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.bindings.read_text(encoding="utf-8"))
    frame = materialize(args.binding_id, payload["bindings"])
    output = args.output or ROOT / "data" / "materialized" / f"{args.binding_id}.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output, index=False)
    print(json.dumps({"binding_id": args.binding_id, "path": str(output), "rows": len(frame), "columns": list(frame.columns)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
'''
