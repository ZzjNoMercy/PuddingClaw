"""Deterministic Tool execution policy and middleware.

The pipeline is the single pre-execution control point for Agent Tool calls.
It does not treat a Docker container as authorization: policy is evaluated
before both Docker and restricted-host execution.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest, hook_config
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command, interrupt

from graph.permission_policy import RunPermissionContext
from graph.permission_resume import permission_resume_registry
from graph.session_manager import session_manager
from graph.skill_plan_resume import skill_plan_resume_registry
from harness.permission_reviewer import PermissionReviewer
from runtime_identity.adapters import (
    ManagedCliRegistry,
    UnsupportedManagedCliCommand,
)
from tools.toolsets import tool_control_descriptor


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class ToolPolicyResult:
    decision: PolicyDecision
    reason: str
    risk: str
    source: str = "deterministic"
    explanation: str = ""


@dataclass(frozen=True)
class ShellCapabilities:
    """Capabilities implied by one shell command, independent of risk."""

    network: bool = False
    workspace_write: bool = False
    package_install: bool = False
    destructive: bool = False


@dataclass(frozen=True)
class NetworkIntent:
    """Network authority requested by a command, separate from connectivity.

    ``ShellCapabilities.network`` controls backend routing.  This richer value
    prevents a reusable approval for one transport surface from silently
    becoming authority for a different tool or destination.
    """

    required: bool = False
    origins: tuple[str, ...] = ()
    target_known: bool = False
    remote_effect: str = "none"
    transport_profile: str = "none"


@dataclass(frozen=True)
class ManagedNpxSkillsAdd:
    """Parsed standalone ``npx skills add`` call owned by Skill Manager."""

    source: str
    skill_names: tuple[str, ...] = ()
    yes: bool = False
    install_all: bool = False
    list_only: bool = False


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
        "pipenv",
        "npx",
        "uvx",
    }
)
_RECURSIVE_RM_FLAGS = frozenset({"-r", "-R", "-rf", "-fr", "-rF", "-Rf", "--recursive"})
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
_ENV_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.DOTALL)
_NETWORK_URL_PATTERN = re.compile(r"\b(?:https?|wss?)://", re.IGNORECASE)
_NON_MATERIAL_REDIRECT_SINKS = frozenset(
    {
        "-",
        "/dev/null",
        "/dev/stdout",
        "/dev/stderr",
        "/proc/self/fd/1",
        "/proc/self/fd/2",
    }
)
_EMBEDDED_NETWORK_API_PATTERN = re.compile(
    r"(?:urllib(?:\.request)?|urlopen\s*\(|requests\.|httpx\.|aiohttp|"
    r"socket\.|http\.client|fetch\s*\(|axios\.|https?\.request\s*\(|node:https?)",
    re.IGNORECASE,
)
_EMBEDDED_WRITE_API_PATTERN = re.compile(
    r"(?:open\s*\([^)]*(?:,\s*|mode\s*=\s*)['\"][wax+]|"
    r"urlretrieve\s*\(|\.write_(?:text|bytes)\s*\(|"
    r"(?:os|shutil)\.(?:remove|unlink|rename|replace|mkdir|makedirs|rmtree|copy|move)\s*\(|"
    r"(?:fs\.)?(?:writeFile|appendFile|unlink|rename|mkdir)\s*\()",
    re.IGNORECASE,
)
_KNOWN_NETWORK_SKILL_ENTRYPOINT_PATTERN = re.compile(
    r"(?:python3?|node)\s+/skills/(?:aihot|tavily-search)/[^\s\"']+",
    re.IGNORECASE,
)
_EMBEDDED_DESTRUCTIVE_API_PATTERN = re.compile(
    r"(?:\bos\.(?:remove|unlink|rmdir|removedirs)\s*\(|"
    r"\bshutil\.rmtree\s*\(|"
    r"\bPath\s*\([^)]*\)\.(?:unlink|rmdir)\s*\(|"
    r"(?:\bfs|require\s*\(\s*['\"](?:node:)?fs['\"]\s*\))"
    r"\.(?:rm|rmSync|rmdir|rmdirSync|unlink|unlinkSync)\s*\(|"
    r"\bDeno\.remove\s*\(|"
    r"\bsubprocess\.(?:run|call|Popen)\s*\([^\n]*(?:\brm\b|git\s+(?:reset|clean|checkout|restore))|"
    r"\bos\.system\s*\([^\n]*(?:\brm\b|git\s+(?:reset|clean|checkout|restore)))",
    re.IGNORECASE,
)
_OPAQUE_CRITICAL_ACTION_PATTERN = re.compile(
    r"(?:^|[;&|`(){}\s])(?:sudo|su|doas|pkexec|docker|podman|nerdctl|mount|umount|chroot)\b|"
    r"(?:^|[;&|`(){}\s])(?:rm|rmdir|truncate|dd)\b|"
    r"\bgit\s+(?:reset|clean|rebase)\b|"
    r"\b(?:chmod|chown)\b",
    re.IGNORECASE,
)
_SMART_GIT_WRITE_SUBCOMMANDS = frozenset({"add", "commit", "switch", "stash"})
_SMART_DOCKER_DESTRUCTIVE_REASONS = frozenset(
    {
        "destructive_workspace_delete:rm_recursive",
        "managed_workspace_write:rmdir",
        "managed_workspace_write:chmod",
        "managed_workspace_write:chown",
        "managed_workspace_write:truncate",
        "managed_workspace_write:dd",
        "managed_workspace_write:find:-delete",
    }
)


class ShellPolicyAnalyzer:
    """Conservative shell analyzer: unknown or ambiguous syntax requires HITL."""

    def __init__(self, *, workspace_path: str, backend_mode: str) -> None:
        self.workspace_path = Path(workspace_path).expanduser().resolve()
        self.backend_mode = backend_mode

    def analyze(self, command: str) -> ToolPolicyResult:
        if not isinstance(command, str) or not command.strip():
            return ToolPolicyResult(PolicyDecision.DENY, "empty_command", "invalid")
        try:
            segments, has_write_redirect = self._segments(command)
        except ValueError:
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "shell_parse_failed",
                "high",
            )
        if not segments:
            return ToolPolicyResult(PolicyDecision.DENY, "empty_command", "invalid")
        for segment in segments:
            tokens = self.unwrap_command(segment)
            if not tokens or Path(tokens[0]).name != "cp":
                continue
            operands = [token for token in tokens[1:] if not token.startswith("-")]
            if (
                len(operands) >= 2
                and operands[-1].startswith("/scratch/external-directories/")
                and any(
                    source == "/workspace" or source.startswith("/workspace/")
                    for source in operands[:-1]
                )
            ):
                return ToolPolicyResult(
                    PolicyDecision.DENY,
                    "external_draft_shadow_import",
                    "critical",
                )
        path_result = self._check_absolute_paths(segments)
        if path_result is not None:
            return path_result
        relative_path_result = self._check_relative_paths(
            segments,
            inline_program=bool(
                re.search(r"<<-?\s*['\"]?[A-Za-z_][A-Za-z0-9_]*", command)
                or re.search(
                    r"(?:^|[;&|]\s*)(?:[A-Za-z_][A-Za-z0-9_]*=[^\s]+\s+)*"
                    r"(?:python(?:\d+(?:\.\d+)*)?|node|ruby|perl)\s+"
                    r"(?:-[ce]\b|--eval(?:\s|=))",
                    command,
                )
            ),
        )
        if relative_path_result is not None:
            return relative_path_result
        # Inspect paths before handing opaque expansion to the smart reviewer.
        # Reviewer approval never gets a chance to bypass a deterministic path
        # escape or Harness-internal scratch boundary.
        if _SHELL_META_PATTERN.search(command):
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "complex_shell_expansion",
                "high",
            )
        decisions = [self._analyze_segment(segment) for segment in segments]
        if has_write_redirect:
            decisions.append(
                ToolPolicyResult(
                    PolicyDecision.ASK,
                    "shell_redirection",
                    "managed_write",
                )
            )
        return self._strictest(decisions)

    @classmethod
    def requires_network(cls, command: str) -> bool:
        """Return whether an already-authorized command needs network access."""

        return cls.capabilities(command).network

    @classmethod
    def parse_segments(cls, command: str) -> list[list[str]]:
        """Return shell command segments for other deterministic classifiers.

        Authorization, capability routing, and post-execution verification must
        interpret wrapper and environment syntax the same way.  Callers should
        pass each returned segment through :meth:`unwrap_command`.
        """

        segments, _ = cls._segments(command)
        return segments

    @classmethod
    def unwrap_command(cls, tokens: list[str]) -> list[str]:
        """Remove effect-neutral wrappers and leading environment assignments."""

        return cls._unwrap(tokens)

    @staticmethod
    def _lark_cli_requires_network(tokens: list[str]) -> bool:
        """Keep purely local CLI discovery offline; all real actions need net."""

        lowered = [item.lower() for item in tokens[1:]]
        if not lowered:
            return False
        return not (
            any(item in {"-h", "--help"} for item in lowered)
            or lowered[0] in {"help", "version", "--version", "schema"}
        )

    @classmethod
    def network_intent(cls, command: str) -> NetworkIntent:
        """Describe only statically provable network intent.

        Unknown programs deliberately remain opaque.  A normal Docker bridge
        is not an origin firewall, so this metadata narrows approval reuse; it
        must never be interpreted as transport-level enforcement.
        """

        effects = cls.capabilities(command)
        if not effects.network:
            return NetworkIntent()
        try:
            segments, _ = cls._segments(command)
        except ValueError:
            return NetworkIntent(required=True, remote_effect="unknown", transport_profile="opaque")
        if len(segments) != 1:
            return NetworkIntent(required=True, remote_effect="unknown", transport_profile="opaque")
        tokens = cls._unwrap(segments[0])
        if not tokens:
            return NetworkIntent(required=True, remote_effect="unknown", transport_profile="opaque")
        executable = Path(tokens[0]).name.lower()
        if executable == "lark-cli":
            lowered = [item.lower() for item in tokens[1:]]
            effect = "auth" if lowered[:1] in (["auth"], ["config"]) else "unknown"
            return NetworkIntent(
                required=True,
                target_known=False,
                remote_effect=effect,
                transport_profile="declared_cli:lark",
            )
        if executable != "curl":
            return NetworkIntent(required=True, remote_effect="unknown", transport_profile="opaque")
        origins: set[str] = set()
        for token in tokens[1:]:
            if not token.lower().startswith(("http://", "https://")):
                continue
            try:
                parsed = urlsplit(token)
                port = parsed.port
            except ValueError:
                continue
            if not parsed.hostname or parsed.username or parsed.password:
                continue
            scheme = parsed.scheme.lower()
            default_port = 443 if scheme == "https" else 80
            origins.add(f"{scheme}://{parsed.hostname.lower()}:{port or default_port}")
        lowered = [item.lower() for item in tokens[1:]]
        mutating_flags = {
            "-d", "--data", "--data-ascii", "--data-binary", "--data-raw",
            "--data-urlencode", "-f", "--form", "--form-string", "-t",
            "--upload-file", "--json",
        }
        remote_effect = "mutate" if any(
            item in mutating_flags
            or item.startswith(tuple(f"{flag}=" for flag in mutating_flags if flag.startswith("--")))
            for item in lowered
        ) else "read"
        return NetworkIntent(
            required=True,
            origins=tuple(sorted(origins)),
            target_known=bool(origins),
            remote_effect=remote_effect,
            transport_profile="validated_http" if origins else "opaque",
        )

    @staticmethod
    def _curl_writes_material_output(tokens: list[str]) -> bool:
        """Return whether curl stores response bytes in a material file.

        ``curl -o /dev/null`` is a common status/probe command. Treating that
        sink as a workspace write adds a capability the command cannot use and
        defeats an otherwise valid Session network grant.
        """

        index = 1
        while index < len(tokens):
            argument = tokens[index]
            if argument in {"-O", "--remote-name", "--remote-header-name", "-OJ"}:
                return True
            if argument in {"-o", "--output"}:
                if (
                    index + 1 >= len(tokens)
                    or tokens[index + 1] not in _NON_MATERIAL_REDIRECT_SINKS
                ):
                    return True
                index += 2
                continue
            if argument.startswith("--output="):
                if argument.partition("=")[2] not in _NON_MATERIAL_REDIRECT_SINKS:
                    return True
            elif argument.startswith("-o") and len(argument) > 2:
                if argument[2:] not in _NON_MATERIAL_REDIRECT_SINKS:
                    return True
            index += 1
        return False

    @classmethod
    def capabilities(
        cls,
        command: str,
        *,
        workspace_path: str | Path | None = None,
        _seen_scripts: frozenset[Path] = frozenset(),
    ) -> ShellCapabilities:
        """Classify effects without treating authorization as an execution hint.

        This method is shared by preflight and the backend router.  Therefore
        an ALLOW decision cannot silently acquire network or write power after
        policy evaluation.
        """

        try:
            segments, has_write_redirect = cls._segments(command)
        except ValueError:
            return ShellCapabilities()
        network = False
        workspace_write = has_write_redirect
        package_install = False
        destructive = False
        for raw_tokens in segments:
            tokens = cls._unwrap(raw_tokens)
            if not tokens:
                continue
            executable = Path(tokens[0]).name.lower()
            args = [item.lower() for item in tokens[1:]]
            joined_args = " ".join(tokens[1:])
            if executable in _SHELLS and len(tokens) >= 3 and args[0] in {"-c", "-lc"}:
                nested = cls.capabilities(
                    tokens[2],
                    workspace_path=workspace_path,
                    _seen_scripts=_seen_scripts,
                )
                network = network or nested.network
                workspace_write = workspace_write or nested.workspace_write
                package_install = package_install or nested.package_install
                destructive = destructive or nested.destructive
                continue
            if executable in _SHELLS:
                script = cls._shell_script_path(tokens[1:], workspace_path)
                if script is not None and script not in _seen_scripts:
                    try:
                        script_text = script.read_text(encoding="utf-8")
                    except (OSError, UnicodeError):
                        script_text = ""
                    if script_text:
                        nested = cls.capabilities(
                            cls._normalize_shell_script(script_text),
                            workspace_path=workspace_path,
                            _seen_scripts=_seen_scripts | {script},
                        )
                        network = network or nested.network
                        workspace_write = workspace_write or nested.workspace_write
                        package_install = package_install or nested.package_install
                        destructive = destructive or nested.destructive
                        continue
            if executable in _NETWORK_COMMANDS:
                network = True
                if executable == "wget" or (
                    executable == "curl"
                    and cls._curl_writes_material_output(tokens)
                ):
                    workspace_write = True
            if executable == "lark-cli" and cls._lark_cli_requires_network(tokens):
                # Routing and authorization are intentionally separate.  This
                # marks every non-local lark action for the one-shot network
                # container without claiming the action itself is safe.
                network = True
            if executable in _DESTRUCTIVE_OR_WRITE_COMMANDS:
                workspace_write = True
            if executable == "rm" and any(
                arg in _RECURSIVE_RM_FLAGS
                or arg.startswith("--recursive=")
                or (arg.startswith("-") and "r" in arg[1:])
                for arg in tokens[1:]
            ):
                destructive = True
            if executable == "rmdir":
                destructive = True
            if executable == "sed" and any(arg == "-i" or arg.startswith("-i") for arg in args):
                workspace_write = True
            if executable == "sort" and any(arg == "-o" or arg.startswith("--output") for arg in args):
                workspace_write = True
            if executable == "uniq" and len([arg for arg in args if not arg.startswith("-")]) > 1:
                workspace_write = True
            if executable == "find":
                write_flags = {
                    "-delete",
                    "-exec",
                    "-execdir",
                    "-ok",
                    "-okdir",
                    "-fprint",
                    "-fprint0",
                    "-fprintf",
                }
                matched = next((arg for arg in args if arg in write_flags), None)
                if matched:
                    workspace_write = True
                if matched in {"-delete", "-exec", "-execdir", "-ok", "-okdir"}:
                    destructive = True
                if matched in {"-exec", "-execdir", "-ok", "-okdir"}:
                    nested_tokens = tokens[tokens.index(matched) + 1 :]
                    nested_tokens = [item for item in nested_tokens if item not in {"{}", ";", "+"}]
                    if nested_tokens:
                        nested = cls.capabilities(shlex.join(nested_tokens))
                        network = network or nested.network
                        workspace_write = workspace_write or nested.workspace_write
                        package_install = package_install or nested.package_install
                        destructive = destructive or nested.destructive
            package_subcommands = {
                "install",
                "uninstall",
                "add",
                "remove",
                "update",
                "upgrade",
                "sync",
            }
            package_action = next(
                (item for item in args if not item.startswith("-")),
                "",
            )
            if executable in {"apt", "apt-get", "apk", "dnf", "yum", "brew", "npx", "uvx"} or (
                executable in {"pip", "pip3", "poetry", "conda", "pipenv", "uv"}
                and package_action in package_subcommands | {"pip", "tool", "run"}
            ):
                package_install = True
                network = True
                workspace_write = True
            if executable == "git":
                subcommand = ""
                skip_git_value = False
                for item in args:
                    if skip_git_value:
                        skip_git_value = False
                        continue
                    if item in {"-c", "--git-dir", "--work-tree"}:
                        skip_git_value = True
                        continue
                    if item.startswith("-"):
                        continue
                    subcommand = item
                    break
                if subcommand in {"clone", "fetch", "pull", "push", "submodule"}:
                    network = True
                if subcommand in {
                    "add",
                    "commit",
                    "switch",
                    "checkout",
                    "stash",
                    "merge",
                    "rebase",
                    "reset",
                    "clean",
                    "restore",
                    "clone",
                    "fetch",
                    "pull",
                    "submodule",
                }:
                    workspace_write = True
                if subcommand in {"reset", "clean", "rebase", "restore"}:
                    destructive = True
            option_value_flags = {"--prefix", "--dir", "--cwd", "-c", "-C"}
            normalized_subcommand = ""
            skip_next = False
            for item in args:
                if skip_next:
                    skip_next = False
                    continue
                if item in option_value_flags:
                    skip_next = True
                    continue
                if item.startswith("-"):
                    continue
                normalized_subcommand = item
                break
            if executable in {"npm", "npx", "pnpm", "yarn", "uvx"} and (
                executable in {"npx", "uvx"}
                or normalized_subcommand in {"ci", "install", "add", "remove", "uninstall"}
            ):
                network = True
                package_install = True
                workspace_write = True
            if executable in {"python", "python3"} and args[:2] == ["-m", "pip"] and any(
                item in package_subcommands for item in args[2:]
            ):
                network = True
                package_install = True
                workspace_write = True
            if executable == "corepack" and args[:1] in (["install"], ["prepare"]):
                network = True
                package_install = True
                workspace_write = True
            if executable in {"python", "python3", "node", "ruby", "perl", "php"} and (
                _NETWORK_URL_PATTERN.search(joined_args)
                or _EMBEDDED_NETWORK_API_PATTERN.search(joined_args)
                or _KNOWN_NETWORK_SKILL_ENTRYPOINT_PATTERN.search(" ".join(tokens))
                or any(
                    item.startswith(("/skills/aihot/", "/skills/tavily-search/"))
                    for item in args
                )
            ):
                network = True
            if executable in {"python", "python3", "node", "ruby", "perl", "php"} and (
                _EMBEDDED_WRITE_API_PATTERN.search(joined_args)
                or any(
                    not item.startswith("-")
                    and item.lower().endswith((".py", ".js", ".mjs", ".cjs", ".rb", ".pl", ".php"))
                    for item in tokens[1:]
                )
            ) and not (executable == "node" and args[:1] == ["--check"]):
                workspace_write = True
            # Project tests can receive a remote base URL. They remain useful
            # low-risk commands, but networking still needs an explicit grant.
            if executable in {
                "pytest",
                "ruff",
                "mypy",
                "pyright",
                "flutter",
                "npm",
                "pnpm",
                "yarn",
            } and _NETWORK_URL_PATTERN.search(joined_args):
                network = True
        return ShellCapabilities(
            network=network,
            workspace_write=workspace_write,
            package_install=package_install,
            destructive=destructive,
        )

    @staticmethod
    def _shell_script_path(
        args: list[str],
        workspace_path: str | Path | None,
    ) -> Path | None:
        if workspace_path is None:
            return None
        candidate = next((item for item in args if not item.startswith("-")), "")
        if not candidate or ".." in Path(candidate).parts:
            return None
        workspace = Path(workspace_path).expanduser().resolve()
        if candidate == "/workspace":
            path = workspace
        elif candidate.startswith("/workspace/"):
            path = workspace / candidate.removeprefix("/workspace/")
        elif Path(candidate).is_absolute():
            return None
        else:
            path = workspace / candidate
        try:
            resolved = path.resolve()
            resolved.relative_to(workspace)
        except (OSError, ValueError):
            return None
        return resolved if resolved.is_file() else None

    @staticmethod
    def _normalize_shell_script(content: str) -> str:
        return "; ".join(
            line.strip()
            for line in content.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )

    def _check_absolute_paths(self, segments: list[list[str]]) -> ToolPolicyResult | None:
        for tokens in segments:
            for token in tokens:
                for raw in self._absolute_path_fragments(token):
                    if raw == "/harness-scratch" or raw.startswith("/harness-scratch/"):
                        return ToolPolicyResult(
                            PolicyDecision.DENY,
                            "harness_internal_path_access",
                            "critical",
                        )
                    if (raw == "/scratch" or raw.startswith("/scratch/")) and ".." in Path(raw).parts:
                        return ToolPolicyResult(
                            PolicyDecision.DENY,
                            "scratch_path_traversal",
                            "critical",
                        )
                    if self.backend_mode != "restricted_host":
                        continue
                    if raw == "/workspace" or raw.startswith("/workspace/"):
                        continue
                    if raw == "/scratch" or raw.startswith("/scratch/"):
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

    @staticmethod
    def _absolute_path_fragments(token: str) -> list[str]:
        """Find absolute host paths while preserving quoted whitespace."""

        if "://" in token:
            return []
        fragments: list[str] = []
        index = 0
        anchors = "=:'\"([{,"
        terminators = set("\t\r\n,;|&<>)]}\"")
        while index < len(token):
            start = token.find("/", index)
            if start < 0:
                break
            previous = token[start - 1] if start > 0 else ""
            if start > 0 and not previous.isspace() and previous not in anchors:
                index = start + 1
                continue
            quote = previous if previous in {"'", '"'} else None
            if quote is not None:
                end = token.find(quote, start)
                if end < 0:
                    end = len(token)
            elif start == 0 or previous == "=":
                # shlex keeps a quoted path with spaces as one token.
                end = start
                while end < len(token) and token[end] not in terminators:
                    end += 1
            else:
                end = start
                while end < len(token) and token[end] not in terminators and not token[end].isspace():
                    end += 1
            raw = token[start:end].rstrip("),]}")
            if raw:
                fragments.append(raw)
            index = max(end, start + 1)
        return fragments

    def _check_relative_paths(
        self,
        segments: list[list[str]],
        *,
        inline_program: bool = False,
    ) -> ToolPolicyResult | None:
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
                # shlex places a heredoc program body in the command token
                # stream. JSON/Python/JS list literals therefore contain `[`;
                # that is source code, not shell pathname expansion. Inline
                # interpreter source (-c/-e/--eval/heredoc) never passes
                # through shell globbing, so wildcard-looking characters
                # there are arithmetic or code, not path expansion. The
                # scratch-mount substring stays critical in every mode:
                # interpreters have their own glob APIs and can enumerate
                # /harness-scratch without the shell's help.
                expansion_syntax = (
                    "harness-scrat" in raw
                    or (
                        not inline_program
                        and (
                            "*" in raw
                            or "?" in raw
                            or "[" in raw
                        )
                    )
                )
                if (
                    self.backend_mode == "docker"
                    and expansion_syntax
                    and (
                        "/" in raw
                        or "harness-scrat" in raw
                        or raw.startswith(("*", "?", ".", "~"))
                    )
                ):
                    # The project container currently reuses one host scratch
                    # mount. Shell pathname expansion can otherwise conceal
                    # `/harness-scratch` (for example `harness-scrat[c]h`) and
                    # bypass literal path checks. Inline interpreter source can
                    # also contain list brackets; do not misclassify code that
                    # is not path-like as shell pathname expansion.
                    return ToolPolicyResult(
                        PolicyDecision.DENY,
                        "container_path_expansion",
                        "critical",
                    )
                normalized_parts = Path(raw.replace("\\", "/")).parts
                if "harness-scratch" in normalized_parts or "harness-scratch" in raw:
                    return ToolPolicyResult(
                        PolicyDecision.DENY,
                        "harness_internal_path_access",
                        "critical",
                    )
                if self.backend_mode == "docker" and ".." in normalized_parts:
                    return ToolPolicyResult(
                        PolicyDecision.DENY,
                        "container_workspace_escape",
                        "critical",
                    )
                if self.backend_mode != "restricted_host":
                    continue
                if raw == "/workspace":
                    candidate = self.workspace_path
                elif raw.startswith("/workspace/"):
                    candidate = self.workspace_path / raw.removeprefix("/workspace/")
                elif raw == "/scratch" or raw.startswith("/scratch/"):
                    continue
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
        has_write_redirect = False
        for index, token in enumerate(tokens):
            if token in {"&&", "||", ";", "|", "&"}:
                if current:
                    segments.append(current)
                    current = []
                continue
            is_redirect = token in {
                ">",
                ">>",
                "<",
                "2>",
                "2>>",
                "&>",
                "&>>",
                ">&",
            } or set(token) <= {
                ">",
                "<",
            }
            if is_redirect:
                if ">" in token:
                    target = tokens[index + 1] if index + 1 < len(tokens) else ""
                    duplicates_fd = token == ">&" and target.isdigit()
                    if (
                        not duplicates_fd
                        and target not in _NON_MATERIAL_REDIRECT_SINKS
                    ):
                        has_write_redirect = True
                continue
            current.append(token)
        if current:
            segments.append(current)
        return segments, has_write_redirect

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
        if command in _SHELLS:
            script = self._shell_script_path(args, self.workspace_path)
            if script is None:
                return ToolPolicyResult(PolicyDecision.ASK, f"arbitrary_shell:{command}", "high")
            try:
                script_text = script.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                return ToolPolicyResult(PolicyDecision.ASK, f"unreadable_shell_script:{command}", "high")
            nested = self.analyze(self._normalize_shell_script(script_text))
            if nested.decision != PolicyDecision.ALLOW:
                return nested
            # A shell file is never approved merely because its entry point is
            # ``bash``/``sh``.  Strict mode still asks; smart Docker mode can
            # take the deterministic fast path only after the script body was
            # successfully inspected and classified as boundary-safe.
            return ToolPolicyResult(
                PolicyDecision.ASK,
                f"inspected_shell_script:{command}",
                "managed_write",
            )
        if command in _NETWORK_COMMANDS:
            return ToolPolicyResult(
                PolicyDecision.ASK,
                f"network_access:{command}",
                "network",
            )
        if command == "lark-cli":
            if not self._lark_cli_requires_network(tokens):
                return ToolPolicyResult(
                    PolicyDecision.ALLOW,
                    "declared_cli_local_inspection:lark-cli",
                    "low",
                )
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "network_access:lark-cli",
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
            if command.startswith("python") and args[:3] in (
                ["-m", "pip", "install"],
                ["-m", "pip", "uninstall"],
            ):
                return ToolPolicyResult(
                    PolicyDecision.ASK,
                    f"package_management:{command}",
                    "package_install",
                )
            if command.startswith("python") and args[:2] == ["-m", "pytest"]:
                return ToolPolicyResult(PolicyDecision.ALLOW, "project_test", "low")
            return ToolPolicyResult(
                PolicyDecision.ASK,
                f"arbitrary_interpreter:{command}",
                "high",
            )
        if command == "rm":
            recursive = any(
                arg in _RECURSIVE_RM_FLAGS
                or arg.startswith("--recursive=")
                or (arg.startswith("-") and "r" in arg[1:])
                for arg in args
            )
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "destructive_workspace_delete:rm_recursive" if recursive else "managed_workspace_write:rm",
                "high" if recursive else "managed_write",
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
        if command == "rg" and any(arg == "--pre" or arg.startswith("--pre=") for arg in args):
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "external_command_hook:rg",
                "high",
            )
        if command == "git":
            if any(arg == "--ext-diff" for arg in args) or any(
                arg == "-c"
                and index + 1 < len(args)
                and (".external=" in args[index + 1].lower() or "pager." in args[index + 1].lower())
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
            subcommand = self._git_subcommand(args)
            if subcommand in {"status", "diff", "log", "show", "branch", "rev-parse"}:
                return ToolPolicyResult(PolicyDecision.ALLOW, "safe_git_read", "low")
            if subcommand in {"push", "pull", "fetch", "clone"}:
                return ToolPolicyResult(PolicyDecision.ASK, "git_network", "network")
            return ToolPolicyResult(
                PolicyDecision.ASK,
                f"managed_git_write:{subcommand or 'unknown'}",
                "managed_write",
            )
        if command in {"pytest", "ruff", "mypy", "pyright"}:
            return ToolPolicyResult(PolicyDecision.ALLOW, "project_test", "low")
        if command in {"npm", "pnpm", "yarn"}:
            normalized = [arg.lower() for arg in args]
            if normalized[:1] in (["test"], ["build"], ["lint"]) or (
                normalized[:1] == ["run"] and normalized[1:2] in (["test"], ["build"], ["lint"], ["check"])
            ):
                return ToolPolicyResult(PolicyDecision.ALLOW, "project_test", "low")
            if normalized[:1] in (
                ["ci"],
                ["install"],
                ["add"],
                ["remove"],
                ["uninstall"],
            ):
                return ToolPolicyResult(PolicyDecision.ASK, "package_management", "package_install")
            return ToolPolicyResult(PolicyDecision.ASK, "node_command", "high")
        if command == "flutter" and args[:1] in (["test"], ["analyze"]):
            return ToolPolicyResult(PolicyDecision.ALLOW, "project_test", "low")
        if command in _SAFE_READ_COMMANDS:
            if command == "sort" and any(arg == "-o" or arg.startswith("--output") for arg in args):
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
    def _git_subcommand(args: list[str]) -> str:
        skip_value = False
        for item in args:
            if skip_value:
                skip_value = False
                continue
            if item in {"-c", "-C", "--git-dir", "--work-tree"}:
                skip_value = True
                continue
            if item.startswith("-"):
                continue
            return item.lower()
        return ""

    @staticmethod
    def _unwrap(tokens: list[str]) -> list[str]:
        remaining = list(tokens)
        while remaining:
            while remaining and _ENV_ASSIGNMENT_PATTERN.match(remaining[0]):
                remaining.pop(0)
            if not remaining or Path(remaining[0]).name.lower() not in _WRAPPERS:
                break
            wrapper = Path(remaining.pop(0)).name.lower()
            while remaining and remaining[0].startswith("-"):
                option = remaining.pop(0)
                if wrapper == "nice" and option in {"-n", "--adjustment"} and remaining:
                    remaining.pop(0)
            if wrapper == "env":
                while remaining and "=" in remaining[0] and not remaining[0].startswith("="):
                    remaining.pop(0)
            elif wrapper in {"timeout", "gtimeout"} and remaining:
                remaining.pop(0)
        return remaining

    @staticmethod
    def _strictest(results: list[ToolPolicyResult]) -> ToolPolicyResult:
        rank = {
            PolicyDecision.ALLOW: 0,
            PolicyDecision.ASK: 1,
            PolicyDecision.DENY: 2,
        }
        risk_rank = {
            "invalid": 0,
            "low": 0,
            "declared": 0,
            "managed_write": 1,
            "high": 2,
            "network": 3,
            "package_install": 3,
            "managed_skill_write": 3,
            "critical": 4,
        }
        return max(
            results,
            key=lambda result: (
                rank[result.decision],
                risk_rank.get(result.risk, 2),
            ),
        )


class ToolExecutionPipeline(AgentMiddleware):
    """Fail-closed Tool preflight with deterministic allow/ask/deny outcomes."""

    BUILTIN_TOOLS = frozenset(
        {
            "update_todos",
            "read_evidence",
            "ls",
            "read_file",
            "write_file",
            "edit_file",
            "inspect_file_version",
            "copy_file",
            "materialize_source_ref",
            "replace_file",
            "patch_file",
            "patch_files",
            "delete_file",
            "execute_external_directory",
            "validate_html_report",
            "rewind_external_file_changes",
            "upsert_scratch_file",
            "stage_external_artifact",
            "commit_external_artifact",
            "prepare_attachment_edit",
            "publish_attachment",
            "stage_external_directory",
            "prepare_external_directory_commit",
            "commit_external_directory",
            "validate_artifact_contract",
            "glob",
            "grep",
            "execute",
            "task",
        }
    )
    DECLARED_ALLOW_TOOLS = frozenset(
        {
            "update_todos",
            "read_evidence",
            "ls",
            "read_file",
            "write_file",
            "edit_file",
            "inspect_file_version",
            "copy_file",
            "materialize_source_ref",
            "replace_file",
            "patch_file",
            "patch_files",
            "delete_file",
            "validate_html_report",
            "rewind_external_file_changes",
            "upsert_scratch_file",
            "stage_external_artifact",
            "commit_external_artifact",
            "prepare_attachment_edit",
            "publish_attachment",
            "stage_external_directory",
            "prepare_external_directory_commit",
            "commit_external_directory",
            "validate_artifact_contract",
            "glob",
            "grep",
            "task",
            "read_resource",
            "inspect_skill",
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
    NETWORK_TOOLS = frozenset({
        "tavily_search",
        "fetch_url",
        "prepare_skill_install",
        "prepare_skill_update",
    })
    SKILL_COMMIT_TOOLS = frozenset({"install_skill", "update_skill"})

    def __init__(
        self,
        *,
        known_tools: set[str],
        backend_mode: str,
        permission_context: RunPermissionContext | None = None,
        base_dir: Path | None = None,
        reviewer: PermissionReviewer | None = None,
        workspace_backend: Any | None = None,
        managed_cli_service: Any | None = None,
    ) -> None:
        self.known_tools = set(known_tools) | set(self.BUILTIN_TOOLS)
        self.backend_mode = backend_mode
        self.base_dir = base_dir.expanduser().resolve() if base_dir is not None else None
        self.reviewer = reviewer
        self.workspace_backend = workspace_backend
        self.managed_cli_registry = ManagedCliRegistry()
        self.managed_cli_service = managed_cli_service
        if self.managed_cli_service is None and workspace_backend is not None and backend_mode == "docker":
            try:
                from runtime_identity.service import ManagedCliService

                self.managed_cli_service = ManagedCliService(workspace_backend)
            except Exception:
                # Home/Keychain access is deferred until a managed command is
                # actually invoked; construction failures remain fail-closed.
                self.managed_cli_service = None
        self.permission_context = permission_context or RunPermissionContext.from_config_snapshot(
            {
                "permissions": {"approval_mode": "strict"},
                "execution": {"backend_mode": backend_mode},
            }
        )

    @staticmethod
    def _awaiting_skill_confirmation(message: Any) -> bool:
        if not isinstance(message, ToolMessage) or message.name != "execute":
            return False
        try:
            value = json.loads(str(message.content or ""))
        except (TypeError, ValueError):
            return False
        return bool(
            isinstance(value, dict)
            and value.get("managed_by") == "skill_management"
            and value.get("intercepted") is True
            and isinstance(value.get("plans"), list)
            and value["plans"]
            and any(
                isinstance(plan, dict)
                and plan.get("status") == "prepared"
                and plan.get("phase") == "awaiting_confirmation"
                and plan.get("ui_commit_supported") is True
                for plan in value["plans"]
            )
        )

    @staticmethod
    def _awaiting_user_browser(message: Any) -> bool:
        if not isinstance(message, ToolMessage) or message.name != "execute":
            return False
        try:
            value = json.loads(str(message.content or ""))
        except (TypeError, ValueError):
            return False
        return bool(
            isinstance(value, dict)
            and value.get("managed_by") == "managed_cli"
            and value.get("status") == "awaiting_user_browser"
        )

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Pause the Agent loop at the durable Skill confirmation boundary.

        A ``Command(goto=END)`` returned from a wrapped tool is not a reliable
        loop exit in LangChain's ToolNode: its static tools-to-model edge can
        route back to the model.  ``before_model`` is the framework-supported
        control point before another model turn. A structured interrupt keeps
        the Run alive until the immutable plans are committed or cancelled;
        an approved batch then continues from this exact model boundary.
        """

        for message in reversed(list(state.get("messages") or [])):
            if isinstance(message, ToolMessage):
                if self._awaiting_skill_confirmation(message):
                    value = json.loads(str(message.content or ""))
                    context = runtime.context if isinstance(getattr(runtime, "context", None), dict) else {}
                    request = skill_plan_resume_registry.create(
                        session_id=str(context.get("session_id") or ""),
                        query_id=str(context.get("query_id") or ""),
                        run_id=str(context.get("run_id") or ""),
                        tool_call_id=str(message.tool_call_id or ""),
                        plans=[plan for plan in value["plans"] if isinstance(plan, dict)],
                    )
                    decision = interrupt({"type": "skill_plan_confirmation_request", "request": request})
                    statuses = decision.get("statuses") if isinstance(decision, dict) else {}
                    if isinstance(statuses, dict):
                        for plan in value["plans"]:
                            if not isinstance(plan, dict):
                                continue
                            status = statuses.get(str(plan.get("plan_id") or ""))
                            if status:
                                plan["status"] = status
                                plan["phase"] = status
                                plan["installed"] = status == "committed"
                    value["confirmation_completed"] = True
                    value["confirmation_action"] = decision.get("action") if isinstance(decision, dict) else "cancel"
                    updated = message.model_copy(update={"content": json.dumps(value, ensure_ascii=False, sort_keys=True)})
                    if value["confirmation_action"] == "confirm":
                        return {"messages": [updated]}
                    return {"messages": [updated], "jump_to": "end"}
                continue
            break
        return None

    @hook_config(can_jump_to=["end"])
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Allow one user-facing auth summary, but never another dependent tool."""

        del runtime
        messages = list(state.get("messages") or [])
        if not messages or not isinstance(messages[-1], AIMessage):
            return None
        latest = messages[-1]
        boundary: ToolMessage | None = None
        for message in reversed(messages[:-1]):
            if isinstance(message, ToolMessage):
                if self._awaiting_user_browser(message):
                    boundary = message
                break
            if isinstance(message, AIMessage):
                break
        if boundary is None:
            return None
        if not latest.tool_calls:
            return {"jump_to": "end"}
        try:
            payload = json.loads(str(boundary.content or ""))
        except (TypeError, ValueError):
            payload = {}
        output = str(payload.get("output") or "") if isinstance(payload, dict) else ""
        url_match = re.search(r"https?://[^\s]+", output)
        link = f"\n\n授权链接：{url_match.group(0)}" if url_match else ""
        replacement = latest.model_copy(
            update={
                "content": (
                    "飞书授权流程已启动，但尚未完成。请使用上方二维码或授权链接在浏览器中完成操作。"
                    "完成后告诉我；下一轮会先验证配置和登录状态，再继续后续步骤。"
                    f"{link}"
                ),
                "tool_calls": [],
            }
        )
        return {"messages": [replacement], "jump_to": "end"}

    @staticmethod
    def _segment_is_npx_skills_add(tokens: list[str]) -> bool:
        tokens = ShellPolicyAnalyzer.unwrap_command(tokens)
        if not tokens or Path(tokens[0]).name.lower() != "npx":
            return False
        lowered = [token.lower() for token in tokens[1:]]
        return any(
            lowered[index : index + 2] == ["skills", "add"]
            for index in range(max(0, len(lowered) - 1))
        )

    @classmethod
    def _contains_npx_skills_add(cls, command: str) -> bool:
        try:
            parsed_match = any(
                cls._segment_is_npx_skills_add(segment)
                for segment in ShellPolicyAnalyzer.parse_segments(command)
            )
        except ValueError:
            parsed_match = False
        # Also catch commands hidden behind a shell wrapper such as
        # ``sh -c 'npx skills add ...'``.  They are deliberately denied rather
        # than partially unwrapped and executed.
        return parsed_match or bool(
            re.search(
                r"(?:^|[\s'\"/])npx\b[^;&|\n]*?\bskills\s+add\b",
                command,
                re.IGNORECASE,
            )
        )

    @classmethod
    def _managed_npx_skills_add(cls, command: str) -> ManagedNpxSkillsAdd | None:
        """Parse only a standalone add command; mixed shell programs never partly execute."""

        # Agents commonly append ``2>&1`` so stderr is visible in the same
        # tool result.  It has no filesystem or process-composition effect and
        # is safe to remove before parsing. Pipes, file redirects, background
        # jobs, command substitution, and compound commands remain rejected.
        command = re.sub(r"\s+2\s*>\s*&\s*1\s*$", "", command.strip())
        try:
            segments = ShellPolicyAnalyzer.parse_segments(command)
        except ValueError:
            return None
        if len(segments) != 1 or not cls._segment_is_npx_skills_add(segments[0]):
            return None
        tokens = ShellPolicyAnalyzer.unwrap_command(segments[0])
        lowered = [token.lower() for token in tokens]
        try:
            skills_index = next(
                index
                for index in range(1, len(tokens) - 1)
                if lowered[index : index + 2] == ["skills", "add"]
            )
        except StopIteration:
            return None
        args = tokens[skills_index + 2 :]
        source = ""
        skill_names: list[str] = []
        yes = any(
            token.lower() in {"-y", "--yes"}
            for token in tokens[1:skills_index]
        )
        install_all = False
        list_only = False
        index = 0
        while index < len(args):
            value = args[index]
            lowered_value = value.lower()
            if lowered_value in {"-y", "--yes"}:
                yes = True
                index += 1
                continue
            if lowered_value == "--all":
                install_all = True
                yes = True
                index += 1
                continue
            if lowered_value in {"-l", "--list"}:
                list_only = True
                index += 1
                continue
            if lowered_value in {"-g", "--global", "--copy", "--full-depth"}:
                index += 1
                continue
            if lowered_value.startswith("--skill="):
                skill_names.append(value.partition("=")[2])
                index += 1
                continue
            if lowered_value in {"-s", "--skill"}:
                index += 1
                while index < len(args) and not args[index].startswith("-"):
                    skill_names.append(args[index])
                    index += 1
                continue
            if lowered_value.startswith("--agent="):
                index += 1
                continue
            if lowered_value in {"-a", "--agent", "--subagent"}:
                index += 1
                while index < len(args) and not args[index].startswith("-"):
                    index += 1
                continue
            if value.startswith("-"):
                # Unknown CLI options remain under Skill Manager authority but
                # are rejected rather than passed through to an arbitrary npx.
                return None
            if not source:
                source = value
                index += 1
                continue
            return None
        if not source:
            return None
        return ManagedNpxSkillsAdd(
            source=source,
            skill_names=tuple(skill_names),
            yes=yes,
            install_all=install_all,
            list_only=list_only,
        )

    async def _invoke_authorized(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
        managed_add: ManagedNpxSkillsAdd | None,
        managed_cli: Any | None = None,
    ) -> ToolMessage | Command[Any]:
        if managed_cli is not None:
            if self.managed_cli_service is None:
                return ToolMessage(
                    content=json.dumps(
                        {
                            "ok": False,
                            "managed_by": "managed_cli",
                            "error": "managed_cli_service_unavailable",
                            "message": "Managed CLI execution requires the Docker runtime.",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    name="execute",
                    tool_call_id=str(request.tool_call.get("id") or ""),
                    status="error",
                )
            context = self._context(request)
            result = await asyncio.to_thread(self.managed_cli_service.execute, managed_cli, context)
            return ToolMessage(
                content=result.content,
                name="execute",
                tool_call_id=str(request.tool_call.get("id") or ""),
                status="success" if result.exit_code == 0 else "error",
            )
        if managed_add is None:
            return await handler(request)
        if self.base_dir is None:
            return ToolMessage(
                content=json.dumps(
                    {
                        "ok": False,
                        "managed_by": "skill_management",
                        "error": "skill_manager_unavailable",
                    },
                    ensure_ascii=False,
                ),
                name="execute",
                tool_call_id=str(request.tool_call.get("id") or ""),
                status="error",
            )
        context = self._context(request)
        from services.skill_management import SkillManagementError, get_skill_management_service

        try:
            service = get_skill_management_service(self.base_dir)
            result = await asyncio.to_thread(
                service.prepare_npx_skills_add,
                source=managed_add.source,
                skill_names=list(managed_add.skill_names),
                yes=managed_add.yes,
                install_all=managed_add.install_all,
                list_only=managed_add.list_only,
                request_context={
                    key: str(context.get(key) or "")
                    for key in ("session_id", "query_id", "run_id")
                    if str(context.get(key) or "")
                },
            )
            status = (
                "success"
                if result.get("ok")
                or result.get("selection_required")
                or result.get("list_only")
                or bool(result.get("plans"))
                else "error"
            )
            content = json.dumps(result, ensure_ascii=False, sort_keys=True)
        except SkillManagementError as exc:
            payload = exc.as_dict()
            payload["managed_by"] = "skill_management"
            payload["intercepted"] = True
            status = "error"
            content = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except Exception as exc:
            content = json.dumps(
                {
                    "ok": False,
                    "managed_by": "skill_management",
                    "intercepted": True,
                    "error": "managed_skill_prepare_failed",
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            status = "error"
        return ToolMessage(
            content=content,
            name="execute",
            tool_call_id=str(request.tool_call.get("id") or ""),
            status=status,
        )

    @classmethod
    def _managed_npx_rejection(cls, request: ToolCallRequest) -> ToolMessage:
        """Return a Skill Manager-owned error without ever invoking a shell."""

        return ToolMessage(
            content=json.dumps(
                {
                    "ok": False,
                    "managed_by": "skill_management",
                    "intercepted": True,
                    "error": "unsupported_npx_skills_add_form",
                    "message": (
                        "Skill Manager intercepted npx skills add, but the command must be one "
                        "standalone supported add operation. Pipes, file redirects, shell wrappers, "
                        "compound commands, and unknown options are not executed."
                    ),
                    "command": cls._command(request),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            name="execute",
            tool_call_id=str(request.tool_call.get("id") or ""),
            status="error",
        )

    @classmethod
    def _managed_cli_rejection(cls, request: ToolCallRequest, message: str) -> ToolMessage:
        return ToolMessage(
            content=json.dumps(
                {
                    "ok": False,
                    "managed_by": "managed_cli",
                    "error": "unsupported_managed_cli_command",
                    "message": message,
                    "command": cls._command(request),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            name="execute",
            tool_call_id=str(request.tool_call.get("id") or ""),
            status="error",
        )

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        is_execute = str(request.tool_call.get("name") or "") == "execute"
        command = self._command(request) if is_execute else ""
        contains_managed_add = is_execute and self._contains_npx_skills_add(command)
        managed_add = self._managed_npx_skills_add(command) if contains_managed_add else None
        if contains_managed_add and managed_add is None:
            # Ownership begins before policy evaluation: unsupported syntax is
            # a Skill Manager validation error, never a raw terminal command.
            return self._managed_npx_rejection(request)
        managed_cli: Any | None = None
        if is_execute and not contains_managed_add:
            try:
                managed_match = self.managed_cli_registry.match(command)
                managed_cli = (
                    self.managed_cli_service.plan(managed_match, self._context(request))
                    if managed_match is not None and self.managed_cli_service is not None
                    else managed_match
                )
            except UnsupportedManagedCliCommand as exc:
                # Like Skill Manager ownership, Adapter ownership begins
                # before generic shell policy and never falls through.
                return self._managed_cli_rejection(request, str(exc))
            except Exception as exc:  # noqa: BLE001
                return self._managed_cli_rejection(
                    request,
                    f"Managed CLI planning failed: {type(exc).__name__}: {exc}",
                )
        result = await self._apreflight(request)
        if result.decision == PolicyDecision.ALLOW:
            self._record_reviewer_decision(request, result)
            delta_denial = self._delta_repair_denial(request)
            if delta_denial is not None:
                return delta_denial
            return await self._invoke_authorized(request, handler, managed_add, managed_cli)
        if result.decision == PolicyDecision.DENY:
            self._record_reviewer_decision(request, result)
            return self._denied_message(request, result)

        context = self._context(request)
        session_id = str(context.get("session_id") or "")
        query_id = str(context.get("query_id") or "")
        command = (
            managed_cli.approval_preview()
            if managed_cli is not None and hasattr(managed_cli, "approval_preview")
            else self._action_preview(request)
        )
        tool_name = str(request.tool_call.get("name") or "")
        fingerprint = permission_resume_registry.tool_action_fingerprint(
            tool_name=tool_name,
            command=command,
            reason=result.reason,
        )
        session_scope = self._session_grant_scope(request)
        required_capabilities = self._required_capabilities(request)
        run_id = str(context.get("run_id") or "")
        if session_manager.consume_tool_action_permission(
            session_id,
            fingerprint,
            session_target_kind=(str(session_scope["target_kind"]) if session_scope else None),
            session_target=str(session_scope["target"]) if session_scope else None,
            required_bindings=self.permission_context.grant_bindings(),
            required_capabilities=required_capabilities,
            current_run_id=run_id,
        ):
            if run_id:
                session_manager.transition_run_status(
                    session_id,
                    run_id,
                    "running",
                    expected_statuses={"running", "waiting_hitl"},
                )
            delta_denial = self._delta_repair_denial(request)
            if delta_denial is not None:
                return delta_denial
            return await self._invoke_authorized(request, handler, managed_add, managed_cli)
        preview = permission_resume_registry.create_tool_action_request(
            session_id=session_id,
            query_id=query_id,
            tool_call_id=str(request.tool_call.get("id") or ""),
            tool_name=tool_name,
            command=command,
            reason=result.reason,
            risk=result.risk,
            session_target_kind=(str(session_scope["target_kind"]) if session_scope else None),
            session_target=str(session_scope["target"]) if session_scope else None,
            session_scope_label=(str(session_scope["label"]) if session_scope else None),
            run_id=str(context.get("run_id") or ""),
            grant_bindings=self.permission_context.grant_bindings(),
            required_capabilities=required_capabilities,
            change_preview=self._skill_change_preview(request),
            policy_source=result.source,
            policy_explanation=result.explanation,
            control_descriptor=self._control_descriptor(tool_name),
        )
        if run_id:
            session_manager.transition_run_status(
                session_id,
                run_id,
                "waiting_hitl",
                expected_statuses={"running", "waiting_hitl"},
            )
        decision = interrupt(
            {
                "type": "permission_request",
                "request": preview,
                "decisions": [{"type": "approve"}, {"type": "reject"}],
            }
        )
        if run_id:
            session_manager.transition_run_status(
                session_id,
                run_id,
                "running",
                expected_statuses={"running", "waiting_hitl"},
            )
        if isinstance(decision, dict) and decision.get("type") == "approve":
            if session_manager.consume_tool_action_permission(
                session_id,
                fingerprint,
                session_target_kind=(str(session_scope["target_kind"]) if session_scope else None),
                session_target=str(session_scope["target"]) if session_scope else None,
                required_bindings=self.permission_context.grant_bindings(),
                required_capabilities=required_capabilities,
                current_run_id=run_id,
            ):
                delta_denial = self._delta_repair_denial(request)
                if delta_denial is not None:
                    return delta_denial
                return await self._invoke_authorized(request, handler, managed_add, managed_cli)
        return self._denied_message(request, result)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        managed_cli: Any | None = None
        if str(request.tool_call.get("name") or "") == "execute":
            try:
                managed_match = self.managed_cli_registry.match(self._command(request))
                managed_cli = (
                    self.managed_cli_service.plan(managed_match, self._context(request))
                    if managed_match is not None and self.managed_cli_service is not None
                    else managed_match
                )
            except UnsupportedManagedCliCommand as exc:
                return self._managed_cli_rejection(request, str(exc))
            except Exception as exc:  # noqa: BLE001
                return self._managed_cli_rejection(
                    request,
                    f"Managed CLI planning failed: {type(exc).__name__}: {exc}",
                )
        result = self._preflight(request)
        if result.decision == PolicyDecision.ALLOW:
            delta_denial = self._delta_repair_denial(request)
            if delta_denial is not None:
                return delta_denial
            if managed_cli is not None:
                if self.managed_cli_service is None:
                    return self._managed_cli_rejection(request, "Managed CLI execution requires the Docker runtime.")
                managed = self.managed_cli_service.execute(managed_cli, self._context(request))
                return ToolMessage(
                    content=managed.content,
                    name="execute",
                    tool_call_id=str(request.tool_call.get("id") or ""),
                    status="success" if managed.exit_code == 0 else "error",
                )
            return handler(request)
        return self._denied_message(request, result)

    def _delta_repair_denial(self, request: ToolCallRequest) -> ToolMessage | None:
        """Enforce the server-authored bounded repair policy before execution."""

        context = self._context(request)
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        if not session_id or not run_id:
            return None
        run = session_manager.get_run_state(session_id, run_id)
        if not isinstance(run, dict) or run.get("execution_mode") != "delta_repair":
            return None
        tool_name = str(request.tool_call.get("name") or "")
        repair_kind = str(run.get("delta_repair_kind") or "bounded_unknown")
        forbidden = {"task"}
        if repair_kind == "presentation_only":
            forbidden.update(
                {
                    "stage_external_directory",
                    "prepare_external_directory_commit",
                    "commit_external_directory",
                }
            )
            forbidden.update(
                name
                for name in self.known_tools
                if name.startswith("database_")
            )
        reservation = session_manager.reserve_delta_repair_tool_call(
            session_id,
            run_id,
            str(request.tool_call.get("id") or ""),
        )
        if not reservation.get("allowed"):
            reason = str(reservation.get("reason") or "delta_repair_policy")
            return ToolMessage(
                content=(
                    "Tool call blocked by bounded delta-repair policy: "
                    f"{reason}; count={reservation.get('count', 0)}; "
                    f"limit={reservation.get('limit', run.get('delta_repair_tool_budget'))}. "
                    "Use the already-related exact artifacts, finish the minimum patch, or explain why "
                    "a new full Run is required."
                ),
                name=tool_name,
                tool_call_id=str(request.tool_call.get("id") or ""),
                status="error",
            )
        if tool_name in forbidden:
            return ToolMessage(
                content=(
                    "Tool call blocked by bounded delta-repair policy: "
                    f"{tool_name} is outside {repair_kind}. Work from the related exact artifacts; "
                    "do not delegate or expand to database/directory-wide work for this repair."
                ),
                name=tool_name,
                tool_call_id=str(request.tool_call.get("id") or ""),
                status="error",
            )
        return None

    def _preflight(self, request: ToolCallRequest) -> ToolPolicyResult:
        tool_name = str(request.tool_call.get("name") or "")
        if tool_name not in self.known_tools:
            return ToolPolicyResult(
                PolicyDecision.DENY,
                f"unknown_tool:{tool_name}",
                "critical",
            )
        control_descriptor = tool_control_descriptor(tool_name)
        if control_descriptor is None:
            return ToolPolicyResult(
                PolicyDecision.DENY,
                f"missing_tool_control_descriptor:{tool_name}",
                "critical",
            )
        if tool_name == "edit_file":
            return ToolPolicyResult(
                PolicyDecision.DENY,
                "versioned_patch_required: use inspect_file_version then patch_file",
                "managed_write",
            )
        if tool_name == "execute":
            command = self._command(request)
            if self._contains_npx_skills_add(command):
                if self._managed_npx_skills_add(command) is None:
                    return ToolPolicyResult(
                        PolicyDecision.DENY,
                        "managed_skill_add_requires_standalone_supported_command",
                        "critical",
                    )
                return ToolPolicyResult(
                    PolicyDecision.ASK,
                    "managed_skill_source_download:npx_skills_add",
                    "network",
                )
        # The registration-boundary control descriptor is the authority for
        # tools whose effects are entirely read-only or internal.  Do not
        # require a second hand-maintained allowlist entry for every new query
        # result reader/materializer: that list can drift and incorrectly turn
        # safe registered tools into ``unclassified_tool`` denials.
        if (
            control_descriptor.side_effect in {"none", "internal_mutation"}
            and control_descriptor.network_scope == "none"
            and control_descriptor.approval_scope == "none"
        ):
            return ToolPolicyResult(
                PolicyDecision.ALLOW,
                f"control_descriptor:{control_descriptor.policy}",
                "declared",
            )
        if tool_name in self.DECLARED_ALLOW_TOOLS:
            return ToolPolicyResult(
                PolicyDecision.ALLOW,
                "declared_tool_policy",
                "declared",
            )
        if tool_name in self.NETWORK_TOOLS:
            if self.permission_context.smart:
                if tool_name == "tavily_search":
                    return ToolPolicyResult(
                        PolicyDecision.ALLOW,
                        "smart_controlled_network:tavily_search",
                        "network",
                    )
                if tool_name in {"prepare_skill_install", "prepare_skill_update"}:
                    return ToolPolicyResult(
                        PolicyDecision.ASK,
                        f"skill_source_download:{tool_name}",
                        "network",
                    )
                if self._smart_fetch_candidate(request):
                    return ToolPolicyResult(
                        PolicyDecision.ALLOW,
                        "smart_controlled_network:fetch_url",
                        "network",
                    )
                return ToolPolicyResult(
                    PolicyDecision.DENY,
                    "unsafe_fetch_url",
                    "critical",
                )
            return ToolPolicyResult(
                PolicyDecision.ASK,
                f"network_access:{tool_name}",
                "network",
            )
        if tool_name in self.SKILL_COMMIT_TOOLS:
            return ToolPolicyResult(
                PolicyDecision.ASK,
                f"managed_skill_write:{tool_name}",
                "managed_skill_write",
            )
        if tool_name == "install_packages":
            if self.permission_context.backend_mode != "docker":
                return ToolPolicyResult(
                    PolicyDecision.DENY,
                    "package_install_requires_docker",
                    "critical",
                )
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "package_management:install_packages",
                "package_install",
            )
        if tool_name == "execute_external_directory":
            if self.permission_context.backend_mode != "docker" or self.backend_mode != "docker":
                return ToolPolicyResult(
                    PolicyDecision.DENY,
                    "external_directory_command_requires_docker",
                    "critical",
                )
            command = self._command(request)
            args = request.tool_call.get("args") or {}
            mode = str(args.get("mode") or "read_only")
            effects = ShellPolicyAnalyzer.capabilities(
                command,
                workspace_path="/external-workspace",
            )
            if effects.network or effects.package_install:
                return ToolPolicyResult(
                    PolicyDecision.DENY,
                    "external_directory_command_is_offline_and_read_only",
                    "critical",
                )
            if mode == "writable_draft":
                if self._safe_external_draft_command(command):
                    return ToolPolicyResult(
                        PolicyDecision.ALLOW,
                        "external_directory_draft:narrow_engineering_command",
                        "managed_write",
                    )
                return ToolPolicyResult(
                    PolicyDecision.ASK,
                    "external_directory_draft:unknown_or_compound_command",
                    "high" if effects.destructive else "managed_write",
                )
            if mode != "read_only":
                return ToolPolicyResult(
                    PolicyDecision.DENY,
                    "unsupported_external_directory_mode",
                    "critical",
                )
            if self._registered_external_validator(command):
                if (
                    "/opt/puddingclaw/bin/validate-html-report-e2e.mjs"
                    in command
                    and not self._browser_e2e_required(request)
                ):
                    return ToolPolicyResult(
                        PolicyDecision.DENY,
                        "browser_e2e_not_required_by_contract",
                        "critical",
                    )
                return ToolPolicyResult(
                    PolicyDecision.ALLOW,
                    "external_directory_validator:registered_read_only",
                    "declared",
                )
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "external_directory_command:exact_read_only_mount",
                "high" if effects.destructive or effects.workspace_write else "managed_write",
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
        command = self._command(request)
        result = analyzer.analyze(command)
        effects = analyzer.capabilities(
            command,
            workspace_path=str(context.get("workspace_path") or "."),
        )
        if effects.network and result.decision == PolicyDecision.ALLOW:
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "network_access:embedded_command",
                "network",
            )
        smart_result = self._smart_docker_workspace_result(
            command=command,
            result=result,
            effects=effects,
        )
        if smart_result is not None:
            return smart_result
        return result

    async def _apreflight(self, request: ToolCallRequest) -> ToolPolicyResult:
        """Run deterministic policy, then review only an eligible smart gray zone."""

        result = self._preflight(request)
        if not self._reviewer_eligible(request, result):
            return result
        assert self.reviewer is not None
        command = self._command(request)
        context = {
            **self._context(request),
            "backend_mode": self.backend_mode,
        }
        effects = ShellPolicyAnalyzer.capabilities(
            command,
            workspace_path=str(context.get("workspace_path") or "."),
        )
        verdict = await self.reviewer.review(
            tool_name=str(request.tool_call.get("name") or ""),
            action=self._review_action(command, context),
            deterministic_reason=result.reason,
            deterministic_risk=result.risk,
            context=context,
            capabilities={
                "network": effects.network,
                "workspace_write": effects.workspace_write,
                "package_install": effects.package_install,
                "destructive": effects.destructive,
            },
        )
        if verdict.decision == "allow":
            return ToolPolicyResult(
                PolicyDecision.ALLOW,
                f"smart_reviewer_allow:{result.reason}",
                "managed_write" if effects.workspace_write else "low",
                source="codex_grok_smart_reviewer",
                explanation=verdict.explanation,
            )
        if verdict.decision == "deny" and verdict.risk == "critical":
            return ToolPolicyResult(
                PolicyDecision.DENY,
                f"smart_reviewer_deny:{result.reason}",
                "critical",
                source="codex_grok_smart_reviewer",
                explanation=verdict.explanation,
            )
        return ToolPolicyResult(
            PolicyDecision.ASK,
            f"smart_reviewer_ask:{result.reason}",
            verdict.risk,
            source="codex_grok_smart_reviewer",
            explanation=verdict.explanation,
        )

    def _reviewer_eligible(
        self,
        request: ToolCallRequest,
        result: ToolPolicyResult,
    ) -> bool:
        if (
            self.reviewer is None
            or not self.permission_context.smart
            or self.permission_context.backend_mode != "docker"
            or self.backend_mode != "docker"
            or result.decision != PolicyDecision.ASK
            or str(request.tool_call.get("name") or "") != "execute"
        ):
            return False
        command = self._command(request)
        context = self._context(request)
        effects = ShellPolicyAnalyzer.capabilities(
            command,
            workspace_path=str(context.get("workspace_path") or "."),
        )
        if effects.network or effects.package_install or effects.destructive:
            return False
        if result.reason in _SMART_DOCKER_DESTRUCTIVE_REASONS:
            return False
        if result.reason.startswith(
            (
                "managed_workspace_write:find:",
                "managed_git_write:",
                "network_access:",
                "package_management",
                "git_network",
                "external_command_hook:",
            )
        ):
            return False
        if _EMBEDDED_DESTRUCTIVE_API_PATTERN.search(command) or _OPAQUE_CRITICAL_ACTION_PATTERN.search(command):
            return False
        return result.reason.startswith(
            (
                "unknown_command:",
                "complex_shell_expansion",
                "shell_parse_failed",
                "wrapper_without_command",
                "node_command",
                "python_tool:",
            )
        )

    @staticmethod
    def _review_action(command: str, context: dict[str, Any]) -> str:
        """Expose inspected shell content to the reviewer, never only its wrapper."""

        try:
            segments = ShellPolicyAnalyzer.parse_segments(command)
        except ValueError:
            return command
        if len(segments) != 1:
            return command
        tokens = ShellPolicyAnalyzer.unwrap_command(segments[0])
        if not tokens or Path(tokens[0]).name.lower() not in _SHELLS:
            return command
        script = ShellPolicyAnalyzer._shell_script_path(
            tokens[1:],
            str(context.get("workspace_path") or "."),
        )
        if script is None:
            return command
        try:
            content = script.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return command
        return f"{command}\n\n# Inspected local shell script: {script.name}\n{content[:12000]}"

    def _smart_docker_workspace_result(
        self,
        *,
        command: str,
        result: ToolPolicyResult,
        effects: ShellCapabilities,
    ) -> ToolPolicyResult | None:
        """Auto-approve ordinary project work inside the real Docker sandbox.

        Smart mode is intended to remove approval noise for computation,
        validation, and normal project writes.  The deterministic analyzer has
        already denied host-path escapes, privilege escalation, and Docker
        control before this hook runs.  Network/package capabilities and
        explicitly destructive workspace operations remain user-controlled.
        """

        if (
            not self.permission_context.smart
            or self.permission_context.backend_mode != "docker"
            or self.backend_mode != "docker"
            or result.decision != PolicyDecision.ASK
        ):
            return None
        if effects.network or effects.package_install or effects.destructive:
            return None
        if result.reason in _SMART_DOCKER_DESTRUCTIVE_REASONS or result.reason.startswith(
            "managed_workspace_write:find:"
        ):
            return None
        # Unknown/opaque entry points are the Grok-style gray zone.  They go
        # through the reviewer instead of being silently treated as ordinary
        # Docker work.  Unreadable shell files are not reviewable because the
        # reviewer would see only the innocent-looking ``bash file`` wrapper.
        if result.reason.startswith(("arbitrary_shell:", "unreadable_shell_script:")):
            return None
        if result.reason.startswith(
            (
                "unknown_command:",
                "shell_parse_failed",
                "wrapper_without_command",
                "node_command",
                "python_tool:",
            )
        ):
            return None
        if result.reason.startswith("managed_git_write:"):
            if not self._smart_git_write_allowed(command, result.reason):
                return None
        if result.reason == "managed_workspace_write:mv" and not self._smart_move_allowed(command):
            return None
        if result.reason.startswith(("external_command_hook:", "git_network")):
            return None

        # Command substitution remains an opaque execution boundary.  A
        # multiline Python/Node program, on the other hand, is common in Agent
        # workflows and is contained by the Docker workspace policy.
        if re.search(r"`|\$\(|\$\{", command):
            return None
        if _EMBEDDED_DESTRUCTIVE_API_PATTERN.search(command):
            return None

        return ToolPolicyResult(
            PolicyDecision.ALLOW,
            "smart_docker_workspace_write" if effects.workspace_write else "smart_docker_workspace_execute",
            "managed_write" if effects.workspace_write else "low",
        )

    @staticmethod
    def _smart_git_write_allowed(command: str, reason: str) -> bool:
        subcommand = reason.partition(":")[2]
        if subcommand not in _SMART_GIT_WRITE_SUBCOMMANDS and subcommand != "checkout":
            return False
        try:
            segments = ShellPolicyAnalyzer.parse_segments(command)
        except ValueError:
            return False
        if len(segments) != 1:
            return False
        tokens = ShellPolicyAnalyzer.unwrap_command(segments[0])
        if not tokens or Path(tokens[0]).name.lower() != "git":
            return False
        lowered = [item.lower() for item in tokens[1:]]
        if any(item in {"-f", "--force", "--hard", "--discard-changes"} for item in lowered):
            return False
        if subcommand == "checkout":
            # Branch checkout is low-friction; checkout/restore of a path can
            # discard user edits and remains an explicit decision.
            try:
                index = lowered.index("checkout")
            except ValueError:
                return False
            tail = lowered[index + 1 :]
            positional = [item for item in tail if not item.startswith("-")]
            if "--" in tail or len(positional) != 1 or any(item in {"-b", "-B", "--orphan"} for item in tail):
                return False
        if subcommand == "stash" and any(item in {"clear", "drop"} for item in lowered):
            return False
        return True

    def _smart_move_allowed(self, command: str) -> bool:
        """Allow a static rename only when both sides stay in managed roots."""

        try:
            segments = ShellPolicyAnalyzer.parse_segments(command)
        except ValueError:
            return False
        for segment in segments:
            tokens = ShellPolicyAnalyzer.unwrap_command(segment)
            if not tokens or Path(tokens[0]).name.lower() != "mv":
                continue
            args = [item for item in tokens[1:] if not item.startswith("-")]
            if len(args) != 2:
                return False
            source, target = args
            if not all(self._managed_container_path(path) for path in (source, target)):
                return False
            return True
        return False

    @classmethod
    def _browser_e2e_required(cls, request: ToolCallRequest) -> bool:
        context = cls._context(request)
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        if not session_id or not run_id:
            return False
        run = session_manager.get_run_state(session_id, run_id)
        contract = (
            run.get("verification_contract")
            if isinstance(run, dict)
            and isinstance(run.get("verification_contract"), dict)
            else {}
        )
        return bool(contract.get("browser_e2e_required"))

    @staticmethod
    def _managed_container_path(raw: str) -> bool:
        normalized = raw.replace("\\", "/")
        if any(char in normalized for char in "*?[") or ".." in Path(normalized).parts:
            return False
        if normalized.startswith("/"):
            return normalized == "/workspace" or normalized.startswith("/workspace/") or normalized == "/scratch" or normalized.startswith("/scratch/")
        return True

    @staticmethod
    def _control_descriptor(tool_name: str) -> dict[str, str] | None:
        descriptor = tool_control_descriptor(tool_name)
        return descriptor.as_dict() if descriptor is not None else None

    @staticmethod
    def _record_reviewer_decision(
        request: ToolCallRequest,
        result: ToolPolicyResult,
    ) -> None:
        if result.source != "codex_grok_smart_reviewer":
            return
        try:
            from graph.trace_collector import get_current_trace_collector

            collector = get_current_trace_collector()
            if collector is None:
                return
            collector.add_custom_span(
                "permission.smart_review",
                {
                    "tool_name": str(request.tool_call.get("name") or ""),
                    "decision": result.decision.value,
                    "reason": result.reason,
                    "risk": result.risk,
                    "explanation": result.explanation,
                },
                span_type="permission",
                metadata={
                    "permission": {
                        "outcome": result.decision.value,
                        "source": result.source,
                    }
                },
            )
        except Exception:
            return

    @staticmethod
    def _smart_fetch_candidate(request: ToolCallRequest) -> bool:
        args = request.tool_call.get("args") or {}
        raw_url = str(args.get("url") or "").strip()
        try:
            parsed = urlsplit(raw_url)
            port = parsed.port
        except ValueError:
            return False
        if parsed.scheme.lower() not in {"http", "https"}:
            return False
        if not parsed.hostname or parsed.username or parsed.password:
            return False
        try:
            literal = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            literal = None
        if literal is not None:
            if getattr(literal, "ipv4_mapped", None) is not None:
                literal = literal.ipv4_mapped
            if not literal.is_global:
                return False
        expected_port = 80 if parsed.scheme.lower() == "http" else 443
        return port in {None, expected_port}

    @staticmethod
    def _context(request: ToolCallRequest) -> dict[str, Any]:
        runtime = request.runtime
        context = runtime.context if runtime is not None else None
        return context if isinstance(context, dict) else {}

    @staticmethod
    def _command(request: ToolCallRequest) -> str:
        args = request.tool_call.get("args") or {}
        return str(args.get("command") or "")

    def _action_preview(self, request: ToolCallRequest) -> str:
        tool_name = str(request.tool_call.get("name") or "")
        if tool_name == "execute":
            return self._command(request)
        args = request.tool_call.get("args") or {}
        if tool_name in self.SKILL_COMMIT_TOOLS and self.base_dir is not None:
            try:
                from services.skill_management import get_skill_management_service

                plan = get_skill_management_service(self.base_dir).preview(str(args.get("plan_id") or ""))
            except Exception:
                plan = None
            if plan is not None:
                args = {"request": args, "verified_plan": plan}
        try:
            rendered = json.dumps(args, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            rendered = str(args)
        return rendered[:4000]

    def _skill_change_preview(self, request: ToolCallRequest) -> dict[str, str] | None:
        tool_name = str(request.tool_call.get("name") or "")
        args = request.tool_call.get("args") or {}
        if tool_name == "execute":
            managed_add = self._managed_npx_skills_add(self._command(request))
            if managed_add is not None:
                skill_names = ", ".join(managed_add.skill_names)
                return {
                    key: value
                    for key, value in {
                        "action": "prepare_install",
                        "skill_name": skill_names or "all discovered skills",
                        "source": managed_add.source,
                    }.items()
                    if value
                }
        if tool_name in {"prepare_skill_install", "prepare_skill_update"}:
            preview = {
                "action": "prepare_update" if tool_name.endswith("update") else "prepare_install",
                "skill_name": str(args.get("skill_name") or args.get("subpath") or ""),
                "source": str(args.get("source") or ""),
                "ref": str(args.get("ref") or ""),
                "subpath": str(args.get("subpath") or ""),
            }
            return {key: value for key, value in preview.items() if value}
        if tool_name not in self.SKILL_COMMIT_TOOLS or self.base_dir is None:
            return None
        try:
            from services.skill_management import get_skill_management_service

            plan = get_skill_management_service(self.base_dir).preview(str(args.get("plan_id") or ""))
        except Exception:
            plan = None
        if plan is None:
            return None
        diff = plan.get("diff") if isinstance(plan.get("diff"), dict) else {}
        metadata = plan.get("staged_metadata") if isinstance(plan.get("staged_metadata"), dict) else {}
        preview = {
            "action": str(plan.get("action") or ""),
            "skill_name": str(plan.get("skill_name") or ""),
            "source": str(plan.get("source") or ""),
            "version": str(metadata.get("version") or ""),
            "changes": str(diff.get("summary") or ""),
            "plan_sha256": str(plan.get("plan_sha256") or ""),
        }
        for key in ("added", "changed", "removed"):
            values = diff.get(key)
            if isinstance(values, list) and values:
                rendered = ", ".join(str(item) for item in values[:20])
                if len(values) > 20:
                    rendered += f" … (+{len(values) - 20})"
                preview[key] = rendered
        return {key: value for key, value in preview.items() if value}

    @classmethod
    def _required_capabilities(cls, request: ToolCallRequest) -> list[str]:
        tool_name = str(request.tool_call.get("name") or "")
        if tool_name == "execute" and cls._managed_npx_skills_add(cls._command(request)) is not None:
            return ["execute", "temporary_network"]
        if tool_name == "install_packages":
            return ["execute", "package_install", "temporary_network"]
        if tool_name in {"prepare_skill_install", "prepare_skill_update"}:
            return ["execute", "temporary_network"]
        if tool_name in cls.SKILL_COMMIT_TOOLS:
            return ["execute", "managed_skill_write"]
        if tool_name in {"fetch_url", "tavily_search"}:
            return ["execute", "network_access"]
        capabilities = ["execute"]
        if tool_name in {"execute", "execute_external_directory"}:
            context = cls._context(request)
            effects = ShellPolicyAnalyzer.capabilities(
                cls._command(request),
                workspace_path=(
                    "/external-workspace"
                    if tool_name == "execute_external_directory"
                    else str(context.get("workspace_path") or ".")
                ),
            )
            if tool_name == "execute_external_directory":
                capabilities.append("external_directory_mount")
            if effects.network:
                capabilities.append("network_access")
            if effects.workspace_write:
                capabilities.append("managed_write")
            if effects.package_install:
                capabilities.append("package_install")
            if effects.destructive:
                capabilities.append("destructive_write")
        return capabilities

    @staticmethod
    def _safe_external_argv_paths(tokens: list[str]) -> bool:
        for token in tokens[1:]:
            path_value = token.partition("=")[2] if "=" in token else token
            if ".." in Path(path_value).parts:
                return False
            if path_value.startswith("/") and not (
                path_value == "/external-workspace"
                or path_value.startswith("/external-workspace/")
                or path_value
                == "/opt/puddingclaw/bin/validate-html-report-e2e.mjs"
            ):
                return False
        return True

    @staticmethod
    def _single_external_argv(command: str) -> list[str] | None:
        """Return one expansion-free argv for narrow external-dir automation."""

        if any(character in command for character in ("$", "`", "\n", "\r", ">", "<")):
            return None
        try:
            segments = ShellPolicyAnalyzer.parse_segments(command)
        except ValueError:
            return None
        if len(segments) != 1:
            return None
        tokens = ShellPolicyAnalyzer.unwrap_command(segments[0])
        if not tokens:
            return None
        if not ToolExecutionPipeline._safe_external_argv_paths(tokens):
            return None
        return tokens

    @classmethod
    def _safe_external_draft_command(cls, command: str) -> bool:
        tokens = cls._single_external_argv(command)
        if not tokens:
            return False
        return Path(tokens[0]).name.lower() in {"cp", "mv", "mkdir"}

    @classmethod
    def _registered_validator_argv(cls, tokens: list[str]) -> bool:
        executable = Path(tokens[0]).name.lower()
        args = tokens[1:]
        if executable == "node":
            return (
                len(args) >= 2
                and args[0] == "--check"
            ) or (
                len(args) == 2
                and args[0]
                == "/opt/puddingclaw/bin/validate-html-report-e2e.mjs"
                and args[1].lower().endswith((".html", ".htm"))
            )
        if executable in {"python", "python3"}:
            return len(args) >= 3 and args[:2] in (
                ["-m", "py_compile"],
                ["-m", "json.tool"],
            )
        return False

    @classmethod
    def _registered_external_validator(cls, command: str) -> bool:
        tokens = cls._single_external_argv(command)
        if tokens:
            return cls._registered_validator_argv(tokens)

        # Low-friction path for a read-only validation plan such as:
        #   node --check a.js && echo "a ok" && node --check b.js
        # or the diagnostic wrapper emitted by older prompts:
        #   pwd && ls ... && node <fixed-html-validator> report.html
        #
        # Every meaningful command must be a registered validator.  We allow
        # only literal status output and narrow directory diagnostics around
        # those validators.  Expansions, fallback branches, pipes, writes,
        # background jobs, path escapes, and arbitrary helper commands remain
        # outside this deterministic allow path.
        if any(
            marker in command
            for marker in ("$", "`", "\n", "\r", ">", "<", "|", ";")
        ):
            return False
        if "&" in command.replace("&&", ""):
            return False
        try:
            segments = ShellPolicyAnalyzer.parse_segments(command)
        except ValueError:
            return False
        if len(segments) < 2 or "&&" not in command:
            return False
        argv_segments = [
            ShellPolicyAnalyzer.unwrap_command(segment)
            for segment in segments
        ]
        if any(not argv for argv in argv_segments):
            return False
        if any(
            not cls._safe_external_argv_paths(argv)
            for argv in argv_segments
        ):
            return False
        validator_count = 0
        for argv in argv_segments:
            if cls._registered_validator_argv(argv):
                validator_count += 1
                continue
            executable = Path(argv[0]).name.lower()
            if executable == "pwd" and len(argv) == 1:
                continue
            if executable == "echo":
                continue
            if executable != "ls":
                return False
            for token in argv[1:]:
                if token.startswith("-"):
                    continue
                if ".." in Path(token).parts:
                    return False
                if token.startswith("/") and not (
                    token == "/external-workspace"
                    or token.startswith("/external-workspace/")
                ):
                    return False
        return validator_count > 0

    def _session_grant_scope(
        self,
        request: ToolCallRequest,
    ) -> dict[str, str] | None:
        """Return the capability scope for a reusable Session approval."""

        tool_name = str(request.tool_call.get("name") or "")
        if tool_name in self.SKILL_COMMIT_TOOLS:
            # Skill writes are always bound to the exact immutable plan and
            # may never become reusable Session authority.
            return None
        if tool_name == "tavily_search":
            return {
                "target_kind": "network_profile",
                "target": "web_search:tavily",
                "label": "本 Session 允许 Tavily 网页搜索",
            }
        if tool_name == "fetch_url":
            args = request.tool_call.get("args") or {}
            origin = self._normalized_network_origin(str(args.get("url") or ""))
            if origin is None:
                return None
            host = urlsplit(origin).hostname or origin
            return {
                "target_kind": "network_origin",
                "target": origin,
                "label": f"本 Session 允许读取 {host}",
            }
        if tool_name == "install_packages":
            if not self.permission_context.smart or self.permission_context.backend_mode != "docker":
                return None
            return {
                "target_kind": "capability",
                "target": "docker_package_install",
                "label": "本 Session 允许在隔离安装器中安装 Skill 依赖",
            }
        if tool_name != "execute":
            return None

        # Raw shell commands have unrestricted bridge egress once approved.
        # Even a static GET can read workspace/HOME data through program flags
        # or a replaced executable, so shell networking is always exact-once.
        return None

    @staticmethod
    def _normalized_network_origin(raw_url: str) -> str | None:
        try:
            parsed = urlsplit(raw_url.strip())
            port = parsed.port
        except ValueError:
            return None
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        default_port = 443 if scheme == "https" else 80
        return f"{scheme}://{parsed.hostname.lower()}:{port or default_port}"

    @staticmethod
    def _denied_message(
        request: ToolCallRequest,
        result: ToolPolicyResult,
    ) -> ToolMessage:
        tool_name = str(request.tool_call.get("name") or "")
        return ToolMessage(
            content=(f"Tool `{tool_name}` was blocked by Harness policy: {result.reason} ({result.risk})."),
            tool_call_id=str(request.tool_call.get("id") or ""),
            name=tool_name,
            status="error",
        )
