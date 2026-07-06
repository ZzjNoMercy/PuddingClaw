"""Runner abstractions for executing generated pandas analysis code.

The first implementation still runs in-process, but the interface is explicit
so a future Docker/subprocess sandbox can replace it without changing the query
engine or agent tool contract.
"""

from __future__ import annotations

import ast
import builtins
import math
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import PandasQueryEngineError


_BLOCKED_RAW_TOKENS = (
    "__",
)

_BLOCKED_ATTRS = {
    # pandas / numpy file and database IO
    "read_csv",
    "read_excel",
    "read_table",
    "read_fwf",
    "read_json",
    "read_html",
    "read_xml",
    "read_pickle",
    "read_parquet",
    "read_feather",
    "read_orc",
    "read_sas",
    "read_spss",
    "read_stata",
    "read_sql",
    "read_sql_query",
    "read_sql_table",
    "to_csv",
    "to_excel",
    "to_json",
    "to_html",
    "to_xml",
    "to_pickle",
    "to_parquet",
    "to_feather",
    "to_orc",
    "to_hdf",
    "to_sql",
    "to_stata",
    "to_latex",
    "to_markdown",
    "to_clipboard",
    # known side-effect or environment escape helpers
    "save",
    "savefig",
    "dump",
    "dumps",
    "load",
    "loads",
    "system",
    "popen",
    "remove",
    "unlink",
    "rmdir",
    "mkdir",
    "makedirs",
    "rename",
    "replace",
    "chmod",
    "chown",
    "chdir",
}

_ALLOWED_BUILTINS = {
    "len",
    "min",
    "max",
    "sum",
    "round",
    "abs",
    "sorted",
    "str",
    "int",
    "float",
    "bool",
    "list",
    "dict",
    "tuple",
    "set",
    "enumerate",
    "range",
}

_ALLOWED_ROOT_NAMES = {"df", "pd", "np", "math", "result"} | _ALLOWED_BUILTINS

_ALLOWED_NODES = (
    ast.Module,
    ast.Assign,
    ast.AnnAssign,
    ast.Expr,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.keyword,
    ast.Subscript,
    ast.Slice,
    ast.Attribute,
    ast.Call,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.JoinedStr,
    ast.FormattedValue,
    # operators
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Not,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
)


class PandasCodeRunner(Protocol):
    """Execute generated pandas code against a prepared DataFrame."""

    def run(self, df: Any, code: str) -> Any:
        """Return the value assigned to ``result`` by generated code."""


@dataclass
class InProcessPandasRunner:
    """In-process pandas runner with AST validation.

    This is a development/local runner. It deliberately blocks file/database IO,
    imports, dunder access, and common environment escape hatches. It is not a
    production-grade isolation boundary; Docker/subprocess runners should
    implement the same ``PandasCodeRunner`` protocol later.
    """

    max_code_chars: int = 8000

    def run(self, df: Any, code: str) -> Any:
        validate_pandas_code(code, max_code_chars=self.max_code_chars)

        try:
            import numpy as np
            import pandas as pd
        except ImportError as exc:
            raise PandasQueryEngineError(f"缺少表格分析依赖：{exc}") from exc

        local_vars: dict[str, Any] = {"df": df.copy(), "pd": pd, "np": np, "math": math, "result": None}
        safe_builtins = {name: getattr(builtins, name) for name in _ALLOWED_BUILTINS if hasattr(builtins, name)}
        exec(compile(code, "<puddingclaw_pandas_query_engine>", "exec"), {"__builtins__": safe_builtins}, local_vars)
        return local_vars.get("result")


def blocked_code_token(code: str) -> str | None:
    lowered = code.lower()
    for token in _BLOCKED_RAW_TOKENS:
        if token in lowered:
            return token.strip()
    return None


def validate_pandas_code(code: str, *, max_code_chars: int = 8000) -> None:
    if not code.strip():
        raise PandasQueryEngineError("生成的 pandas 代码为空。")
    if len(code) > max_code_chars:
        raise PandasQueryEngineError(f"生成的 pandas 代码过长：{len(code)} > {max_code_chars}")

    blocked = blocked_code_token(code)
    if blocked:
        raise PandasQueryEngineError(f"生成的 pandas 代码包含不允许的操作：{blocked}")

    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise PandasQueryEngineError(f"生成的 pandas 代码语法错误：{exc}") from exc

    assigned_names = {"result"}
    for stmt in tree.body:
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.Expr)):
            raise PandasQueryEngineError(f"生成的 pandas 代码包含不允许的语句：{type(stmt).__name__}")
        targets = []
        if isinstance(stmt, ast.Assign):
            targets = list(stmt.targets)
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
        for target in targets:
            for name in _target_names(target):
                if name.startswith("_"):
                    raise PandasQueryEngineError(f"生成的 pandas 代码包含不允许的变量名：{name}")
                assigned_names.add(name)

    allowed_names = _ALLOWED_ROOT_NAMES | assigned_names
    has_result_assignment = False
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise PandasQueryEngineError(f"生成的 pandas 代码包含不允许的语法：{type(node).__name__}")
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            raise PandasQueryEngineError(f"生成的 pandas 代码包含不允许的语法：{type(node).__name__}")
        if isinstance(node, ast.Name):
            if node.id.startswith("_"):
                raise PandasQueryEngineError(f"生成的 pandas 代码包含不允许的变量名：{node.id}")
            if isinstance(node.ctx, ast.Load) and node.id not in allowed_names:
                raise PandasQueryEngineError(f"生成的 pandas 代码包含未授权变量：{node.id}")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr in _BLOCKED_ATTRS:
                raise PandasQueryEngineError(f"生成的 pandas 代码包含不允许的方法/属性：{node.attr}")
        if isinstance(node, ast.Call):
            _validate_call(node)

    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            has_result_assignment = any(isinstance(target, ast.Name) and target.id == "result" for target in stmt.targets)
        elif isinstance(stmt, ast.AnnAssign):
            has_result_assignment = isinstance(stmt.target, ast.Name) and stmt.target.id == "result"
        if has_result_assignment:
            break
    if not has_result_assignment:
        raise PandasQueryEngineError("生成的 pandas 代码必须把最终结果赋值给 result。")


def _target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for item in target.elts:
            names.update(_target_names(item))
        return names
    raise PandasQueryEngineError(f"生成的 pandas 代码包含不允许的赋值目标：{type(target).__name__}")


def _validate_call(node: ast.Call) -> None:
    if isinstance(node.func, ast.Name):
        if node.func.id not in _ALLOWED_BUILTINS:
            raise PandasQueryEngineError(f"生成的 pandas 代码包含不允许的函数调用：{node.func.id}")
        return
    if isinstance(node.func, ast.Attribute):
        if node.func.attr.startswith("_") or node.func.attr in _BLOCKED_ATTRS:
            raise PandasQueryEngineError(f"生成的 pandas 代码包含不允许的方法调用：{node.func.attr}")
        return
    raise PandasQueryEngineError(f"生成的 pandas 代码包含不允许的调用：{type(node.func).__name__}")
