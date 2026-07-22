"""Deterministic cross-file contracts for delivered UI artifacts."""

from __future__ import annotations

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit


class _HeatmapHTMLParser(HTMLParser):
    """Collect only live DOM nodes; HTML comments never enter callbacks."""

    def __init__(self, select_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.select_id = select_id
        self.select_present = False
        self._select_depth = 0
        self._option_attrs: dict[str, str | None] | None = None
        self._option_text: list[str] = []
        self.option_years: list[str] = []
        self.selected_years: list[str] = []
        self.script_sources: list[str] = []

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str | None]:
        return {str(key).lower(): value for key, value in attrs}

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        lowered = tag.lower()
        normalized = self._attrs(attrs)
        if lowered == "script" and normalized.get("src"):
            self.script_sources.append(str(normalized["src"]))
        if lowered == "select":
            if self._select_depth:
                self._select_depth += 1
            elif normalized.get("id") == self.select_id:
                self.select_present = True
                self._select_depth = 1
            return
        if lowered == "option" and self._select_depth:
            self._option_attrs = normalized
            self._option_text = []

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._option_attrs is not None:
            self._option_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "option" and self._option_attrs is not None:
            year = str(self._option_attrs.get("value") or "").strip() or "".join(
                self._option_text
            ).strip()
            if year:
                self.option_years.append(year)
                if "selected" in self._option_attrs:
                    self.selected_years.append(year)
            self._option_attrs = None
            self._option_text = []
            return
        if lowered == "select" and self._select_depth:
            self._select_depth -= 1


def _javascript_code_mask(source: str) -> str:
    """Preserve code punctuation/identifiers while blanking comments/literals."""

    output = list(source)
    index = 0
    state = "code"
    quote = ""
    escaped = False
    previous_significant = ""
    while index < len(source):
        character = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if state == "line_comment":
            if character == "\n":
                state = "code"
            else:
                output[index] = " "
            index += 1
            continue
        if state == "block_comment":
            output[index] = " "
            if character == "*" and following == "/":
                output[index + 1] = " "
                state = "code"
                index += 2
            else:
                index += 1
            continue
        if state in {"string", "regex"}:
            output[index] = " "
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif (state == "string" and character == quote) or (
                state == "regex" and character == "/"
            ):
                state = "code"
            index += 1
            continue
        if character == "/" and following == "/":
            output[index] = output[index + 1] = " "
            state = "line_comment"
            index += 2
            continue
        if character == "/" and following == "*":
            output[index] = output[index + 1] = " "
            state = "block_comment"
            index += 2
            continue
        if character in {'"', "'", "`"}:
            output[index] = " "
            quote = character
            state = "string"
            index += 1
            continue
        if character == "/" and previous_significant in {"", "=", "(", "[", "{", ",", ":", ";", "!", "?"}:
            output[index] = " "
            state = "regex"
            index += 1
            continue
        if not character.isspace():
            previous_significant = character
        index += 1
    return "".join(output)


def _balanced_end(mask: str, start: int, opening: str, closing: str) -> int | None:
    depth = 0
    for index in range(start, len(mask)):
        character = mask[index]
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _skip_js_trivia(source: str, index: int) -> int:
    while index < len(source):
        if source[index].isspace():
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            return len(source) if newline < 0 else _skip_js_trivia(source, newline + 1)
        if source.startswith("/*", index):
            closing = source.find("*/", index + 2)
            return len(source) if closing < 0 else _skip_js_trivia(source, closing + 2)
        break
    return index


def _simple_quoted_value(source: str, index: int) -> tuple[str | None, int]:
    index = _skip_js_trivia(source, index)
    if index >= len(source) or source[index] not in {'"', "'"}:
        return None, index
    quote = source[index]
    closing = index + 1
    escaped = False
    while closing < len(source):
        character = source[closing]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == quote:
            return source[index + 1 : closing], closing + 1
        closing += 1
    return None, index


def _heatmap_matrices(
    javascript: str, mask: str, object_name: str
) -> dict[str, Any]:
    import re

    assignment = re.search(
        rf"\b(?:const|let|var)\s+{re.escape(object_name)}\s*=\s*\{{",
        mask,
    )
    if assignment is None:
        return {}
    opening = mask.find("{", assignment.start())
    closing = _balanced_end(mask, opening, "{", "}")
    if closing is None:
        return {}
    matrices: dict[str, Any] = {}
    depth = 0
    property_starts = [opening + 1]
    for index in range(opening, closing):
        character = mask[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
        elif character == "," and depth == 1:
            property_starts.append(index + 1)
    for start in property_starts:
        year, cursor = _simple_quoted_value(javascript, start)
        if year is None or len(year) != 4 or not year.isdigit():
            continue
        cursor = _skip_js_trivia(javascript, cursor)
        if cursor >= len(javascript) or javascript[cursor] != ":":
            continue
        cursor = _skip_js_trivia(javascript, cursor + 1)
        if cursor >= len(javascript) or javascript[cursor] != "[":
            continue
        array_end = _balanced_end(mask, cursor, "[", "]")
        if array_end is None:
            matrices[year] = None
            continue
        try:
            matrices[year] = json.loads(javascript[cursor:array_end])
        except json.JSONDecodeError:
            matrices[year] = None
    return matrices


def _declared_year(javascript: str, mask: str, variable: str) -> str | None:
    import re

    match = re.search(
        rf"\b(?:const|let|var)\s+{re.escape(variable)}\s*=", mask
    )
    if match is None:
        return None
    year, _cursor = _simple_quoted_value(javascript, match.end())
    return year if year is not None and len(year) == 4 and year.isdigit() else None


def _change_handler_uses_heatmap(mask: str, javascript: str, data_name: str, year_name: str) -> bool:
    import re

    reference = re.compile(
        rf"\b{re.escape(data_name)}\s*\[\s*{re.escape(year_name)}\s*\]"
    )
    for match in re.finditer(r"\.\s*addEventListener\s*\(", mask):
        opening = mask.find("(", match.start())
        closing = _balanced_end(mask, opening, "(", ")")
        if closing is None:
            continue
        event_name, _cursor = _simple_quoted_value(javascript, opening + 1)
        if event_name == "change" and reference.search(mask, opening, closing):
            return True
    return False


def _javascript_syntax(javascript: str) -> tuple[bool, str | None]:
    node = shutil.which("node")
    if node is None:
        return False, "node executable is unavailable"
    try:
        result = subprocess.run(
            [node, "--check", "-"],
            input=javascript,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    error = (result.stderr or result.stdout or "").strip()
    return result.returncode == 0, error[:1000] or None


def _script_basename(source: str) -> str:
    path = unquote(urlsplit(source).path).replace("\\", "/")
    return PurePosixPath(path).name


def validate_heatmap_year_contract(
    *,
    html: str,
    javascript: str,
    javascript_filename: str,
    select_id: str = "heatmapYearSelect",
    data_object_name: str = "heatmapByYear",
    current_year_variable: str = "currentHeatYear",
    expected_rows: int = 8,
    expected_columns: int = 10,
) -> dict[str, Any]:
    """Validate live selector, loaded script, syntax, defaults and matrices."""

    parser = _HeatmapHTMLParser(select_id)
    parser.feed(html)
    parser.close()
    syntax_valid, syntax_error = _javascript_syntax(javascript)
    mask = _javascript_code_mask(javascript) if syntax_valid else " " * len(javascript)
    matrices = _heatmap_matrices(javascript, mask, data_object_name)
    current_year = _declared_year(javascript, mask, current_year_variable)
    malformed_years = sorted(
        year
        for year, matrix in matrices.items()
        if not isinstance(matrix, list)
        or len(matrix) != expected_rows
        or any(
            not isinstance(row, list) or len(row) != expected_columns
            for row in matrix
        )
    )
    data_years = sorted(matrices)
    script_basenames = [_script_basename(item) for item in parser.script_sources]
    checks = {
        "select_present": parser.select_present,
        "script_source_matches": javascript_filename in script_basenames,
        "javascript_syntax_valid": syntax_valid,
        "year_key_set_equal": sorted(set(parser.option_years)) == data_years
        and bool(data_years),
        "default_year_equal": len(parser.selected_years) == 1
        and parser.selected_years[0] == current_year
        and current_year in matrices,
        "matrix_shape_valid": not malformed_years and bool(matrices),
        "event_reference_valid": _change_handler_uses_heatmap(
            mask, javascript, data_object_name, current_year_variable
        ),
    }
    return {
        "contract_id": "heatmap_year_contract/v1",
        "passed": all(checks.values()),
        "checks": checks,
        "observed": {
            "option_years": parser.option_years,
            "selected_years": parser.selected_years,
            "script_sources": parser.script_sources,
            "expected_javascript_filename": javascript_filename,
            "data_years": data_years,
            "current_year": current_year,
            "malformed_years": malformed_years,
            "expected_shape": [expected_rows, expected_columns],
            "javascript_syntax_error": syntax_error,
        },
    }


__all__ = ["validate_heatmap_year_contract"]
