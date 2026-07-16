"""Deterministic Tool execution policy and middleware.

The pipeline is the single pre-execution control point for Agent Tool calls.
It does not treat a Docker container as authorization: policy is evaluated
before both Docker and restricted-host execution.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command, interrupt

from graph.permission_resume import permission_resume_registry
from graph.session_manager import session_manager


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class ToolPolicyResult:
    decision: PolicyDecision
    reason: str
    risk: str


_HARD_DENY_COMMANDS = frozenset(
    {
        "sudo",
        "su",
        "doas",
        "pkexec",
        "mount",
        "umount",
        "chroot",
        "docker",
        "podman",
        "nerdctl",
        "systemctl",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
    }
)
_NETWORK_COMMANDS = frozenset(
    {
        "curl",
        "wget",
        "ssh",
        "scp",
        "sftp",
        "ftp",
        "nc",
        "ncat",
        "telnet",
        "ping",
    }
)
_PACKAGE_COMMANDS = frozenset(
    {
        "apt",
        "apt-get",
        "apk",
        "dnf",
        "yum",
        "brew",
        "pip",
        "pip3",
        "poetry",
        "uv",
        "conda",
    }
)
_DESTRUCTIVE_OR_WRITE_COMMANDS = frozenset(
    {
        "rm",
        "rmdir",
        "mv",
        "cp",
        "touch",
        "mkdir",
        "chmod",
        "chown",
        "truncate",
        "tee",
        "dd",
        "install",
    }
)
_SAFE_READ_COMMANDS = frozenset(
    {
        "pwd",
        "ls",
        "cat",
        "head",
        "tail",
        "grep",
        "rg",
        "find",
        "stat",
        "file",
        "wc",
        "du",
        "df",
        "which",
        "whereis",
        "printf",
        "echo",
        "sort",
        "uniq",
        "cut",
        "tr",
        "jq",
    }
)
_WRAPPERS = frozenset({"command", "env", "timeout", "gtimeout", "nice", "nohup"})
_SHELLS = frozenset({"sh", "bash", "zsh"})
_SHELL_META_PATTERN = re.compile(r"(`|\$\(|\$\{|\n|<<)")
_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![\w.-])(/[^\s;&|<>\"']+)")


class ShellPolicyAnalyzer:
    """Conservative shell analyzer: unknown or ambiguous syntax requires HITL."""

    def __init__(self, *, workspace_path: str, backend_mode: str) -> None:
        self.workspace_path = Path(workspace_path).expanduser().resolve()
        self.backend_mode = backend_mode

    def analyze(self, command: str) -> ToolPolicyResult:
        if not isinstance(command, str) or not command.strip():
            return ToolPolicyResult(PolicyDecision.DENY, "empty_command", "invalid")
        if _SHELL_META_PATTERN.search(command):
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "complex_shell_expansion",
                "high",
            )
        path_result = self._check_absolute_paths(command)
        if path_result is not None:
            return path_result
        try:
            segments, has_redirect = self._segments(command)
        except ValueError:
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "shell_parse_failed",
                "high",
            )
        if not segments:
            return ToolPolicyResult(PolicyDecision.DENY, "empty_command", "invalid")
        relative_path_result = self._check_relative_paths(segments)
        if relative_path_result is not None:
            return relative_path_result
        decisions = [self._analyze_segment(segment) for segment in segments]
        if has_redirect:
            decisions.append(
                ToolPolicyResult(
                    PolicyDecision.ASK,
                    "shell_redirection",
                    "managed_write",
                )
            )
        return self._strictest(decisions)

    def _check_absolute_paths(self, command: str) -> ToolPolicyResult | None:
        if self.backend_mode != "restricted_host":
            return None
        without_urls = re.sub(
            r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s;&|<>\"']+",
            "",
            command,
        )
        for matched in _ABSOLUTE_PATH_PATTERN.findall(without_urls):
            raw = matched.rstrip("),]")
            if raw == "/workspace" or raw.startswith("/workspace/"):
                continue
            try:
                resolved = Path(raw).expanduser().resolve()
                resolved.relative_to(self.workspace_path)
            except (OSError, ValueError):
                return ToolPolicyResult(
                    PolicyDecision.DENY,
                    "host_filesystem_access",
                    "critical",
                )
        return None

    def _check_relative_paths(
        self,
        segments: list[list[str]],
    ) -> ToolPolicyResult | None:
        if self.backend_mode != "restricted_host":
            return None
        for tokens in segments:
            for index, token in enumerate(tokens):
                raw = token.strip().rstrip("),]")
                if not raw or "://" in raw:
                    continue
                if index == 0 and "/" not in raw:
                    continue
                if raw.startswith("-"):
                    if "=" not in raw:
                        continue
                    raw = raw.split("=", 1)[1].strip()
                    if not raw:
                        continue
                if raw == "/workspace":
                    candidate = self.workspace_path
                elif raw.startswith("/workspace/"):
                    candidate = self.workspace_path / raw.removeprefix("/workspace/")
                elif raw.startswith("~/"):
                    candidate = Path(raw).expanduser()
                elif Path(raw).is_absolute():
                    candidate = Path(raw)
                else:
                    candidate = self.workspace_path / raw
                try:
                    candidate.resolve().relative_to(self.workspace_path)
                except (OSError, ValueError):
                    # Resolve every argument, not only explicit "../" paths:
                    # an in-workspace symlink can otherwise redirect reads or
                    # writes to the host filesystem. ``Path.resolve`` also
                    # resolves an existing parent symlink for a not-yet-created
                    # output path.
                    return ToolPolicyResult(
                        PolicyDecision.DENY,
                        "host_filesystem_access",
                        "critical",
                    )
        return None

    @staticmethod
    def _segments(command: str) -> tuple[list[list[str]], bool]:
        lexer = shlex.shlex(
            command,
            posix=True,
            punctuation_chars="|&;<>",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
        segments: list[list[str]] = []
        current: list[str] = []
        has_redirect = False
        for token in tokens:
            if token in {"&&", "||", ";", "|", "&"}:
                if current:
                    segments.append(current)
                    current = []
                continue
            if token in {">", ">>", "<", "2>", "2>>", "&>"} or set(token) <= {
                ">",
                "<",
            }:
                has_redirect = True
                continue
            current.append(token)
        if current:
            segments.append(current)
        return segments, has_redirect

    def _analyze_segment(self, tokens: list[str]) -> ToolPolicyResult:
        tokens = self._unwrap(tokens)
        if not tokens:
            return ToolPolicyResult(PolicyDecision.ASK, "wrapper_without_command", "high")
        command = Path(tokens[0]).name.lower()
        args = tokens[1:]

        if command in _HARD_DENY_COMMANDS:
            return ToolPolicyResult(
                PolicyDecision.DENY,
                f"hard_denied_command:{command}",
                "critical",
            )
        if command in _SHELLS and len(args) >= 2 and args[0] in {"-c", "-lc"}:
            return self.analyze(args[1])
        if command in _NETWORK_COMMANDS:
            return ToolPolicyResult(
                PolicyDecision.ASK,
                f"network_access:{command}",
                "network",
            )
        if command in _PACKAGE_COMMANDS:
            if command in {"pip", "pip3"} and args[:1] not in (["install"], ["uninstall"]):
                return ToolPolicyResult(PolicyDecision.ASK, f"python_tool:{command}", "high")
            return ToolPolicyResult(
                PolicyDecision.ASK,
                f"package_management:{command}",
                "package_install",
            )
        if command in {"python", "python3", "node", "ruby", "perl", "php"}:
            if command.startswith("python") and args[:2] == ["-m", "pytest"]:
                return ToolPolicyResult(PolicyDecision.ALLOW, "project_test", "low")
            return ToolPolicyResult(
                PolicyDecision.ASK,
                f"arbitrary_interpreter:{command}",
                "high",
            )
        if command in _DESTRUCTIVE_OR_WRITE_COMMANDS:
            return ToolPolicyResult(
                PolicyDecision.ASK,
                f"managed_workspace_write:{command}",
                "managed_write",
            )
        if command == "find":
            execution_flags = {
                "-delete",
                "-exec",
                "-execdir",
                "-ok",
                "-okdir",
                "-fprint",
                "-fprint0",
                "-fprintf",
            }
            matched_flag = next(
                (arg for arg in args if arg.lower() in execution_flags),
                None,
            )
            if matched_flag:
                if matched_flag in {"-exec", "-execdir", "-ok", "-okdir"}:
                    nested = args[args.index(matched_flag) + 1 :]
                    nested_command = next(
                        (Path(arg).name.lower() for arg in nested if arg not in {"{}", ";", "+"}),
                        "",
                    )
                    if nested_command in _HARD_DENY_COMMANDS:
                        return ToolPolicyResult(
                            PolicyDecision.DENY,
                            f"hard_denied_command:{nested_command}",
                            "critical",
                        )
                return ToolPolicyResult(
                    PolicyDecision.ASK,
                    f"managed_workspace_write:find:{matched_flag}",
                    "managed_write",
                )
        if command == "rg" and any(
            arg == "--pre" or arg.startswith("--pre=") for arg in args
        ):
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "external_command_hook:rg",
                "high",
            )
        if command == "git":
            if any(arg == "--ext-diff" for arg in args) or any(
                arg == "-c"
                and index + 1 < len(args)
                and (
                    ".external=" in args[index + 1].lower()
                    or "pager." in args[index + 1].lower()
                )
                for index, arg in enumerate(args)
            ):
                return ToolPolicyResult(
                    PolicyDecision.ASK,
                    "external_command_hook:git",
                    "high",
                )
        if command == "sed" and any(arg == "-i" or arg.startswith("-i") for arg in args):
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "managed_workspace_write:sed",
                "managed_write",
            )
        if command == "git":
            subcommand = next((arg for arg in args if not arg.startswith("-")), "")
            if subcommand in {"status", "diff", "log", "show", "branch", "rev-parse"}:
                return ToolPolicyResult(PolicyDecision.ALLOW, "safe_git_read", "low")
            if subcommand in {"push", "pull", "fetch", "clone"}:
                return ToolPolicyResult(PolicyDecision.ASK, "git_network", "network")
            return ToolPolicyResult(PolicyDecision.ASK, "managed_git_write", "high")
        if command in {"pytest", "ruff", "mypy", "pyright"}:
            return ToolPolicyResult(PolicyDecision.ALLOW, "project_test", "low")
        if command in {"npm", "pnpm", "yarn"}:
            normalized = [arg.lower() for arg in args]
            if normalized[:1] in (["test"], ["build"], ["lint"]) or (
                normalized[:1] == ["run"]
                and normalized[1:2] in (["test"], ["build"], ["lint"], ["check"])
            ):
                return ToolPolicyResult(PolicyDecision.ALLOW, "project_test", "low")
            if normalized[:1] in (["install"], ["add"], ["remove"], ["uninstall"]):
                return ToolPolicyResult(PolicyDecision.ASK, "package_management", "package_install")
            return ToolPolicyResult(PolicyDecision.ASK, "node_command", "high")
        if command == "flutter" and args[:1] in (["test"], ["analyze"]):
            return ToolPolicyResult(PolicyDecision.ALLOW, "project_test", "low")
        if command in _SAFE_READ_COMMANDS:
            if command == "sort" and any(
                arg == "-o" or arg.startswith("--output") for arg in args
            ):
                return ToolPolicyResult(
                    PolicyDecision.ASK,
                    "managed_workspace_write:sort",
                    "managed_write",
                )
            if command == "uniq":
                positional = [arg for arg in args if not arg.startswith("-")]
                if len(positional) > 1:
                    return ToolPolicyResult(
                        PolicyDecision.ASK,
                        "managed_workspace_write:uniq",
                        "managed_write",
                    )
            return ToolPolicyResult(PolicyDecision.ALLOW, "safe_read", "low")
        if command == "cd":
            return ToolPolicyResult(PolicyDecision.ALLOW, "workspace_navigation", "low")
        return ToolPolicyResult(
            PolicyDecision.ASK,
            f"unknown_command:{command}",
            "high",
        )

    @staticmethod
    def _unwrap(tokens: list[str]) -> list[str]:
        remaining = list(tokens)
        while remaining and Path(remaining[0]).name.lower() in _WRAPPERS:
            wrapper = Path(remaining.pop(0)).name.lower()
            while remaining and remaining[0].startswith("-"):
                remaining.pop(0)
            if wrapper == "env":
                while remaining and "=" in remaining[0] and not remaining[0].startswith("="):
                    remaining.pop(0)
        return remaining

    @staticmethod
    def _strictest(results: list[ToolPolicyResult]) -> ToolPolicyResult:
        rank = {
            PolicyDecision.ALLOW: 0,
            PolicyDecision.ASK: 1,
            PolicyDecision.DENY: 2,
        }
        return max(results, key=lambda result: rank[result.decision])


class ToolExecutionPipeline(AgentMiddleware):
    """Fail-closed Tool preflight with deterministic allow/ask/deny outcomes."""

    BUILTIN_TOOLS = frozenset(
        {
            "write_todos",
            "ls",
            "read_file",
            "write_file",
            "edit_file",
            "glob",
            "grep",
            "execute",
            "task",
        }
    )
    DECLARED_ALLOW_TOOLS = frozenset(
        {
            "write_todos",
            "ls",
            "read_file",
            "write_file",
            "edit_file",
            "glob",
            "grep",
            "task",
            "read_resource",
            "llamaindex_knowledge_query",
            "pandas_knowledge_query",
            "database_schema_inspect",
            "database_sql_generate",
            "database_sql_validate",
            "database_sql_execute",
            "database_query_trace_inspect",
            "database_query_result_page",
            "semantic_entity_lookup",
            "inspect_dimension_build_input",
            "request_dimension_build_rule",
            "enqueue_semantic_dimension_build",
            "get_semantic_dimension_build_job",
            "publish_semantic_dimension_build",
            "ensure_attachment_table_asset",
            "list_logical_dataset_candidates",
            "request_logical_dataset_rule",
            "apply_logical_dataset_rule",
        }
    )
    NETWORK_TOOLS = frozenset({"tavily_search", "fetch_url"})

    def __init__(
        self,
        *,
        known_tools: set[str],
        backend_mode: str,
    ) -> None:
        self.known_tools = set(known_tools) | set(self.BUILTIN_TOOLS)
        self.backend_mode = backend_mode

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        result = self._preflight(request)
        if result.decision == PolicyDecision.ALLOW:
            return await handler(request)
        if result.decision == PolicyDecision.DENY:
            return self._denied_message(request, result)

        context = self._context(request)
        session_id = str(context.get("session_id") or "")
        query_id = str(context.get("query_id") or "")
        command = self._action_preview(request)
        tool_name = str(request.tool_call.get("name") or "")
        fingerprint = permission_resume_registry.tool_action_fingerprint(
            tool_name=tool_name,
            command=command,
            reason=result.reason,
        )
        if session_manager.consume_tool_action_permission(session_id, fingerprint):
            return await handler(request)
        preview = permission_resume_registry.create_tool_action_request(
            session_id=session_id,
            query_id=query_id,
            tool_call_id=str(request.tool_call.get("id") or ""),
            tool_name=tool_name,
            command=command,
            reason=result.reason,
            risk=result.risk,
        )
        interrupt(
            {
                "type": "permission_request",
                "request": preview,
                "decisions": [{"type": "approve"}, {"type": "reject"}],
            }
        )
        return self._denied_message(request, result)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        result = self._preflight(request)
        if result.decision == PolicyDecision.ALLOW:
            return handler(request)
        return self._denied_message(request, result)

    def _preflight(self, request: ToolCallRequest) -> ToolPolicyResult:
        tool_name = str(request.tool_call.get("name") or "")
        if tool_name not in self.known_tools:
            return ToolPolicyResult(
                PolicyDecision.DENY,
                f"unknown_tool:{tool_name}",
                "critical",
            )
        if tool_name in self.DECLARED_ALLOW_TOOLS:
            return ToolPolicyResult(
                PolicyDecision.ALLOW,
                "declared_tool_policy",
                "declared",
            )
        if tool_name in self.NETWORK_TOOLS:
            return ToolPolicyResult(
                PolicyDecision.ASK,
                f"network_access:{tool_name}",
                "network",
            )
        if tool_name != "execute":
            return ToolPolicyResult(
                PolicyDecision.DENY,
                f"unclassified_tool:{tool_name}",
                "critical",
            )
        context = self._context(request)
        analyzer = ShellPolicyAnalyzer(
            workspace_path=str(context.get("workspace_path") or "."),
            backend_mode=self.backend_mode,
        )
        return analyzer.analyze(self._command(request))

    @staticmethod
    def _context(request: ToolCallRequest) -> dict[str, Any]:
        runtime = request.runtime
        context = runtime.context if runtime is not None else None
        return context if isinstance(context, dict) else {}

    @staticmethod
    def _command(request: ToolCallRequest) -> str:
        args = request.tool_call.get("args") or {}
        return str(args.get("command") or "")

    @classmethod
    def _action_preview(cls, request: ToolCallRequest) -> str:
        if str(request.tool_call.get("name") or "") == "execute":
            return cls._command(request)
        args = request.tool_call.get("args") or {}
        try:
            rendered = json.dumps(args, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            rendered = str(args)
        return rendered[:4000]

    @staticmethod
    def _denied_message(
        request: ToolCallRequest,
        result: ToolPolicyResult,
    ) -> ToolMessage:
        tool_name = str(request.tool_call.get("name") or "")
        return ToolMessage(
            content=(
                f"Tool `{tool_name}` was blocked by Harness policy: "
                f"{result.reason} ({result.risk})."
            ),
            tool_call_id=str(request.tool_call.get("id") or ""),
            name=tool_name,
            status="error",
        )
