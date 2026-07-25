"""SQL literal extraction for semantic enum-consistency checks.

Uses sqlglot (the only SQL parser in the codebase) to extract, for governed
columns, the exact string literals used in IN / = / LIKE predicates and the
labelled arms of CASE expressions. Regex-based extraction is not reliable on
the multi-CTE SQL produced by NL2SQL; keep this module the single place that
turns SQL text into literal sets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

_DIALECT = "postgres"


@dataclass
class CaseLabel:
    """One labelled THEN/ELSE arm of a CASE expression."""

    label: str
    literals: set[str]
    is_else: bool
    # Whether the condition actually referenced a governed column/EAV domain.
    # Labels whose condition is on unrelated columns (e.g. serial_name) must
    # not be judged against this asset's classifications.
    governed: bool = True


@dataclass
class EnumUsage:
    """Literal usage extracted from one SQL statement."""

    # governed key -> literals used in IN/= predicates on that key
    column_literals: dict[str, set[str]] = field(default_factory=dict)
    case_labels: list[CaseLabel] = field(default_factory=list)
    # raw LIKE patterns on governed columns, for forbidden-pattern checks
    like_patterns: list[str] = field(default_factory=list)


def _string_literal(node: Any) -> str | None:
    if isinstance(node, exp.Literal) and node.is_string:
        return str(node.this)
    return None


def _column_name(node: Any) -> str | None:
    if isinstance(node, exp.Column):
        return node.name.lower()
    return None


class _Extractor:
    def __init__(self, governed_columns: set[str], eav_type_names: set[str]) -> None:
        self.governed_columns = {item.lower() for item in governed_columns}
        self.eav_type_names = set(eav_type_names)
        self.usage = EnumUsage()
        # alias name -> governed key ("column:<name>" or "eav:<type_name>")
        self.alias_map: dict[str, str] = {}

    # -- alias resolution ------------------------------------------------

    def _build_alias_map(self, tree: exp.Expression) -> None:
        for alias in tree.find_all(exp.Alias):
            inner = alias.this
            alias_name = str(alias.alias).lower()
            if not alias_name:
                continue
            inner_sql = inner.sql(dialect=_DIALECT).lower()
            for type_name in self.eav_type_names:
                # MAX(CASE WHEN type_name = '能源类型' THEN type_value END) AS x
                if f"'{type_name.lower()}'" in inner_sql and "type_value" in inner_sql:
                    self.alias_map[alias_name] = f"eav:{type_name}"
            # Only a bare column passthrough preserves the governed domain.
            # CASE / computed aliases emit derived values (e.g. classification
            # labels like '传统能源'), so predicates on them must NOT be
            # attributed to the governed column — that leaked labels into the
            # literal set and blocked correct SQL (2026-07-25 incident).
            if isinstance(inner, exp.Column):
                name = inner.name.lower()
                if name in self.governed_columns:
                    self.alias_map[alias_name] = f"column:{name}"

    def _resolve(self, node: Any) -> str | None:
        name = _column_name(node)
        if name is None:
            return None
        # Table-qualified references (other_table.et) must not be bound to an
        # alias defined inside some unrelated CTE.
        if not getattr(node, "table", None) and name in self.alias_map:
            return self.alias_map[name]
        if name in self.governed_columns:
            return f"column:{name}"
        if name == "type_value":
            # EAV value column is governed only when a sibling predicate pins
            # the type_name; handled in _scan_eav below.
            return None
        return None

    # -- predicate scanning ----------------------------------------------

    def _record(self, key: str | None, literals: set[str]) -> None:
        if key and literals:
            self.usage.column_literals.setdefault(key, set()).update(literals)

    def _literals_of(self, nodes: Any) -> set[str]:
        result: set[str] = set()
        for node in nodes or []:
            value = _string_literal(node)
            if value is not None:
                result.add(value)
        return result

    def _scan_predicates(self, tree: exp.Expression) -> None:
        for node in tree.find_all(exp.In):
            # NOT IN is exclusion semantics: excluding a value (even a dirty
            # one) is a legitimate defensive filter, not an inclusion claim.
            if isinstance(node.parent, exp.Not):
                continue
            self._record(self._resolve(node.this), self._literals_of(node.expressions))
        for node in tree.find_all(exp.EQ):
            left, right = node.this, node.expression
            for column_side, value_side in ((left, right), (right, left)):
                value = _string_literal(value_side)
                if value is not None:
                    self._record(self._resolve(column_side), {value})
        for node in tree.find_all(exp.Like):
            pattern = _string_literal(node.expression)
            if not pattern:
                continue
            if self._resolve(node.this) is not None:
                self.usage.like_patterns.append(pattern)
                continue
            if _column_name(node.this) == "type_value":
                pin = self._type_name_pin(node)
                if pin is not None and pin in self.eav_type_names:
                    self.usage.like_patterns.append(pattern)

    def _type_name_pin(self, node: exp.Expression) -> str | None:
        """type_name pin for a type_value predicate.

        Walk ancestors nearest-first so the pin comes from the same AND
        conjunction branch, and only consider pins inside the node's own
        query scope — never an outer query's type_name across a subquery
        boundary, and never a sibling branch's type_name.
        """

        def query_scope(item: exp.Expression) -> exp.Select | None:
            cursor: exp.Expression | None = item
            while cursor is not None and not isinstance(cursor, exp.Select):
                cursor = cursor.parent
            return cursor if isinstance(cursor, exp.Select) else None

        node_scope = query_scope(node)
        ancestor = node.parent
        while ancestor is not None and ancestor is not node_scope:
            for eq in ancestor.find_all(exp.EQ):
                if query_scope(eq) is not node_scope:
                    continue
                for column_side, value_side in (
                    (eq.this, eq.expression),
                    (eq.expression, eq.this),
                ):
                    if _column_name(column_side) == "type_name":
                        return _string_literal(value_side)
            ancestor = ancestor.parent
        return None

    def _scan_eav(self, tree: exp.Expression) -> None:
        """Bind type_value literals to the nearest type_name pin in scope.

        A type_value predicate is attributed only when a sibling
        ``type_name = 'X'`` exists within its nearest enclosing AND / CASE-WHEN
        branch. Top-level ``type_name IN (...)`` row filters do NOT pin a
        domain: literals like ``type_value = '皮卡'`` belong to 级别, not to
        whatever names happen to appear in that filter. The pin lookup stops
        at subquery boundaries.
        """

        def attach(node: exp.Expression, literals: set[str]) -> None:
            if not literals:
                return
            pin = self._type_name_pin(node)
            if pin is not None and pin in self.eav_type_names:
                self._record(f"eav:{pin}", literals)

        for node in tree.find_all(exp.In):
            if _column_name(node.this) == "type_value":
                attach(node, self._literals_of(node.expressions))
        for node in tree.find_all(exp.EQ):
            for column_side, value_side in ((node.this, node.expression), (node.expression, node.this)):
                if _column_name(column_side) == "type_value":
                    value = _string_literal(value_side)
                    if value is not None:
                        attach(node, {value})

    def _scan_case_labels(self, tree: exp.Expression) -> None:
        for case in tree.find_all(exp.Case):
            arm_governed: list[bool] = []
            for when in case.args.get("ifs") or []:
                label = _string_literal(when.args.get("true"))
                if label is None:
                    continue
                sub = _Extractor(self.governed_columns, self.eav_type_names)
                sub.alias_map = dict(self.alias_map)
                sub._scan_predicates(when.this)
                merged: set[str] = set()
                for literals in sub.usage.column_literals.values():
                    merged.update(literals)
                governed = bool(sub.usage.column_literals)
                arm_governed.append(governed)
                self.usage.case_labels.append(
                    CaseLabel(label=label, literals=merged, is_else=False, governed=governed)
                )
            default = _string_literal(case.args.get("default"))
            if default is not None:
                # An ELSE arm is judged only when at least one sibling arm
                # actually referenced a governed column; pure naming
                # coincidences on unrelated columns are skipped.
                self.usage.case_labels.append(
                    CaseLabel(label=default, literals=set(), is_else=True, governed=any(arm_governed))
                )

    def run(self, sql: str) -> EnumUsage:
        tree = sqlglot.parse_one(sql, read=_DIALECT)
        self._build_alias_map(tree)
        self._scan_predicates(tree)
        self._scan_eav(tree)
        self._scan_case_labels(tree)
        return self.usage


def extract_enum_usage(
    sql: str,
    governed_columns: set[str],
    eav_type_names: set[str],
) -> EnumUsage:
    """Extract literals on governed columns from one SQL statement."""

    extractor = _Extractor(governed_columns, eav_type_names)
    return extractor.run(sql)
