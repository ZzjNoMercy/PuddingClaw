"""Export the provider-neutral evaluation protocol JSON Schemas."""

from __future__ import annotations

import json
from pathlib import Path

from evaluation.contracts import PROTOCOL_VERSION, protocol_json_schemas


def main() -> None:
    output = Path(__file__).resolve().parent.parent / "evaluation" / "schemas" / f"protocol-{PROTOCOL_VERSION}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {"protocol_version": PROTOCOL_VERSION, "schemas": protocol_json_schemas()},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(output)


if __name__ == "__main__":
    main()
