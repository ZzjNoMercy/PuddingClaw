"""Historical SQL replay and admission metrics for the Agent Validator.

This is intentionally a preflight harness, not a second SQL executor.  A
historical case is only counted as an admission candidate after the static
read-only contract passes.  Live-column, semantic and EAV checks still belong
to ``database_sql_validate`` and can be supplied through the async callback
API when a test database/runtime is available.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from analytics.nl2sql.sql_runner import SqlRunnerError, validate_readonly_sql


@dataclass(frozen=True, slots=True)
class ReplayCase:
    case_id: str
    question: str
    sql: str
    database_source_id: str
    allowed_tables: tuple[str, ...]
    expected_status: str = "passed"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any], *, fallback_id: str) -> ReplayCase:
        result = raw.get("result") if isinstance(raw.get("result"), dict) else {}
        route = result.get("route") if isinstance(result.get("route"), dict) else {}
        request = raw.get("request") if isinstance(raw.get("request"), dict) else {}
        sql = str(raw.get("sql") or result.get("sql") or "").strip()
        allowed = raw.get("allowed_tables") or route.get("table_names") or route.get("selected_tables") or []
        tables = tuple(sorted({str(item).strip() for item in allowed if str(item).strip()}))
        if not sql or not tables:
            raise ValueError(f"Replay case {raw.get('case_id') or fallback_id} requires sql and allowed_tables")
        return cls(
            case_id=str(raw.get("case_id") or fallback_id),
            question=str(raw.get("question") or request.get("question") or result.get("question") or ""),
            sql=sql,
            database_source_id=str(
                raw.get("database_source_id")
                or request.get("database_source_id")
                or route.get("database_source_id")
                or ""
            ),
            allowed_tables=tables,
            expected_status=str(raw.get("expected_status") or "passed"),
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class ReplayResult:
    case_id: str
    status: str
    expected_status: str
    error_code: str = ""
    error: str = ""
    unsupported: bool = False


@dataclass(frozen=True, slots=True)
class ReplaySummary:
    total: int
    passed: int
    rejected: int
    unsupported: int
    expected_failures: int
    false_rejections: int
    false_acceptances: int
    results: tuple[ReplayResult, ...]

    @property
    def false_rejection_rate(self) -> float | None:
        denominator = self.total - self.expected_failures - self.unsupported
        return self.false_rejections / denominator if denominator > 0 else 0.0

    @property
    def unsupported_rate(self) -> float | None:
        return self.unsupported / self.total if self.total else None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["results"] = [asdict(item) for item in self.results]
        payload["false_rejection_rate"] = self.false_rejection_rate
        payload["unsupported_rate"] = self.unsupported_rate
        return payload


def load_replay_cases(path: str | Path) -> list[ReplayCase]:
    cases: list[ReplayCase] = []
    for index, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError(f"Replay line {index} must be a JSON object")
        cases.append(ReplayCase.from_mapping(raw, fallback_id=f"line-{index}"))
    return cases


def cases_from_generation_records(records: Iterable[dict[str, Any]]) -> list[ReplayCase]:
    """Convert persisted legacy generations into replay cases without SQL mutation."""

    return [
        ReplayCase.from_mapping(
            {
                "case_id": record.get("id"),
                "request": record.get("request"),
                "result": record.get("result"),
                "metadata": {"legacy_generation_id": record.get("id")},
            },
            fallback_id=f"generation-{index}",
        )
        for index, record in enumerate(records, start=1)
        if isinstance(record, dict)
    ]


def replay_static_contract(cases: Iterable[ReplayCase], *, require_schema_qualified: bool = True) -> ReplaySummary:
    results: list[ReplayResult] = []
    for case in cases:
        try:
            validate_readonly_sql(
                case.sql,
                allowed_tables=list(case.allowed_tables),
                require_schema_qualified=require_schema_qualified,
            )
            results.append(ReplayResult(case.case_id, "passed", case.expected_status))
        except SqlRunnerError as exc:
            unsupported = exc.error_code in {"sql_validation_failed", "column_scope_unresolved"}
            status = "unsupported" if unsupported else "rejected"
            results.append(
                ReplayResult(
                    case.case_id,
                    status,
                    case.expected_status,
                    error_code=exc.error_code,
                    error=str(exc),
                    unsupported=unsupported,
                )
            )
    expected_failures = sum(1 for item in results if item.expected_status != "passed")
    false_rejections = sum(1 for item in results if item.expected_status == "passed" and item.status == "rejected")
    false_acceptances = sum(1 for item in results if item.expected_status != "passed" and item.status == "passed")
    return ReplaySummary(
        total=len(results),
        passed=sum(1 for item in results if item.status == "passed"),
        rejected=sum(1 for item in results if item.status == "rejected"),
        unsupported=sum(1 for item in results if item.status == "unsupported"),
        expected_failures=expected_failures,
        false_rejections=false_rejections,
        false_acceptances=false_acceptances,
        results=tuple(results),
    )


async def replay_with_validator(
    cases: Iterable[ReplayCase],
    validator: Callable[[ReplayCase], Awaitable[dict[str, Any]]],
) -> ReplaySummary:
    """Run the same metrics against a real validator adapter in tests/CI."""

    results: list[ReplayResult] = []
    for case in cases:
        response = await validator(case)
        passed = str(response.get("status") or "") == "passed"
        status = "passed" if passed else str(response.get("status") or "rejected")
        results.append(
            ReplayResult(
                case.case_id,
                status,
                case.expected_status,
                error_code=str(response.get("code") or ""),
                error=str(response.get("message") or ""),
            )
        )
    expected_failures = sum(1 for item in results if item.expected_status != "passed")
    false_rejections = sum(1 for item in results if item.expected_status == "passed" and item.status == "rejected")
    false_acceptances = sum(1 for item in results if item.expected_status != "passed" and item.status == "passed")
    return ReplaySummary(
        total=len(results),
        passed=sum(1 for item in results if item.status == "passed"),
        rejected=sum(1 for item in results if item.status == "rejected"),
        unsupported=sum(1 for item in results if item.status == "unsupported"),
        expected_failures=expected_failures,
        false_rejections=false_rejections,
        false_acceptances=false_acceptances,
        results=tuple(results),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay historical SQL against the static Agent admission contract.")
    parser.add_argument("input", type=Path, help="JSONL replay cases")
    parser.add_argument("--allow-bare-public", action="store_true", help="Keep legacy public-table naming compatibility")
    args = parser.parse_args()
    summary = replay_static_contract(
        load_replay_cases(args.input),
        require_schema_qualified=not args.allow_bare_public,
    )
    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))
    return 0 if summary.false_rejections == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
