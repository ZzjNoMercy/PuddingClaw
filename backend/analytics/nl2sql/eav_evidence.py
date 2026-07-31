"""Deterministic evidence checks for EAV ``type_name`` literals.

Vanna is a candidate-discovery index.  It is not the source of truth for
whether a physical EAV value currently exists, and cosine similarity cannot
prove that two values are semantically interchangeable.  This module keeps
those responsibilities explicit:

* sqlglot extracts the physical literals used by generated SQL;
* live database inspection proves existence;
* semantic bindings declare when several physical names form one concept.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

_DIALECT = "postgres"
_UNIT_SUFFIX = re.compile(r"\s*[\[（(][^\]）)]*[\]）)]\s*$")
_NON_WORD = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


@dataclass(frozen=True)
class EavEquivalenceBinding:
    """Server-loaded contract for one logical EAV concept."""

    concept: str
    type_names: tuple[str, ...]
    match: str = "any"
    value_resolution: str = "coalesce_by_priority"
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class EavEvidenceCheck:
    """Result of checking one generated SQL statement."""

    used_type_names: frozenset[str]
    unsupported: frozenset[str]
    incomplete_bindings: tuple[EavEquivalenceBinding, ...]
    invalid_binding_resolutions: tuple[EavEquivalenceBinding, ...]
    unprovable_predicates: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not (
            self.unsupported
            or self.incomplete_bindings
            or self.invalid_binding_resolutions
            or self.unprovable_predicates
        )


def _literal(node: Any) -> str | None:
    if isinstance(node, exp.Literal) and node.is_string:
        return str(node.this)
    return None


def _reachable_scopes(tree: exp.Expression) -> list[Scope]:
    root = build_scope(tree)
    if root is None:
        return []
    found: list[Scope] = []
    seen: set[int] = set()

    def visit(scope: Scope) -> None:
        if id(scope) in seen:
            return
        seen.add(id(scope))
        found.append(scope)
        for branch in [*scope.union_scopes, *scope.subquery_scopes]:
            visit(branch)
        for _alias, (_node, source) in scope.selected_sources.items():
            if isinstance(source, Scope):
                visit(source)

    visit(root)
    return found


def _resolved_eav_column_ids(
    tree: exp.Expression,
    column_name: str,
) -> tuple[set[int], set[int], list[Scope]]:
    """Resolve EAV columns per SQL scope; never trust a global alias name."""

    resolved: set[int] = set()
    ambiguous: set[int] = set()
    scopes = _reachable_scopes(tree)

    def source_depends_on_eav(source: Any, seen: set[int] | None = None) -> bool:
        if isinstance(source, exp.Table):
            return source.name.lower() == "vehicle_params"
        if not isinstance(source, Scope):
            return False
        visited = set(seen or ())
        if id(source) in visited:
            return False
        visited.add(id(source))
        return any(
            source_depends_on_eav(selected_source, visited)
            for _alias, (_node, selected_source) in source.selected_sources.items()
        ) or any(
            source_depends_on_eav(branch, visited)
            for branch in [*source.union_scopes, *source.subquery_scopes]
        )

    for scope in scopes:
        eav_aliases = {
            str(alias).lower()
            for alias, source in scope.sources.items()
            if source_depends_on_eav(source)
        }
        if not eav_aliases:
            continue
        selected_source_count = len(scope.selected_sources)
        for column in scope.columns:
            if column.name.lower() != column_name:
                continue
            qualifier = str(column.table or "").lower()
            if qualifier:
                if qualifier in eav_aliases:
                    resolved.add(id(column))
            elif selected_source_count == 1 and len(eav_aliases) == 1:
                resolved.add(id(column))
            else:
                ambiguous.add(id(column))
    return resolved, ambiguous, scopes


def _is_resolved_column(node: Any, resolved_ids: set[int], name: str) -> bool:
    return isinstance(node, exp.Column) and node.name.lower() == name and id(node) in resolved_ids


def _analyze_eav_usage(sql: str) -> tuple[set[str], tuple[str, ...]]:
    if not str(sql or "").strip():
        return set(), ()
    tree = sqlglot.parse_one(sql, read=_DIALECT)
    type_name_ids, ambiguous_ids, _scopes = _resolved_eav_column_ids(tree, "type_name")
    if not type_name_ids and not ambiguous_ids:
        return set(), ()
    values: set[str] = set()
    proven_columns: set[int] = set()
    for node in tree.find_all(exp.EQ):
        for column_side, literal_side in (
            (node.this, node.expression),
            (node.expression, node.this),
        ):
            if not _is_resolved_column(column_side, type_name_ids, "type_name"):
                continue
            value = _literal(literal_side)
            if value:
                values.add(value)
                proven_columns.add(id(column_side))
    for node in tree.find_all(exp.In):
        if not _is_resolved_column(node.this, type_name_ids, "type_name"):
            continue
        literals = [_literal(item) for item in node.expressions]
        if literals and all(literals):
            values.update(str(item) for item in literals)
            proven_columns.add(id(node.this))
    unprovable = tuple(
        dict.fromkeys(
            column.parent.sql(dialect=_DIALECT)
            for column in tree.find_all(exp.Column)
            if (
                id(column) in ambiguous_ids
                or (_is_resolved_column(column, type_name_ids, "type_name") and id(column) not in proven_columns)
            )
        )
    )
    return values, unprovable


def extract_eav_type_names(sql: str) -> set[str]:
    """Extract literal values used by ``type_name =`` and ``type_name IN``.

    Dynamic expressions are intentionally not treated as evidence-backed
    literals.  Callers can fail closed when a generated query needs a shape
    that cannot be proven by this extractor.
    """

    values, _unprovable = _analyze_eav_usage(sql)
    return values


def eav_concept_key(value: str) -> str:
    """Stable comparison key for explicit binding validation and diagnostics."""

    normalized = _UNIT_SUFFIX.sub("", str(value or "").strip())
    return _NON_WORD.sub("", normalized).lower()


def eav_names_related(left: str, right: str) -> bool:
    """Conservative relevance check for a physical-name repair candidate.

    This is not semantic equivalence.  It only prevents an unrelated schema
    row (for example an engine field) from authorizing a CLTC repair.  Actual
    multi-field equivalence still requires an explicit semantic binding.
    """

    left_key = eav_concept_key(left)
    right_key = eav_concept_key(right)
    if not left_key or not right_key:
        return False
    if left_key in right_key or right_key in left_key:
        return True
    left_latin = {item.lower() for item in re.findall(r"[A-Za-z0-9+]{3,}", left)}
    right_latin = {item.lower() for item in re.findall(r"[A-Za-z0-9+]{3,}", right)}
    if left_latin & right_latin:
        return True
    ignored_words = ("配置项", "字段名", "类型", "名称", "数值", "参数", "信息")
    ignored = {
        word[index : index + 2]
        for word in ignored_words
        for index in range(max(0, len(word) - 1))
    }
    left_cn = "".join(re.findall(r"[\u4e00-\u9fff]", left_key))
    right_cn = "".join(re.findall(r"[\u4e00-\u9fff]", right_key))
    left_pairs = {left_cn[index : index + 2] for index in range(max(0, len(left_cn) - 1))}
    right_pairs = {right_cn[index : index + 2] for index in range(max(0, len(right_cn) - 1))}
    return bool((left_pairs & right_pairs) - ignored)


def bindings_from_semantic_trace(
    trace: dict[str, Any],
    *,
    question: str = "",
) -> list[EavEquivalenceBinding]:
    """Load machine-readable EAV equivalence contracts from matched assets."""

    bindings: list[EavEquivalenceBinding] = []
    # Model references may contain every available dimension.  Only assets
    # actually matched/selected for this question may impose a required field.
    assets = [item for item in trace.get("matched", []) if isinstance(item, dict)]
    for asset in assets:
        frontmatter = asset.get("frontmatter") if isinstance(asset.get("frontmatter"), dict) else {}
        asset_aliases = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in [
                    frontmatter.get("name") or asset.get("name"),
                    *(frontmatter.get("aliases") or []),
                ]
                if str(item).strip()
            )
        )
        if question and str(trace.get("resolution_mode") or "") in {"fuzzy", "model_scoped_fuzzy"}:
            query_key = eav_concept_key(question)
            terms = [
                frontmatter.get("name") or asset.get("name"),
                *(frontmatter.get("aliases") or []),
            ]
            term_keys = [eav_concept_key(str(term)) for term in terms if str(term).strip()]
            if not any(
                len(term_key) >= 2 and (term_key in query_key or query_key in term_key)
                for term_key in term_keys
            ):
                continue
        resolution = frontmatter.get("resolution") if isinstance(frontmatter.get("resolution"), dict) else {}
        raw_bindings = resolution.get("eav_equivalence") or frontmatter.get("eav_equivalence") or []
        if isinstance(raw_bindings, dict):
            raw_bindings = [raw_bindings]
        equivalence_names: set[str] = set()
        for item in raw_bindings if isinstance(raw_bindings, list) else []:
            if not isinstance(item, dict):
                continue
            names = tuple(
                dict.fromkeys(
                    str(name).strip()
                    for name in item.get("type_names", [])
                    if str(name).strip()
                )
            )
            if not names:
                continue
            equivalence_names.update(names)
            bindings.append(
                EavEquivalenceBinding(
                    concept=str(item.get("concept") or asset.get("id") or asset.get("name") or "eav"),
                    type_names=names,
                    match=str(item.get("match") or "any"),
                    value_resolution=str(item.get("value_resolution") or "coalesce_by_priority"),
                    aliases=asset_aliases,
                )
            )
        for item in resolution.get("bindings", []) if isinstance(resolution.get("bindings"), list) else []:
            if not isinstance(item, dict):
                continue
            fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
            exact_name = str(fields.get("type_name") or "").strip()
            if exact_name and exact_name not in equivalence_names:
                bindings.append(
                    EavEquivalenceBinding(
                        concept=str(asset.get("id") or asset.get("name") or exact_name),
                        type_names=(exact_name,),
                        match="exact",
                        value_resolution="exact",
                        aliases=asset_aliases,
                    )
                )
    return bindings


def _subtree_type_names(node: exp.Expression, type_name_ids: set[int]) -> set[str]:
    names: set[str] = set()
    for eq in node.find_all(exp.EQ):
        for column_side, literal_side in ((eq.this, eq.expression), (eq.expression, eq.this)):
            if _is_resolved_column(column_side, type_name_ids, "type_name"):
                value = _literal(literal_side)
                if value:
                    names.add(value)
    return names


def _binding_resolution_is_valid(sql: str, binding: EavEquivalenceBinding) -> bool:
    if binding.value_resolution in {"exact", "match_any"}:
        return True
    tree = sqlglot.parse_one(sql, read=_DIALECT)
    type_name_ids, _ambiguous, reachable_scopes = _resolved_eav_column_ids(tree, "type_name")
    type_value_ids, _ambiguous_values, _ = _resolved_eav_column_ids(tree, "type_value")
    reachable_expression_ids = {id(scope.expression) for scope in reachable_scopes}

    def is_reachable_projection(node: exp.Expression) -> bool:
        select = node.find_ancestor(exp.Select)
        if select is None or id(select) not in reachable_expression_ids:
            return False
        current = node
        while current.parent is not None and current.parent is not select:
            current = current.parent
        return current in select.expressions

    def argument_resolves_name(argument: exp.Expression, name: str) -> bool:
        current = argument
        while isinstance(current, (exp.Cast, exp.TryCast, exp.Paren)):
            current = current.this
        if not isinstance(current, exp.Max):
            return False
        max_value = current.this
        while isinstance(max_value, (exp.Cast, exp.TryCast, exp.Paren)):
            max_value = max_value.this
        if not isinstance(max_value, exp.Case):
            return False
        branches = max_value.args.get("ifs") or []
        if len(branches) != 1 or _subtree_type_names(argument, type_name_ids) != {name}:
            return False
        branch = branches[0]
        condition = branch.this
        true_value = branch.args.get("true")
        while isinstance(condition, exp.Paren):
            condition = condition.this
        if not isinstance(condition, exp.EQ):
            return False
        condition_matches = any(
            _is_resolved_column(column_side, type_name_ids, "type_name")
            and _literal(literal_side) == name
            for column_side, literal_side in (
                (condition.this, condition.expression),
                (condition.expression, condition.this),
            )
        )
        if not condition_matches:
            return False
        value_columns = (
            list(true_value.find_all(exp.Column))
            if isinstance(true_value, exp.Expression)
            else []
        )
        arithmetic_types = (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Pow)
        if not value_columns or not all(id(column) in type_value_ids for column in value_columns):
            return False
        if any(true_value.find(kind) is not None for kind in arithmetic_types):
            return False
        default_value = max_value.args.get("default")
        if default_value is not None and not isinstance(default_value, exp.Null):
            return False
        return True

    if binding.value_resolution == "coalesce_by_priority":
        for coalesce in tree.find_all(exp.Coalesce):
            arguments = [coalesce.this, *(coalesce.expressions or [])]
            if any(argument.find(exp.Sum) is not None for argument in arguments):
                continue
            if not is_reachable_projection(coalesce):
                continue
            if len(arguments) == len(binding.type_names) and all(
                argument_resolves_name(argument, name)
                for argument, name in zip(arguments, binding.type_names, strict=True)
            ):
                return True
        return False
    return False


def check_eav_evidence(
    sql: str,
    *,
    live_type_names: Iterable[str],
    bindings: Iterable[EavEquivalenceBinding] = (),
) -> EavEvidenceCheck:
    """Require live existence and completeness for explicitly bound concepts."""

    raw_used, unprovable = _analyze_eav_usage(sql)
    used = frozenset(raw_used)
    live = {str(item) for item in live_type_names}
    unsupported = frozenset(item for item in used if item not in live)
    incomplete: list[EavEquivalenceBinding] = []
    invalid_resolutions: list[EavEquivalenceBinding] = []
    for binding in bindings:
        members = set(binding.type_names)
        if not members.issubset(used):
            incomplete.append(binding)
        elif not _binding_resolution_is_valid(sql, binding):
            invalid_resolutions.append(binding)
    return EavEvidenceCheck(
        used_type_names=used,
        unsupported=unsupported,
        incomplete_bindings=tuple(incomplete),
        invalid_binding_resolutions=tuple(invalid_resolutions),
        unprovable_predicates=unprovable,
    )


def eav_mapping_fingerprint(
    sql: str,
    *,
    bindings: Iterable[EavEquivalenceBinding] = (),
    replacement_groups: Iterable[Iterable[str]] = (),
) -> str:
    """Hash SQL structure after erasing only EAV physical-name choices.

    This lets an automatic evidence repair prove that it did not change joins,
    filters, grouping, projections, or any other business-relevant structure.
    """

    tree = sqlglot.parse_one(sql, read=_DIALECT).copy()
    type_name_ids, _ambiguous, _scopes = _resolved_eav_column_ids(tree, "type_name")
    token_by_name: dict[str, str] = {}
    for index, binding in enumerate(bindings):
        token = f"__EAV_CONCEPT_{index}_{eav_concept_key(binding.concept)}__"
        token_by_name.update({name: token for name in binding.type_names})
    for index, group in enumerate(replacement_groups):
        names = tuple(dict.fromkeys(str(item) for item in group if str(item)))
        if len(names) < 2:
            continue
        token = next(
            (token_by_name[name] for name in names if name in token_by_name),
            f"__EAV_REPAIR_{index}__",
        )
        for name in names:
            token_by_name[name] = token

    def mapped_literal(node: Any) -> exp.Literal | None:
        value = _literal(node)
        token = token_by_name.get(value or "")
        return exp.Literal.string(token) if token else None

    for node in tree.find_all(exp.EQ):
        if _is_resolved_column(node.this, type_name_ids, "type_name") and mapped_literal(node.expression) is not None:
            node.set("expression", mapped_literal(node.expression))
        elif _is_resolved_column(node.expression, type_name_ids, "type_name") and mapped_literal(node.this) is not None:
            node.set("this", mapped_literal(node.this))
    for node in tree.find_all(exp.In):
        if _is_resolved_column(node.this, type_name_ids, "type_name"):
            tokens = {
                token_by_name.get(value or "")
                for value in (_literal(item) for item in node.expressions)
            }
            if len(tokens) == 1 and None not in tokens:
                node.replace(
                    exp.EQ(
                        this=node.this.copy(),
                        expression=exp.Literal.string(next(iter(tokens))),
                    )
                )

    def collapse_equivalent_coalesce(node: exp.Expression) -> exp.Expression:
        if not isinstance(node, exp.Coalesce):
            return node
        arguments = [node.this, *(node.expressions or [])]
        rendered = [item.sql(dialect=_DIALECT, pretty=False) for item in arguments]
        if rendered and len(set(rendered)) == 1:
            return arguments[0].copy()
        return node

    tree = tree.transform(collapse_equivalent_coalesce)
    payload = tree.sql(dialect=_DIALECT, pretty=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sql_business_fingerprint(sql: str) -> str:
    """Hash business invariants while allowing internal JOIN/CTE rewrites.

    This is intentionally conservative: automatic technical repair may change
    implementation structure, but not output metrics, filter values, grouping
    grain, DISTINCT semantics, ordering/limit or set-operation shape.
    """

    tree = sqlglot.parse_one(sql, read=_DIALECT)
    scopes = _reachable_scopes(tree)
    expressions = [scope.expression for scope in scopes]

    def canonical(node: exp.Expression | None) -> str:
        if node is None:
            return ""
        return node.copy().sql(dialect=_DIALECT, pretty=False).lower()

    def scope_depth(scope: Scope) -> int:
        depth = 0
        current = scope.parent
        while current is not None:
            depth += 1
            current = current.parent
        return depth

    root = build_scope(tree)
    scope_paths: dict[int, str] = {}

    def map_scope_paths(scope: Scope, path: str) -> None:
        if id(scope.expression) in scope_paths:
            return
        scope_paths[id(scope.expression)] = path
        for index, branch in enumerate(scope.union_scopes):
            map_scope_paths(branch, f"{path}/union:{index}")
        for index, branch in enumerate(scope.subquery_scopes):
            map_scope_paths(branch, f"{path}/subquery:{index}")
        for index, (_alias, (_node, source)) in enumerate(scope.selected_sources.items()):
            if isinstance(source, Scope):
                map_scope_paths(source, f"{path}/source:{index}")

    if root is not None:
        map_scope_paths(root, "root")

    def physical_sources(source: Any, seen: set[int] | None = None) -> set[str]:
        if isinstance(source, exp.Table):
            catalog = str(source.catalog or "").lower()
            db = str(source.db or "").lower()
            name = source.name.lower()
            return {".".join(part for part in (catalog, db, name) if part)}
        if not isinstance(source, Scope):
            return set()
        visited = set(seen or ())
        if id(source) in visited:
            return set()
        visited.add(id(source))
        result: set[str] = set()
        for _alias, (_node, selected_source) in source.selected_sources.items():
            result.update(physical_sources(selected_source, visited))
        for branch in [*source.union_scopes, *source.subquery_scopes]:
            result.update(physical_sources(branch, visited))
        return result

    def scope_source_signature(scope: Scope) -> tuple[str, ...]:
        result: set[str] = set()
        for _alias, (_node, source) in scope.selected_sources.items():
            result.update(physical_sources(source))
        return tuple(sorted(result))

    root_scopes: list[Scope] = []
    if root is not None:
        root_scopes = root.union_scopes or [root]
    root_select_scopes = [
        scope for scope in root_scopes if isinstance(scope.expression, exp.Select)
    ]
    root_selects = [scope.expression for scope in root_select_scopes]
    projections = []
    for scope in root_select_scopes:
        select = scope.expression
        for item in select.expressions:
            projections.append(
                {
                    "scope_path": scope_paths.get(id(select), "root"),
                    "sources": scope_source_signature(scope),
                    "alias": str(item.alias_or_name or "").lower(),
                    "expression": canonical(item.this if isinstance(item, exp.Alias) else item),
                    "windowed": item.find(exp.Window) is not None,
                }
            )

    predicates: set[tuple[str, tuple[str, ...], str, str]] = set()
    group_expressions: set[tuple[str, tuple[str, ...], str]] = set()
    joins: set[tuple[str, tuple[str, ...], str, str, str, str]] = set()
    for scope in scopes:
        expression = scope.expression
        depth = scope_depth(scope)
        path = scope_paths.get(id(expression), f"depth:{depth}")
        sources = scope_source_signature(scope)
        for key in ("where", "having"):
            predicate = expression.args.get(key)
            if isinstance(predicate, exp.Expression):
                predicates.add((path, sources, key, canonical(predicate.this)))
        group = expression.args.get("group")
        if isinstance(group, exp.Group):
            group_expressions.update(
                (path, sources, canonical(group_expression))
                for group_expression in group.expressions
            )
        for join in expression.args.get("joins") or []:
            joins.add(
                (
                    path,
                    sources,
                    str(join.side or "").lower(),
                    str(join.kind or "").lower(),
                    canonical(join.this),
                    canonical(join.args.get("on")),
                )
            )
    order_expressions = {
        (
            scope_paths.get(id(scope.expression), "root"),
            scope_source_signature(scope),
            canonical(order_expression),
        )
        for scope in root_select_scopes
        for select in [scope.expression]
        for order in select.find_all(exp.Order)
        for order_expression in order.expressions
    }
    distinct_nodes = {
        id(node) for expression in expressions for node in expression.find_all(exp.Distinct)
    }
    payload = {
        "projections": projections,
        "predicates": sorted(predicates),
        "group_expressions": sorted(group_expressions),
        "order_expressions": sorted(order_expressions),
        "joins": sorted(joins),
        "distinct_count": len(distinct_nodes),
        "limits": sorted(
            str(literal.this)
            for select in root_selects
            for limit in select.find_all(exp.Limit)
            for literal in limit.find_all(exp.Literal)
        ),
        "offsets": sorted(
            str(literal.this)
            for select in root_selects
            for offset in select.find_all(exp.Offset)
            for literal in offset.find_all(exp.Literal)
        ),
        "set_operations": sorted(
            (
                type(node).__name__.lower(),
                bool(node.args.get("distinct", True)),
            )
            for node in tree.find_all(exp.SetOperation)
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def bindings_prompt(bindings: Iterable[EavEquivalenceBinding]) -> str:
    """Serialize trusted bindings as data, not executable prompt prose."""

    return json.dumps(
        [
            {
                "concept": item.concept,
                "type_names": list(item.type_names),
                "match": item.match,
                "value_resolution": item.value_resolution,
            }
            for item in bindings
        ],
        ensure_ascii=False,
        indent=2,
    )
