"""Deterministic Tool execution policy and middleware.

The pipeline is the single pre-execution control point for Agent Tool calls.
It does not treat an execution runner as authorization: policy is evaluated
before both spawn and kernel execution. Managed Docker runtime calls are
separate internal compatibility surfaces.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import shlex
import stat
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest, hook_config
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command, interrupt

from graph.citations import dedupe_sources, materialize_artifact_citations
from graph.effective_grants import EffectiveGrantSet, SelectedGrantSet
from graph.host_read_policy import is_sensitive_host_read_path
from graph.kernel_fallback_resume import kernel_fallback_resume_registry
from graph.permission_policy import (
    PermissionRuleDecision,
    RunPermissionContext,
    evaluate_permission_rules,
)
from graph.permission_resume import permission_resume_registry
from graph.session_manager import session_manager
from graph.skill_plan_resume import skill_plan_resume_registry
from graph.virtual_paths import PathAuthority, classify_path_authority
from harness.execution_context import (
    AuthorizedBrowserAction,
    AuthorizedExecution,
    bind_authorized_browser_action,
    bind_authorized_execution,
    browser_action_digest,
)
from harness.execution_permits import ExecutionPermit
from harness.permission_reviewer import PermissionReviewer
from harness.sandbox_profiles import SandboxGrantProfile
from harness.shell_access import ShellAccessPlan
from runtime_identity.adapters import (
    ManagedCliRegistry,
    UnsupportedManagedCliCommand,
)
from tools.toolsets import tool_control_descriptor

logger = logging.getLogger(__name__)


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
class _ManagedCliPolicyProjection:
    adapter_id: str
    requires_network: bool
    workspace_writable: bool = False


@dataclass(frozen=True)
class FilesystemIntent:
    """One statically proven external filesystem effect."""

    path: str
    access: str


@dataclass(frozen=True)
class ExecutionRequirements:
    """Immutable command requirements shared by policy and runner routing."""

    capabilities: ShellCapabilities
    filesystem_intents: tuple[FilesystemIntent, ...] = ()
    shell_access_required: bool = False
    opaque: bool = False
    opaque_reason: str = ""
    execution_command: str = ""
    external_path_candidates: tuple[str, ...] = ()
    environment_binding_digest: str = ""

    @property
    def digest(self) -> str:
        payload = {
            "capabilities": {
                "network": self.capabilities.network,
                "workspace_write": self.capabilities.workspace_write,
                "package_install": self.capabilities.package_install,
                "destructive": self.capabilities.destructive,
            },
            "filesystem_intents": [
                {"path": intent.path, "access": intent.access} for intent in self.filesystem_intents
            ],
            "shell_access_required": self.shell_access_required,
            "opaque": self.opaque,
            "opaque_reason": self.opaque_reason,
            "execution_command": self.execution_command,
            "external_path_candidates": list(self.external_path_candidates),
            "environment_binding_digest": self.environment_binding_digest,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


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
        "ln",
        "patch",
        "rsync",
        "unlink",
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
        "id",
        "whoami",
        "uname",
        "date",
        "printf",
        "echo",
        "sort",
        "uniq",
        "cut",
        "tr",
        "awk",
        "jq",
    }
)
_WRAPPERS = frozenset({"command", "env", "timeout", "gtimeout", "nice", "nohup", "time", "builtin"})
_SHELLS = frozenset({"sh", "bash", "zsh"})
_SHELL_META_PATTERN = re.compile(r"(`|\$\(|\$\{|[<>]\(|\n|<<)")
_ENV_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.DOTALL)
_CRITICAL_EXECUTION_ENV_OVERRIDE_PATTERN = re.compile(
    r"(?:^|[;&|()\s])(?:env\s+)?(?:PATH|LD_PRELOAD|DYLD_INSERT_LIBRARIES|"
    r"PYTHONPATH|PYTHONSTARTUP|NODE_OPTIONS|RUBYOPT|PERL5OPT|BASH_ENV|ENV)\s*=",
    re.IGNORECASE,
)
_PERSISTENCE_TARGET_PATTERN = re.compile(
    r"(?i)(?:^|[/\\s'\"])(?:\.zshrc|\.bashrc|\.bash_profile|\.profile|"
    r"\.config/autostart/|library/launchagents/|library/launchdaemons/|"
    r"\.ssh/|\.aws/(?:credentials|config)|\.config/gcloud/|\.kube/config|"
    r"authorized_keys|crontab)(?:[/\\s'\";&|()<>]|$)"
)
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
    r"(?:fs(?:\.promises)?\.)?(?:writeFile|appendFile|copyFile|unlink|rename|mkdir)(?:Sync)?\s*\()",
    re.IGNORECASE,
)
_KNOWN_NETWORK_SKILL_ENTRYPOINT_PATTERN = re.compile(
    r"(?:python3?|node)\s+/skills/aihot/[^\s\"']+",
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
_OPAQUE_DYNAMIC_CODE_PATTERN = re.compile(r"\b__import__\s*\(", re.IGNORECASE)
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

    def __init__(
        self,
        *,
        workspace_path: str,
        backend_mode: str,
        filesystem_mode: str = "restricted",
        allowed_external_paths: tuple[str | Path, ...] = (),
        path_resolver: Callable[[str], str | Path] | None = None,
    ) -> None:
        self.workspace_path = Path(workspace_path).expanduser().resolve()
        self.backend_mode = backend_mode
        self.filesystem_mode = filesystem_mode
        self.allowed_external_paths = tuple(
            Path(path).expanduser().resolve(strict=False) for path in allowed_external_paths if str(path)
        )
        self.path_resolver = path_resolver

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
                and any(source == "/workspace" or source.startswith("/workspace/") for source in operands[:-1])
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
    def requirements(
        cls,
        command: str,
        *,
        workspace_path: str | Path,
        path_resolver: Callable[[str], str | Path] | None = None,
    ) -> ExecutionRequirements:
        """Return a fail-closed, runner-neutral execution description.

        Safe filesystem segments joined by ``&&`` are authorized atomically.
        Their path operands are canonicalized once here so the Grant Profile,
        macOS Seatbelt command and Docker bind command all describe the same
        host objects. Unsupported shell grammar remains opaque.
        """

        capabilities = cls.capabilities(command, workspace_path=workspace_path)
        try:
            segments, operators, has_redirect = cls._requirements_structure(command)
        except ValueError:
            return ExecutionRequirements(
                capabilities=capabilities,
                opaque=True,
                opaque_reason="shell_parse_failed",
            )
        external_candidates = cls._external_path_candidates(
            segments,
            workspace_path=workspace_path,
            path_resolver=path_resolver,
        )
        if has_redirect:
            return ExecutionRequirements(
                capabilities=capabilities,
                opaque=True,
                opaque_reason="shell_redirection",
                external_path_candidates=external_candidates,
            )
        if any(operator != "&&" for operator in operators):
            return ExecutionRequirements(
                capabilities=capabilities,
                opaque=True,
                opaque_reason="unsupported_shell_operator",
                external_path_candidates=external_candidates,
            )
        if _SHELL_META_PATTERN.search(command):
            return ExecutionRequirements(
                capabilities=capabilities,
                opaque=True,
                opaque_reason="expanding_shell",
                external_path_candidates=external_candidates,
            )

        external: list[FilesystemIntent] = []
        normalized_segments: list[str] = []
        for raw_segment in segments:
            tokens = cls._unwrap(raw_segment)
            if not tokens:
                return ExecutionRequirements(
                    capabilities=capabilities,
                    opaque=True,
                    opaque_reason="empty_after_unwrap",
                    external_path_candidates=external_candidates,
                )
            if tokens != raw_segment:
                return ExecutionRequirements(
                    capabilities=capabilities,
                    opaque=True,
                    opaque_reason="unsupported_command_wrapper",
                    external_path_candidates=external_candidates,
                )
            parsed = cls._supported_filesystem_segment(tokens)
            if isinstance(parsed, str):
                return ExecutionRequirements(
                    capabilities=capabilities,
                    opaque=True,
                    opaque_reason=parsed,
                    external_path_candidates=external_candidates,
                )
            operand_accesses, operand_indexes = parsed
            normalized_tokens = list(tokens)
            for (raw_path, access), operand_index in zip(
                operand_accesses,
                operand_indexes,
                strict=True,
            ):
                resolved_path = str(path_resolver(raw_path)) if path_resolver is not None else raw_path
                classified = classify_path_authority(
                    resolved_path,
                    workspace_root=workspace_path,
                )
                if classified.authority is PathAuthority.ESCAPE:
                    return ExecutionRequirements(
                        capabilities=capabilities,
                        opaque=True,
                        opaque_reason="path_escape",
                        external_path_candidates=external_candidates,
                    )
                if classified.authority is not PathAuthority.EXTERNAL:
                    continue
                canonical = classified.canonical_host_path
                if canonical is None:
                    return ExecutionRequirements(
                        capabilities=capabilities,
                        opaque=True,
                        opaque_reason="external_path_not_canonical",
                        external_path_candidates=external_candidates,
                    )
                canonical_text = str(canonical)
                if resolved_path == raw_path:
                    normalized_tokens[operand_index] = canonical_text
                external.append(FilesystemIntent(path=canonical_text, access=access))
            normalized_segments.append(shlex.join(normalized_tokens))
        return ExecutionRequirements(
            capabilities=capabilities,
            filesystem_intents=tuple(external),
            shell_access_required=bool(external),
            execution_command=" && ".join(normalized_segments),
        )

    @classmethod
    def _external_path_candidates(
        cls,
        segments: list[list[str]],
        *,
        workspace_path: str | Path,
        path_resolver: Callable[[str], str | Path] | None = None,
    ) -> tuple[str, ...]:
        candidates: list[str] = []
        for segment in segments:
            for token in segment:
                for raw_path in cls._absolute_path_fragments(token):
                    # File-descriptor sinks such as ``2>/dev/null`` do not
                    # expose host data or grant access to a host directory.
                    # Treating them as external paths makes an otherwise
                    # sandboxed Python/Node script request directory HITL
                    # before SMART-mode execution policy can approve it.
                    if raw_path in _NON_MATERIAL_REDIRECT_SINKS:
                        continue
                    resolved_path = str(path_resolver(raw_path)) if path_resolver is not None else raw_path
                    classified = classify_path_authority(
                        resolved_path,
                        workspace_root=workspace_path,
                    )
                    if classified.authority is PathAuthority.EXTERNAL and classified.canonical_host_path is not None:
                        candidates.append(str(classified.canonical_host_path))
        return tuple(dict.fromkeys(candidates))

    @staticmethod
    def _supported_filesystem_segment(
        tokens: list[str],
    ) -> tuple[list[tuple[str, str]], list[int]] | str:
        """Parse only path operands whose authority can be proven exactly."""

        executable = Path(tokens[0]).name.lower()
        script_interpreters = {
            "python",
            "python3",
            "node",
            "ruby",
            "perl",
            "php",
            "sh",
            "bash",
            "zsh",
        }
        if executable in script_interpreters:
            # Script entry points have one statically provable host dependency:
            # the interpreter must read the script. Arguments remain ordinary
            # argv; any additional authorized directory access is constrained
            # by the active SandboxGrantProfile rather than inferred here.
            inline_flags = {"-c", "-e", "--eval", "-m"}
            args = tokens[1:]
            if not args or any(
                arg in inline_flags or any(arg.startswith(f"{flag}=") for flag in inline_flags if flag.startswith("--"))
                for arg in args
            ):
                return "unsupported_command_grammar"
            script_index = next(
                (index for index, arg in enumerate(tokens[1:], start=1) if not arg.startswith("-")),
                None,
            )
            if script_index is None or tokens[script_index] == "-":
                return "unsupported_command_grammar"
            # We can prove that the interpreter reads its entry script, but an
            # additional absolute path may be an input, output, socket, or some
            # application-specific target.  Leave that form opaque so the
            # authority preflight derives a conservative directory prompt for
            # every external path instead of silently omitting one.
            if any(Path(arg).expanduser().is_absolute() for arg in tokens[script_index + 1 :]):
                return "unsupported_command_grammar"
            return ([(tokens[script_index], "read")], [script_index])
        short_options = {
            "cp": frozenset("Rrapfnv"),
            "mv": frozenset("fnv"),
            "mkdir": frozenset("pv"),
            "ls": frozenset("laAhCFRStux1dnpq"),
        }
        long_options = {
            "cp": frozenset({"--archive", "--recursive", "--preserve", "--force", "--no-clobber", "--verbose"}),
            "mv": frozenset({"--force", "--no-clobber", "--verbose"}),
            "mkdir": frozenset({"--parents", "--verbose"}),
            "ls": frozenset(
                {
                    "--all",
                    "--almost-all",
                    "--long",
                    "--classify",
                    "--directory",
                    "--inode",
                    "--human-readable",
                    "--reverse",
                    "--recursive",
                }
            ),
        }
        if executable not in short_options:
            return "unsupported_command_grammar"
        operands: list[tuple[int, str]] = []
        options_done = False
        for index, token in enumerate(tokens[1:], start=1):
            if not options_done and token == "--":
                options_done = True
                continue
            if not options_done and token.startswith("--"):
                option = token.split("=", 1)[0]
                if option not in long_options[executable] or "=" in token:
                    return "unsupported_command_option"
                continue
            if not options_done and token.startswith("-") and token != "-":
                if not token[1:] or not set(token[1:]).issubset(short_options[executable]):
                    return "unsupported_command_option"
                continue
            operands.append((index, token))
        if executable in {"cp", "mv"} and len(operands) < 2:
            return "invalid_operand_count"
        if executable in {"mkdir", "ls"} and not operands:
            # ``ls`` without a path is a workspace-only command and still has
            # a complete description; only mkdir requires a target.
            if executable == "mkdir":
                return "invalid_operand_count"
            return ([], [])

        accesses: list[tuple[str, str]] = []
        indexes: list[int] = []
        if executable == "cp":
            for index, source in operands[:-1]:
                accesses.append((source, "read"))
                indexes.append(index)
            accesses.append((operands[-1][1], "write"))
            indexes.append(operands[-1][0])
        elif executable == "mv":
            for index, source in operands[:-1]:
                accesses.extend(((source, "read"), (source, "delete")))
                indexes.extend((index, index))
            accesses.append((operands[-1][1], "write"))
            indexes.append(operands[-1][0])
        elif executable == "mkdir":
            for index, target in operands:
                accesses.append((target, "write"))
                indexes.append(index)
        else:
            for index, target in operands:
                accesses.append((target, "read"))
                indexes.append(index)
        return accesses, indexes

    @staticmethod
    def _requirements_structure(
        command: str,
    ) -> tuple[list[list[str]], list[str], bool]:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
        segments: list[list[str]] = []
        operators: list[str] = []
        current: list[str] = []
        has_redirect = False
        for token in tokens:
            if token in {"&&", "||", ";", "|", "&"}:
                if not current:
                    raise ValueError("dangling shell operator")
                segments.append(current)
                current = []
                operators.append(token)
                continue
            if token and set(token).issubset({">", "<"}):
                has_redirect = True
            current.append(token)
        if not current:
            if operators:
                raise ValueError("dangling shell operator")
        else:
            segments.append(current)
        if operators and len(operators) != len(segments) - 1:
            raise ValueError("invalid shell structure")
        return segments, operators, has_redirect

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
    def _managed_cli_match(tokens: list[str]):
        """Classify built-in managed CLIs through the same Adapter boundary."""

        registry = ManagedCliRegistry()
        surface = shlex.join(tokens)
        try:
            return registry.match(surface)
        except UnsupportedManagedCliCommand:
            if registry.claims(surface):
                # Claimed-but-invalid managed syntax must never look like an
                # offline opaque command to the legacy shell analyzer. The
                # managed preflight will reject it at the authoritative edge;
                # this conservative projection only prevents policy downgrade.
                return _ManagedCliPolicyProjection(
                    adapter_id=Path(tokens[0]).name.lower(),
                    requires_network=True,
                )
            return None

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
        managed_match = cls._managed_cli_match(tokens)
        if managed_match is not None:
            lowered = [item.lower() for item in tokens[1:]]
            effect = "auth" if lowered[:1] in (["auth"], ["config"]) else "unknown"
            return NetworkIntent(
                required=managed_match.requires_network,
                target_known=False,
                remote_effect=effect,
                transport_profile=f"declared_cli:{managed_match.adapter_id}",
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
            "-d",
            "--data",
            "--data-ascii",
            "--data-binary",
            "--data-raw",
            "--data-urlencode",
            "-f",
            "--form",
            "--form-string",
            "-t",
            "--upload-file",
            "--json",
        }
        remote_effect = (
            "mutate"
            if any(
                item in mutating_flags
                or item.startswith(tuple(f"{flag}=" for flag in mutating_flags if flag.startswith("--")))
                for item in lowered
            )
            else "read"
        )
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
                if index + 1 >= len(tokens) or tokens[index + 1] not in _NON_MATERIAL_REDIRECT_SINKS:
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
            if executable == "xargs":
                nested_tokens = cls._xargs_command(tokens[1:])
                if nested_tokens:
                    nested = cls.capabilities(
                        shlex.join(nested_tokens),
                        workspace_path=workspace_path,
                        _seen_scripts=_seen_scripts,
                    )
                    network = network or nested.network
                    workspace_write = workspace_write or nested.workspace_write
                    package_install = package_install or nested.package_install
                    destructive = destructive or nested.destructive
                continue
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
                if executable == "wget" or (executable == "curl" and cls._curl_writes_material_output(tokens)):
                    workspace_write = True
            if executable == "rsync":
                remote_operands = [item for item in tokens[1:] if not item.startswith("-")]
                if any(
                    item.lower().startswith("rsync://") or re.match(r"^(?:[^/@\s]+@)?[^/:\s]+:.+", item)
                    for item in remote_operands
                ):
                    network = True
                if any(item == "--delete" or item.startswith("--delete-") for item in args):
                    destructive = True
            managed_match = cls._managed_cli_match(tokens)
            if managed_match is not None:
                network = network or managed_match.requires_network
                workspace_write = workspace_write or managed_match.workspace_writable
            if executable in _DESTRUCTIVE_OR_WRITE_COMMANDS:
                workspace_write = True
            if executable == "rm" and any(
                arg in _RECURSIVE_RM_FLAGS or arg.startswith("--recursive=") or (arg.startswith("-") and "r" in arg[1:])
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
            if executable == "tar" and any(
                item in {"-x", "--extract", "--get", "-c", "--create", "-r", "--append", "-u", "--update"}
                or (item.startswith("-") and not item.startswith("--") and any(flag in item[1:] for flag in "xcru"))
                for item in args
            ):
                workspace_write = True
            if executable == "unzip" and not any(item in {"-l", "-t", "-p", "-c", "-z", "-v"} for item in args):
                workspace_write = True
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
                executable in {"npx", "uvx"} or normalized_subcommand in {"ci", "install", "add", "remove", "uninstall"}
            ):
                network = True
                package_install = True
                workspace_write = True
            if (
                executable in {"python", "python3"}
                and args[:2] == ["-m", "pip"]
                and any(item in package_subcommands for item in args[2:])
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
                or any(item.startswith("/skills/aihot/") for item in args)
            ):
                network = True
            if (
                executable in {"python", "python3", "node", "ruby", "perl", "php"}
                and (
                    _EMBEDDED_WRITE_API_PATTERN.search(joined_args)
                    or any(
                        not item.startswith("-")
                        and item.lower().endswith((".py", ".js", ".mjs", ".cjs", ".rb", ".pl", ".php"))
                        for item in tokens[1:]
                    )
                )
                and not (executable == "node" and args[:1] == ["--check"])
            ):
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
            line.strip() for line in content.splitlines() if line.strip() and not line.lstrip().startswith("#")
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
                    if self.backend_mode != "spawn" or self.filesystem_mode == "unrestricted":
                        continue
                    if raw == "/workspace" or raw.startswith("/workspace/"):
                        continue
                    if raw == "/scratch" or raw.startswith("/scratch/"):
                        continue
                    resolved_raw = str(self.path_resolver(raw)) if self.path_resolver is not None else raw
                    resolved = Path(resolved_raw).expanduser()
                    try:
                        resolved = resolved.resolve()
                        resolved.relative_to(self.workspace_path)
                    except (OSError, ValueError):
                        if self._external_path_allowed(resolved):
                            continue
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
        terminators = set('\t\r\n,;|&<>)]}"')
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
                expansion_syntax = "harness-scrat" in raw or (
                    not inline_program and ("*" in raw or "?" in raw or "[" in raw)
                )
                if (
                    self.backend_mode == "docker"
                    and expansion_syntax
                    and ("/" in raw or "harness-scrat" in raw or raw.startswith(("*", "?", ".", "~")))
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
                if self.backend_mode != "spawn" or self.filesystem_mode == "unrestricted":
                    continue
                if raw == "/workspace":
                    candidate = self.workspace_path
                elif raw.startswith("/workspace/"):
                    candidate = self.workspace_path / raw.removeprefix("/workspace/")
                elif raw == "/scratch" or raw.startswith("/scratch/"):
                    continue
                elif raw.startswith("~/"):
                    resolved_raw = str(self.path_resolver(raw)) if self.path_resolver is not None else raw
                    candidate = Path(resolved_raw).expanduser()
                elif Path(raw).is_absolute():
                    resolved_raw = str(self.path_resolver(raw)) if self.path_resolver is not None else raw
                    candidate = Path(resolved_raw)
                else:
                    candidate = self.workspace_path / raw
                resolved_candidate = candidate
                try:
                    resolved_candidate = resolved_candidate.resolve()
                    resolved_candidate.relative_to(self.workspace_path)
                except (OSError, ValueError):
                    if self._external_path_allowed(resolved_candidate):
                        continue
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

    def _external_path_allowed(self, path: str | Path) -> bool:
        if not self.allowed_external_paths:
            return False
        try:
            candidate = Path(path).expanduser().resolve(strict=False)
        except OSError:
            return False
        for allowed in self.allowed_external_paths:
            if candidate == allowed:
                return True
            try:
                candidate.relative_to(allowed)
                return True
            except ValueError:
                continue
        return False

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
                    if not duplicates_fd and target not in _NON_MATERIAL_REDIRECT_SINKS:
                        has_write_redirect = True
                continue
            current.append(token)
        if current:
            segments.append(current)
        return segments, has_write_redirect

    def _analyze_segment(self, tokens: list[str]) -> ToolPolicyResult:
        raw_tokens = list(tokens)
        tokens = self._unwrap(tokens)
        if not tokens:
            if raw_tokens:
                return ToolPolicyResult(PolicyDecision.ALLOW, "shell_control", "low")
            return ToolPolicyResult(PolicyDecision.ASK, "wrapper_without_command", "high")
        command = Path(tokens[0]).name.lower()
        args = tokens[1:]

        if command in _HARD_DENY_COMMANDS:
            return ToolPolicyResult(
                PolicyDecision.DENY,
                f"hard_denied_command:{command}",
                "critical",
            )
        if command in {"eval", "source", "."}:
            return ToolPolicyResult(
                PolicyDecision.ASK,
                f"dynamic_shell_execution:{command}",
                "high",
            )
        if command == "xargs":
            nested = self._xargs_command(args)
            if nested:
                return self._analyze_segment(nested)
            return ToolPolicyResult(PolicyDecision.ALLOW, "safe_read", "low")
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
        managed_match = self._managed_cli_match(tokens)
        if managed_match is not None:
            if not managed_match.requires_network:
                return ToolPolicyResult(
                    PolicyDecision.ALLOW,
                    f"declared_cli_local_inspection:{managed_match.adapter_id}",
                    "low",
                )
            return ToolPolicyResult(
                PolicyDecision.ASK,
                f"network_access:{managed_match.adapter_id}",
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
                arg in _RECURSIVE_RM_FLAGS or arg.startswith("--recursive=") or (arg.startswith("-") and "r" in arg[1:])
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
        if command == "tar" and any(
            arg in {"-x", "--extract", "--get", "-c", "--create", "-r", "--append", "-u", "--update"}
            or (arg.startswith("-") and not arg.startswith("--") and any(flag in arg[1:] for flag in "xcru"))
            for arg in args
        ):
            return ToolPolicyResult(PolicyDecision.ASK, "managed_workspace_write:tar", "managed_write")
        if command == "unzip" and not any(arg.lower() in {"-l", "-t", "-p", "-c", "-z", "-v"} for arg in args):
            return ToolPolicyResult(PolicyDecision.ASK, "managed_workspace_write:unzip", "managed_write")
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
        # shlex does not classify grouping punctuation unless it is included
        # in ``punctuation_chars``.  Without normalizing it here, commands such
        # as ``( rm -rf path )`` and ``{ chmod -R ...; }`` look like unknown
        # executables and can lose their real effect classification.
        while remaining and remaining[0] in {"(", "{"}:
            remaining.pop(0)
        while remaining and remaining[-1] in {
            ")",
            "}",
        }:
            remaining.pop()
        control_prefixes = {"!", "if", "then", "elif", "else", "while", "until", "do"}
        while remaining and remaining[0].lower() in control_prefixes:
            remaining.pop(0)
        if remaining and remaining[0].lower() in {"fi", "done", "esac"}:
            return []
        if remaining and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\(\)\{?", remaining[0]):
            remaining.pop(0)
        if remaining and remaining[0].lower() == "case":
            pattern_index = next(
                (index for index, item in enumerate(remaining[1:], start=1) if item.endswith(")")),
                None,
            )
            if pattern_index is None:
                return []
            remaining = remaining[pattern_index + 1 :]
        elif len(remaining) > 1 and remaining[0].endswith(")"):
            # A subsequent case arm begins with its ``pattern)`` token.
            remaining.pop(0)
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
    def _xargs_command(args: list[str]) -> list[str]:
        """Return the explicit command invoked by xargs, if one is present."""

        value_options = {
            "-a",
            "--arg-file",
            "-E",
            "--eof",
            "-I",
            "--replace",
            "-L",
            "--max-lines",
            "-n",
            "--max-args",
            "-P",
            "--max-procs",
            "-s",
            "--max-chars",
        }
        index = 0
        while index < len(args):
            item = args[index]
            if item == "--":
                return args[index + 1 :]
            if item in value_options:
                index += 2
                continue
            if any(item.startswith(f"{option}=") for option in value_options if option.startswith("--")):
                index += 1
                continue
            if item.startswith("-"):
                index += 1
                continue
            return args[index:]
        return []

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
            "database_evidence_search",
            "database_sql_generate",
            "database_sql_validate_legacy",
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
    NETWORK_TOOLS = frozenset(
        {
            "web_search",
            "tavily_search",
            "fetch_url",
            "prepare_skill_install",
            "prepare_skill_update",
        }
    )
    SKILL_COMMIT_TOOLS = frozenset({"install_skill", "update_skill"})
    SEMANTIC_COMMIT_TOOLS = frozenset({"publish_semantic_markdown"})

    def __init__(
        self,
        *,
        known_tools: set[str],
        mcp_tool_names: set[str] | None = None,
        backend_mode: str,
        permission_context: RunPermissionContext | None = None,
        base_dir: Path | None = None,
        reviewer: PermissionReviewer | None = None,
        workspace_backend: Any | None = None,
        managed_cli_service: Any | None = None,
    ) -> None:
        self.known_tools = set(known_tools) | set(self.BUILTIN_TOOLS)
        self.mcp_tool_names = frozenset(str(name) for name in (mcp_tool_names or ()) if str(name))
        self.backend_mode = backend_mode
        self.base_dir = base_dir.expanduser().resolve() if base_dir is not None else None
        self.reviewer = reviewer
        self.workspace_backend = workspace_backend
        self.managed_cli_registry = ManagedCliRegistry()
        # Managed provider/browser CLIs are a separate control-plane surface.
        # Do not infer or construct one from the ordinary shell backend: doing
        # so couples user-selected spawn/kernel execution to Docker-backed
        # credentials and runtime mounts. Callers that explicitly own the
        # managed runtime may inject a service here.
        self.managed_cli_service = managed_cli_service
        self.permission_context = permission_context or RunPermissionContext.from_config_snapshot(
            {
                "permissions": {"approval_mode": "smart"},
                "execution": {"backend_mode": backend_mode},
            }
        )

    def _effective_backend_mode(self) -> str:
        """Return the runner actually selected after an explicit fallback."""

        effective = getattr(self.workspace_backend, "effective_mode", None)
        if isinstance(effective, str) and effective in {"spawn", "kernel"}:
            return effective
        return self.backend_mode

    @property
    def _smart_local_filesystem_unrestricted(self) -> bool:
        """Whether ordinary host paths are intentionally outside Harness roots."""

        return bool(
            self.permission_context.smart
            and self.permission_context.filesystem_mode == "unrestricted"
            and self._effective_backend_mode() in {"spawn", "kernel"}
        )

    @property
    def _filesystem_mode(self) -> str:
        return "unrestricted" if self._smart_local_filesystem_unrestricted else "restricted"

    def _policy_command(self, command: str) -> str:
        """Return the backend-canonical command used for policy and execution.

        A backend may expose a private in-sandbox alias for the same public
        workspace (for example SWE-bench ``/testbed`` → ``/workspace``).  The
        canonicalizer is backend-owned and runs before path classification, so
        aliases cannot manufacture host authority or bypass the permit bound
        to the original tool call.
        """

        normalize = getattr(self.workspace_backend, "normalize_execution_command", None)
        if not callable(normalize):
            return command
        normalized = normalize(command)
        if not isinstance(normalized, str) or (command.strip() and not normalized.strip()):
            raise ValueError("Workspace backend returned an invalid canonical execution command")
        return normalized

    def _execution_path_resolver(self) -> Callable[[str], str | Path] | None:
        resolver = getattr(self.workspace_backend, "resolve_execution_path", None)
        return resolver if callable(resolver) else None

    def _filesystem_roots(self, access: str) -> tuple[Path, ...]:
        attribute = {
            "read": "filesystem_read_roots",
            "write": "filesystem_write_roots",
            "delete": "filesystem_delete_roots",
        }.get(access)
        if attribute is None or self.workspace_backend is None:
            return ()
        return tuple(
            Path(root).expanduser().resolve(strict=False)
            for root in (getattr(self.workspace_backend, attribute, ()) or ())
        )

    @staticmethod
    def _covered_by_roots(path: Path, roots: tuple[Path, ...]) -> bool:
        for root in roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _baseline_filesystem_access(self, intent: FilesystemIntent) -> bool | None:
        """Return allow/deny only for paths owned by the runner grant profile."""

        if self._smart_local_filesystem_unrestricted:
            return True

        candidate = Path(intent.path).expanduser().resolve(strict=False)
        required_roots = self._filesystem_roots(intent.access)
        if self._covered_by_roots(candidate, required_roots):
            return True
        all_roots = tuple(
            dict.fromkeys(
                (
                    *self._filesystem_roots("read"),
                    *self._filesystem_roots("write"),
                    *self._filesystem_roots("delete"),
                )
            )
        )
        return False if self._covered_by_roots(candidate, all_roots) else None

    def _baseline_filesystem_violation(
        self,
        requirements: ExecutionRequirements,
    ) -> FilesystemIntent | None:
        intents = requirements.filesystem_intents
        if requirements.opaque:
            access = (
                "delete"
                if requirements.capabilities.destructive
                else "write"
                if requirements.capabilities.workspace_write
                else "read"
            )
            intents = tuple(
                FilesystemIntent(path=path, access=access) for path in requirements.external_path_candidates
            )
        return next(
            (intent for intent in intents if self._baseline_filesystem_access(intent) is False),
            None,
        )

    def _execution_requirements(
        self,
        command: str,
        *,
        workspace_path: str | Path,
    ) -> ExecutionRequirements:
        policy_command = self._policy_command(command)
        requirements = ShellPolicyAnalyzer.requirements(
            policy_command,
            workspace_path=workspace_path,
            path_resolver=self._execution_path_resolver(),
        )
        # Opaque shell grammar intentionally has no analyzer-produced command,
        # but a backend workspace alias must still be canonical at the actual
        # spawn boundary.  Preserve any stronger operand canonicalization the
        # analyzer already produced; otherwise bind the backend-normalized
        # command into the immutable requirements digest.
        if policy_command != command and not requirements.execution_command:
            requirements = replace(requirements, execution_command=policy_command)
        return requirements

    @staticmethod
    def _kernel_fallback_error(request: ToolCallRequest, *, reason: str) -> ToolMessage:
        return ToolMessage(
            content=json.dumps(
                {
                    "ok": False,
                    "error": "kernel_execution_unavailable",
                    "message": reason,
                    "requires_explicit_fallback": True,
                    "tool_call_id": str(request.tool_call.get("id") or ""),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            name=str(request.tool_call.get("name") or "execute"),
            tool_call_id=str(request.tool_call.get("id") or ""),
            status="error",
        )

    def _kernel_fallback_reason(self) -> str:
        reason = str(getattr(self.workspace_backend, "fallback_reason", "") or "").strip()
        return reason or "当前主机未通过 Kernel 沙箱可用性检查。"

    async def _ensure_execution_backend(self, request: ToolCallRequest) -> ToolMessage | None:
        if (
            str(request.tool_call.get("name") or "") != "execute"
            or self._effective_backend_mode() != "kernel"
            or not bool(getattr(self.workspace_backend, "kernel_unavailable", False))
        ):
            return None
        context = self._context(request)
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        if not session_id or not run_id:
            return self._kernel_fallback_error(request, reason=self._kernel_fallback_reason())
        probe_fingerprint = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    {
                        "backend": str(getattr(self.workspace_backend, "id", "")),
                        "reason": self._kernel_fallback_reason(),
                        "runner": str(getattr(self.workspace_backend, "kernel_runner_mode", "")),
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
        )
        request_payload = kernel_fallback_resume_registry.create(
            session_id=session_id,
            run_id=run_id,
            query_id=str(context.get("query_id") or ""),
            goal_id=str(context.get("goal_id") or ""),
            goal_revision=context.get("goal_revision"),
            project_id=(str(context.get("project_id")) if context.get("project_id") else None),
            tool_call_id=str(request.tool_call.get("id") or ""),
            workspace_identity="sha256:"
            + hashlib.sha256(str(context.get("workspace_path") or "").encode("utf-8")).hexdigest(),
            configured_mode="kernel",
            availability_class="stable" if "unsupported" in self._kernel_fallback_reason().lower() else "transient",
            reason_code="probe_failed",
            reason=self._kernel_fallback_reason(),
            probe_fingerprint=probe_fingerprint,
        )
        decision = interrupt(
            {
                "type": "kernel_fallback_request",
                "request": request_payload,
                "decisions": [
                    {"action": "switch_project_to_spawn"},
                    {"action": "fallback_once"},
                    {"action": "reject"},
                ],
            }
        )
        action = str(decision.get("action") or "") if isinstance(decision, dict) else ""
        if action in {"switch_project_to_spawn", "fallback_once"}:
            if action == "switch_project_to_spawn" and context.get("project_id"):
                from projects.registry import project_registry

                project_registry.set_execution_mode(str(context["project_id"]), "spawn")
            session_manager.record_run_execution_fallback(
                session_id,
                run_id,
                scope="project" if action == "switch_project_to_spawn" else "run",
                request=request_payload,
            )
            activate = getattr(self.workspace_backend, "activate_spawn", None)
            if not callable(activate):
                return self._kernel_fallback_error(request, reason="Kernel fallback target is unavailable.")
            activate()
            return None
        return self._kernel_fallback_error(request, reason="用户拒绝将本次 Kernel 执行切换为宿主执行。")

    @staticmethod
    def _permission_resume_decision(value: Any) -> Any:
        """Normalize LangGraph's permission resume envelope.

        Permission interrupts use the standard ``{"decisions": [...]}``
        payload when resumed through the streaming coordinator. Direct unit
        callers historically returned the sole decision object itself. Both
        forms describe the same one-action approval and must reach the exact
        suspended middleware frame with identical semantics.
        """

        if not isinstance(value, dict) or "decisions" not in value:
            return value
        decisions = value.get("decisions")
        if not isinstance(decisions, list) or len(decisions) != 1 or not isinstance(decisions[0], dict):
            return value
        return decisions[0]

    def _external_authority_requirements(
        self,
        requirements: ExecutionRequirements,
    ) -> ExecutionRequirements:
        """Turn opaque path candidates into a conservative permission request.

        This description is used only to ask for missing directory authority;
        it is never substituted for the original opaque execution permit.
        Parsers improve the requested access level, while the OS sandbox still
        enforces the Grant Profile for commands the parser does not understand.
        """

        if requirements.opaque:
            if requirements.capabilities.destructive:
                access = "delete"
            elif requirements.capabilities.workspace_write:
                access = "write"
            else:
                access = "read"
            intents = tuple(
                FilesystemIntent(path=path, access=access) for path in requirements.external_path_candidates
            )
        else:
            intents = requirements.filesystem_intents
        external = tuple(intent for intent in intents if self._baseline_filesystem_access(intent) is None)
        return ExecutionRequirements(
            capabilities=requirements.capabilities,
            filesystem_intents=external,
            shell_access_required=bool(external),
            external_path_candidates=requirements.external_path_candidates,
        )

    def _spawn_read_only_external_paths(
        self,
        *,
        command: str,
        workspace_path: str,
    ) -> tuple[str, ...]:
        """Return host paths whose only detected effect is reading in Spawn.

        Spawn intentionally carries the desktop user's host authority.  The
        Tool Gate still blocks network, package, write, and destructive
        effects, but an external pathname alone must not manufacture a second
        approval for ordinary inspection or a read-only Skill transform.
        """

        if self._effective_backend_mode() != "spawn":
            return ()
        requirements = self._execution_requirements(
            command,
            workspace_path=workspace_path,
        )
        authority = self._external_authority_requirements(requirements)
        effects = requirements.capabilities
        if (
            not authority.filesystem_intents
            or effects.network
            or effects.workspace_write
            or effects.package_install
            or effects.destructive
            or any(intent.access != "read" for intent in authority.filesystem_intents)
            or not self._provable_spawn_read_command(command)
        ):
            return ()
        return tuple(dict.fromkeys(intent.path for intent in authority.filesystem_intents))

    @staticmethod
    def _safe_python_external_read(tokens: list[str]) -> bool:
        """Recognize a deliberately small, side-effect-free PDF/text program.

        Absence of a dangerous regex match is not proof that dynamic code is
        read-only.  This AST allowlist exists only for the common PDF Skill
        transform that reads with pypdf and emits text to stdout.  Everything
        else remains runtime-evaluated and can produce one meaningful ASK.
        """

        try:
            code_index = tokens.index("-c")
            source = tokens[code_index + 1]
        except (ValueError, IndexError):
            return False
        if code_index + 2 != len(tokens):
            # External paths are normally embedded in the program. Positional
            # arguments would expand the authority surface and are not needed
            # by the supported transform.
            return False
        try:
            tree = ast.parse(source, mode="exec")
        except SyntaxError:
            return False

        allowed_modules = {"pypdf", "PyPDF2"}
        imported_call_names: set[str] = set()
        allowed_name_calls = {
            "all",
            "any",
            "bool",
            "dict",
            "enumerate",
            "float",
            "int",
            "len",
            "list",
            "max",
            "min",
            "print",
            "range",
            "round",
            "set",
            "sorted",
            "str",
            "sum",
            "tuple",
            "zip",
        }
        allowed_attribute_calls = {
            "PdfReader",
            "count",
            "endswith",
            "extract_text",
            "get",
            "items",
            "join",
            "keys",
            "lower",
            "lstrip",
            "replace",
            "rstrip",
            "split",
            "startswith",
            "strip",
            "upper",
            "values",
        }
        forbidden_names = {
            "__import__",
            "breakpoint",
            "compile",
            "delattr",
            "eval",
            "exec",
            "getattr",
            "globals",
            "help",
            "input",
            "locals",
            "open",
            "setattr",
            "vars",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name.split(".", 1)[0] not in allowed_modules for alias in node.names):
                    return False
            elif isinstance(node, ast.ImportFrom):
                if not node.module or node.module.split(".", 1)[0] not in allowed_modules:
                    return False
                imported_call_names.update(alias.asname or alias.name for alias in node.names)
            elif isinstance(node, ast.Attribute):
                if node.attr.startswith("_"):
                    return False
            elif isinstance(node, ast.Name) and node.id in forbidden_names:
                return False
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id not in allowed_name_calls | imported_call_names:
                        return False
                elif isinstance(node.func, ast.Attribute):
                    if node.func.attr not in allowed_attribute_calls:
                        return False
                else:
                    return False
        return True

    @classmethod
    def _provable_spawn_read_command(cls, command: str) -> bool:
        """Return whether every command segment belongs to a read-only set."""

        try:
            segments = ShellPolicyAnalyzer.parse_segments(command)
        except ValueError:
            return False
        if not segments:
            return False
        for segment in segments:
            # A wrapper is executable behavior, not syntactic decoration.
            # Stripping `env PATH=...` or `LD_PRELOAD=...` before proving the
            # command would let a nominally safe binary resolve to arbitrary
            # code.  Fast-path reads therefore require an unwrapped runtime
            # identity and no command-local environment mutation.
            if any(_ENV_ASSIGNMENT_PATTERN.match(item) for item in segment):
                return False
            if segment and Path(segment[0]).name.lower() in _WRAPPERS:
                return False
            tokens = ShellPolicyAnalyzer.unwrap_command(segment)
            if not tokens:
                return False
            executable = Path(tokens[0]).name.lower()
            args = tokens[1:]
            if executable == "rg" and any(item == "--pre" or item.startswith("--pre=") for item in args):
                return False
            if executable in _SAFE_READ_COMMANDS or executable in {"pdfinfo", "strings"}:
                continue
            if executable == "pdftotext":
                positional = [item for item in args if not item.startswith("-")]
                if not args or args[-1] != "-" or not positional:
                    return False
                continue
            if executable in {"python", "python3"} and cls._safe_python_external_read(args):
                continue
            return False
        return True

    @staticmethod
    def _sensitive_host_read(
        command: str,
        requirements: ExecutionRequirements,
    ) -> bool:
        """Keep credential material outside Spawn Smart's local-work fast path."""

        candidates = [
            Path(intent.path).expanduser()
            for intent in requirements.filesystem_intents
            if intent.access == "read" and intent.path
        ]
        if any(is_sensitive_host_read_path(candidate) for candidate in candidates):
            return True

        # Opaque shell/Python expressions do not always yield a normalized path
        # candidate. Preserve the same boundary for the common credential roots
        # and key-file names when they are visible only in source text.
        lowered = command.lower().replace("\\", "/")
        if re.search(r"(?:^|[/\s'\"])(?:\.ssh|\.gnupg|\.aws|\.azure|\.kube|\.docker)(?:[/\s'\"]|$)", lowered):
            return True
        return bool(
            re.search(
                r"(?:^|[/\s'\"])(?:\.env(?:\.[^/\s'\"]+)?|\.netrc|\.npmrc|\.pypirc|"
                r"id_(?:rsa|dsa|ecdsa|ed25519)|credentials(?:\.json)?|service[-_]account\.json|"
                r"[^/\s'\"]+\.(?:pem|key|p12|pfx))(?:[/\s'\"]|$)",
                lowered,
            )
        )

    def _smart_external_local_read_allowed(
        self,
        command: str,
        requirements: ExecutionRequirements,
        authority_requirements: ExecutionRequirements,
    ) -> bool:
        """Return whether Smart may treat ordinary host reads as local work."""

        if (
            not self.permission_context.smart
            or self._effective_backend_mode() not in {"spawn", "kernel"}
            or not authority_requirements.filesystem_intents
            or any(intent.access != "read" for intent in authority_requirements.filesystem_intents)
            or requirements.capabilities.network
            or requirements.capabilities.workspace_write
            or requirements.capabilities.package_install
            or requirements.capabilities.destructive
            or self._sensitive_host_read(command, authority_requirements)
        ):
            return False
        return not (
            _EMBEDDED_DESTRUCTIVE_API_PATTERN.search(command)
            or _OPAQUE_CRITICAL_ACTION_PATTERN.search(command)
            or _OPAQUE_DYNAMIC_CODE_PATTERN.search(command)
            or _CRITICAL_EXECUTION_ENV_OVERRIDE_PATTERN.search(command)
            or self._contains_credential_literal(command)
        )

    def _smart_kernel_external_read_roots(
        self,
        command: str,
        requirements: ExecutionRequirements,
        authority_requirements: ExecutionRequirements,
    ) -> tuple[Path, ...]:
        """Project Smart's per-call ordinary reads into the Kernel profile."""

        if self._effective_backend_mode() != "kernel" or not self._smart_external_local_read_allowed(
            command,
            requirements,
            authority_requirements,
        ):
            return ()
        roots: list[Path] = []
        for intent in authority_requirements.filesystem_intents:
            try:
                candidate = Path(intent.path).expanduser().resolve(strict=False)
                root = candidate if candidate.is_dir() else candidate.parent
            except OSError:
                continue
            if root.is_dir() and not root.is_symlink():
                roots.append(root.resolve())
        return tuple(dict.fromkeys(roots))

    def _require_external_shell_authority(
        self,
        request: ToolCallRequest,
    ) -> ToolMessage | None:
        """Interrupt once for the atomic directory set required by ``execute``."""

        if str(request.tool_call.get("name") or "") != "execute":
            return None
        context = self._context(request)
        workspace_path = str(context.get("workspace_path") or "")
        command = self._command(request)
        if not workspace_path or not command:
            return None
        requirements = self._execution_requirements(
            command,
            workspace_path=workspace_path,
        )
        violation = self._baseline_filesystem_violation(requirements)
        if violation is not None:
            return ToolMessage(
                content=(
                    "Shell filesystem access was denied by the execution grant profile: "
                    f"{violation.access} {violation.path}"
                ),
                name="execute",
                tool_call_id=str(request.tool_call.get("id") or ""),
                status="error",
            )
        authority_requirements = self._external_authority_requirements(requirements)
        if not authority_requirements.filesystem_intents:
            return None
        if self._smart_local_filesystem_unrestricted:
            # Trusted-local smart mode does not turn an ordinary host path
            # into a directory Grant. Effect policy is evaluated separately.
            return None
        if self._spawn_read_only_external_paths(
            command=command,
            workspace_path=workspace_path,
        ):
            return None
        if self._smart_external_local_read_allowed(
            command,
            requirements,
            authority_requirements,
        ):
            return None
        if (
            self._effective_backend_mode() == "spawn"
            and not requirements.capabilities.network
            and not requirements.capabilities.workspace_write
            and not requirements.capabilities.package_install
            and not requirements.capabilities.destructive
            and all(intent.access == "read" for intent in authority_requirements.filesystem_intents)
        ):
            # Dynamic code whose effects cannot be proved gets one Tool Gate
            # decision, not a misleading external-directory read Grant first.
            return None
        session_id = str(context.get("session_id") or "")
        query_id = str(context.get("query_id") or "")
        run_id = str(context.get("run_id") or "")
        if not session_id or not run_id:
            return ToolMessage(
                content="External shell access requires an active Harness Run.",
                name="execute",
                tool_call_id=str(request.tool_call.get("id") or ""),
                status="error",
            )
        grants, revision = session_manager.permission_grants_snapshot(session_id)
        effective = EffectiveGrantSet.resolve(
            grants,
            run_id=run_id,
            current_bindings=self.permission_context.grant_bindings(),
            current_shell_bindings=self.permission_context.shell_grant_bindings(),
            permission_revision=revision,
        )
        try:
            SelectedGrantSet.select(effective, authority_requirements)
            session_manager.transition_run_status(
                session_id,
                run_id,
                "running",
                expected_statuses={"running", "waiting_hitl"},
            )
            return None
        except PermissionError:
            pass
        try:
            plan = ShellAccessPlan.compile(authority_requirements, effective)
        except ValueError as exc:
            return ToolMessage(
                content=f"External shell access was denied: {exc}",
                name="execute",
                tool_call_id=str(request.tool_call.get("id") or ""),
                status="error",
            )
        permission_request = permission_resume_registry.create_shell_access_request(
            session_id=session_id,
            query_id=query_id,
            run_id=run_id,
            tool_call_id=str(request.tool_call.get("id") or ""),
            command=command,
            plan=plan,
            grant_bindings=self.permission_context.shell_grant_bindings(),
        )
        session_manager.transition_run_status(
            session_id,
            run_id,
            "waiting_hitl",
            expected_statuses={"running", "waiting_hitl"},
        )
        resume_value = interrupt(
            {
                "type": "permission_request",
                "request": permission_request,
                "decisions": [{"type": "approve"}, {"type": "reject"}],
            }
        )
        resume_value = self._permission_resume_decision(resume_value)
        # LangGraph resumes at the exact interrupt call site; it does not
        # replay this middleware from the top. Trust only newly persisted
        # authority, never the client-provided resume payload by itself.
        resumed_grants, resumed_revision = session_manager.permission_grants_snapshot(session_id)
        resumed_effective = EffectiveGrantSet.resolve(
            resumed_grants,
            run_id=run_id,
            current_bindings=self.permission_context.grant_bindings(),
            current_shell_bindings=self.permission_context.shell_grant_bindings(),
            permission_revision=resumed_revision,
        )
        try:
            SelectedGrantSet.select(resumed_effective, authority_requirements)
        except PermissionError:
            decision = str(resume_value.get("type") or "") if isinstance(resume_value, dict) else ""
            detail = "rejected" if decision == "reject" else "not persisted"
            return ToolMessage(
                content=f"External shell authority was {detail} after resume.",
                name="execute",
                tool_call_id=str(request.tool_call.get("id") or ""),
                status="error",
            )
        session_manager.transition_run_status(
            session_id,
            run_id,
            "running",
            expected_statuses={"running", "waiting_hitl"},
        )
        return None

    def _authorized_external_shell_paths(self, request: ToolCallRequest) -> tuple[str, ...]:
        if str(request.tool_call.get("name") or "") != "execute":
            return ()
        context = self._context(request)
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        workspace_path = str(context.get("workspace_path") or "")
        command = self._command(request)
        if not session_id or not run_id or not workspace_path or not command:
            return ()
        requirements = self._execution_requirements(
            command,
            workspace_path=workspace_path,
        )
        authority_requirements = self._external_authority_requirements(requirements)
        if not authority_requirements.filesystem_intents:
            return ()
        grants, revision = session_manager.permission_grants_snapshot(session_id)
        effective = EffectiveGrantSet.resolve(
            grants,
            run_id=run_id,
            current_bindings=self.permission_context.grant_bindings(),
            current_shell_bindings=self.permission_context.shell_grant_bindings(),
            permission_revision=revision,
        )
        try:
            SelectedGrantSet.select(effective, authority_requirements)
        except (PermissionError, ValueError):
            return ()
        return tuple(dict.fromkeys(intent.path for intent in authority_requirements.filesystem_intents))

    def _compile_kernel_execution(
        self,
        request: ToolCallRequest,
    ) -> AuthorizedExecution | None:
        backend_mode = self._effective_backend_mode()
        if (
            str(request.tool_call.get("name") or "") != "execute"
            or backend_mode not in {"kernel", "adaptive", "docker"}
            or self.workspace_backend is None
        ):
            return None
        context = self._context(request)
        workspace = Path(str(context.get("workspace_path") or "")).expanduser().resolve()
        scratch_value = getattr(self.workspace_backend, "scratch_path", None)
        if not workspace.is_dir() or scratch_value is None:
            raise RuntimeError("Sandbox execution roots are unavailable")
        scratch = Path(scratch_value).expanduser().resolve()
        command = self._command(request)
        requirements = self._execution_requirements(command, workspace_path=workspace)
        violation = self._baseline_filesystem_violation(requirements)
        if violation is not None:
            raise PermissionError(
                f"Execution requirements exceed the runner filesystem grant: {violation.access} {violation.path}"
            )
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        active_skill_ids = tuple(
            dict.fromkeys(
                str(item.get("skill_id") or "")
                for item in (
                    session_manager.get_effective_run_skill_activations(session_id, run_id)
                    if session_id and run_id
                    else []
                )
                if str(item.get("skill_id") or "")
            )
        )
        grants, revision = session_manager.permission_grants_snapshot(session_id) if session_id else ([], 0)
        effective = EffectiveGrantSet.resolve(
            grants,
            run_id=run_id,
            current_bindings=self.permission_context.grant_bindings(),
            current_shell_bindings=self.permission_context.shell_grant_bindings(),
            permission_revision=revision,
        )
        authority_requirements = self._external_authority_requirements(requirements)
        smart_kernel_read_roots = self._smart_kernel_external_read_roots(
            command,
            requirements,
            authority_requirements,
        )
        if authority_requirements.filesystem_intents and not smart_kernel_read_roots:
            # Re-check at permit compilation so a revoked or changed Grant
            # cannot ride the earlier middleware decision.
            SelectedGrantSet.select(effective, authority_requirements)
        selected = SelectedGrantSet.all_shell_authority(effective)
        runtime_for_command = getattr(self.workspace_backend, "skill_runtime_for_command", None)
        explicit_docker_skill = bool(callable(runtime_for_command) and runtime_for_command(command) == "docker")
        kernel_runner_mode = str(getattr(self.workspace_backend, "kernel_runner_mode", "kernel_macos_seatbelt"))
        selected_runner = (
            "docker"
            if backend_mode == "docker" or backend_mode == "adaptive" and (explicit_docker_skill)
            else kernel_runner_mode
        )
        runtime_read_roots: tuple[Path, ...] = ()
        runtime_environment: tuple[tuple[str, str], ...] = ()
        runtime_secret_values: tuple[str, ...] = ()

        def runtime_environment_current() -> bool:
            return True

        if selected_runner.startswith("kernel_"):
            prepare_host = getattr(self.workspace_backend, "prepare_host_execution", None)
            if callable(prepare_host):
                projection = prepare_host(
                    command,
                    active_skill_ids=active_skill_ids,
                )
                execution_command, runtime_read_roots = projection
                runtime_environment = tuple(getattr(projection, "environment", ()) or ())
                runtime_secret_values = tuple(getattr(projection, "secret_values", ()) or ())
                runtime_environment_current = getattr(
                    projection,
                    "environment_current",
                    runtime_environment_current,
                )
                binding_digest = str(getattr(projection, "environment_binding_digest", "") or "")
                if execution_command != command or binding_digest:
                    requirements = replace(
                        requirements,
                        execution_command=execution_command,
                        environment_binding_digest=binding_digest,
                    )
        elif explicit_docker_skill:
            prepare_docker = getattr(self.workspace_backend, "prepare_docker_execution", None)
            if callable(prepare_docker):
                projection = prepare_docker(command)
                execution_command, runtime_read_roots = projection
                requirements = replace(
                    requirements,
                    execution_command=execution_command,
                    environment_binding_digest=str(getattr(projection, "environment_binding_digest", "") or ""),
                )
        managed_readonly_roots = (
            tuple(getattr(self.workspace_backend, "managed_readonly_host_roots", ()) or ())
            if selected_runner.startswith("kernel_")
            else ()
        )
        profile = SandboxGrantProfile.build(
            workspace_root=workspace,
            scratch_root=scratch,
            workspace_writable=requirements.capabilities.workspace_write,
            external_read_roots=(
                *selected.read_roots,
                *smart_kernel_read_roots,
                *managed_readonly_roots,
                *runtime_read_roots,
            ),
            external_write_roots=selected.write_roots,
            external_delete_roots=selected.delete_roots,
            network_allowed=requirements.capabilities.network,
            filesystem=self._filesystem_mode,
        )
        runner_binding_digest = ""
        if selected_runner.startswith("kernel_"):
            binding_resolver = getattr(self.workspace_backend, "kernel_runner_binding_digest", None)
            if callable(binding_resolver):
                runner_binding_digest = str(binding_resolver() or "")
            else:
                runner_binding_digest = str(binding_resolver or "")
            if not runner_binding_digest:
                raise RuntimeError("Kernel backend does not expose a runner binding digest")
        tool_call_id = str(request.tool_call.get("id") or "")
        permit = ExecutionPermit.issue(
            tool_call_id=tool_call_id,
            command=command,
            requirements=requirements,
            permission_revision=revision,
            profile_digest=profile.digest,
            selected_runner=selected_runner,
            runner_binding_digest=runner_binding_digest,
        )

        def current_revision() -> int:
            if not session_id:
                return 0
            _current_grants, current = session_manager.permission_grants_snapshot(session_id)
            return current

        return AuthorizedExecution(
            permit=permit,
            command=command,
            requirements=requirements,
            profile=profile,
            current_permission_revision=current_revision,
            environment=runtime_environment,
            secret_values=runtime_secret_values,
            environment_current=runtime_environment_current,
        )

    def _granted_external_shell_fast_path(
        self,
        request: ToolCallRequest,
        result: ToolPolicyResult,
    ) -> ToolPolicyResult | None:
        """Allow only non-overwriting cp/mv and mkdir after explicit grants."""

        if result.decision != PolicyDecision.ASK:
            return None
        context = self._context(request)
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        workspace_path = str(context.get("workspace_path") or "")
        if not session_id or not run_id or not workspace_path:
            return None
        command = self._command(request)
        requirements = self._execution_requirements(
            command,
            workspace_path=workspace_path,
        )
        if requirements.opaque or not requirements.filesystem_intents:
            return None
        try:
            segments = ShellPolicyAnalyzer.parse_segments(command)
            tokens = ShellPolicyAnalyzer.unwrap_command(segments[0])
        except (ValueError, IndexError):
            return None
        executable = Path(tokens[0]).name.lower() if tokens else ""
        if executable not in {"cp", "mv", "mkdir"}:
            return None
        write_targets = [Path(intent.path) for intent in requirements.filesystem_intents if intent.access == "write"]
        if not write_targets or any(path.exists() for path in write_targets):
            return None
        grants, revision = session_manager.permission_grants_snapshot(session_id)
        effective = EffectiveGrantSet.resolve(
            grants,
            run_id=run_id,
            current_bindings=self.permission_context.grant_bindings(),
            current_shell_bindings=self.permission_context.shell_grant_bindings(),
            permission_revision=revision,
        )
        try:
            SelectedGrantSet.select(effective, requirements)
        except (PermissionError, ValueError):
            return None
        return ToolPolicyResult(
            PolicyDecision.ALLOW,
            f"authorized_external_shell:{executable}:non_overwrite",
            "managed_write",
        )

    @staticmethod
    def _shell_host_path(raw_path: str, authorized: AuthorizedExecution) -> Path | None:
        """Resolve one already-classified shell operand to its host location.

        This is evidence bookkeeping, not a second authorization path.  The
        command can only reach these bytes through the permit-bound Grant
        Profile compiled above.
        """

        normalized = str(raw_path or "").replace("\\", "/")
        if normalized == "/scratch" or normalized.startswith("/scratch/"):
            relative = normalized.removeprefix("/scratch").lstrip("/")
            candidate = (authorized.profile.scratch_root / relative).resolve(strict=False)
            try:
                candidate.relative_to(authorized.profile.scratch_root)
            except ValueError:
                return None
            return candidate
        classified = classify_path_authority(
            normalized,
            workspace_root=authorized.profile.workspace_root,
        )
        if classified.authority in {PathAuthority.ESCAPE, PathAuthority.MANAGED}:
            return None
        return classified.canonical_host_path

    @classmethod
    def _shell_mutation_paths(
        cls,
        authorized: AuthorizedExecution,
    ) -> tuple[tuple[str, str, Path], ...]:
        """Return statically proven effects for the narrow cp/mv/mkdir grammar."""

        if authorized.requirements.opaque:
            return ()
        try:
            segments = ShellPolicyAnalyzer.parse_segments(authorized.execution_command)
        except ValueError:
            return ()
        effects: list[tuple[str, str, Path]] = []
        for raw_segment in segments:
            tokens = ShellPolicyAnalyzer.unwrap_command(raw_segment)
            if not tokens:
                return ()
            operation = Path(tokens[0]).name.lower()
            parsed = ShellPolicyAnalyzer._supported_filesystem_segment(tokens)
            if isinstance(parsed, str):
                return ()
            _accesses, operand_indexes = parsed
            unique_indexes = tuple(dict.fromkeys(operand_indexes))
            operands = [tokens[index] for index in unique_indexes]
            if operation in {"cp", "mv"} and len(operands) >= 2:
                sources = [cls._shell_host_path(item, authorized) for item in operands[:-1]]
                destination = cls._shell_host_path(operands[-1], authorized)
                if destination is None or any(source is None for source in sources):
                    return ()
                concrete_sources = [source for source in sources if source is not None]
                destination_is_directory = destination.is_dir()
                for source in concrete_sources:
                    target = destination / source.name if destination_is_directory else destination
                    effects.append((operation, "write", target.resolve(strict=False)))
                    if operation == "mv":
                        effects.append((operation, "delete", source.resolve(strict=False)))
            elif operation == "mkdir" and operands:
                for operand in operands:
                    target = cls._shell_host_path(operand, authorized)
                    if target is not None:
                        effects.append((operation, "write", target.resolve(strict=False)))
        return tuple(dict.fromkeys(effects))

    @staticmethod
    def _path_snapshot(path: Path) -> dict[str, Any]:
        try:
            metadata = path.lstat()
        except OSError:
            return {"exists": False}
        if stat.S_ISREG(metadata.st_mode):
            kind = "file"
        elif stat.S_ISDIR(metadata.st_mode):
            kind = "directory"
        elif stat.S_ISLNK(metadata.st_mode):
            kind = "symlink"
        else:
            kind = "other"
        snapshot: dict[str, Any] = {
            "exists": True,
            "kind": kind,
            "size_bytes": metadata.st_size,
            "mtime_ns": metadata.st_mtime_ns,
        }
        if kind == "file":
            hasher = hashlib.sha256()
            try:
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
            except OSError:
                return {**snapshot, "content_sha256": None}
            snapshot["content_sha256"] = f"sha256:{hasher.hexdigest()}"
        return snapshot

    @classmethod
    def _shell_mutation_before(
        cls,
        authorized: AuthorizedExecution,
    ) -> tuple[tuple[str, str, Path, dict[str, Any]], ...]:
        return tuple(
            (operation, access, path, cls._path_snapshot(path))
            for operation, access, path in cls._shell_mutation_paths(authorized)
        )

    @classmethod
    def _attach_shell_mutation_evidence(
        cls,
        result: ToolMessage | Command[Any],
        *,
        authorized: AuthorizedExecution,
        before: tuple[tuple[str, str, Path, dict[str, Any]], ...],
    ) -> ToolMessage | Command[Any]:
        """Attach server-observed postconditions without changing model text."""

        if not isinstance(result, ToolMessage) or not before:
            return result
        mutations: list[dict[str, Any]] = []
        for operation, access, path, previous in before:
            current = cls._path_snapshot(path)
            if current == previous:
                continue
            mutations.append(
                {
                    "kind": "shell_mutation_observed",
                    "receipt_version": 1,
                    "tool_call_id": authorized.permit.tool_call_id,
                    "operation": operation,
                    "access": access,
                    "target_path": str(path),
                    "before": previous,
                    "after": current,
                    "atomic": False,
                    "command_digest": authorized.permit.command_digest,
                    "requirements_digest": authorized.permit.requirements_digest,
                    "permission_revision": authorized.permit.permission_revision,
                    "profile_digest": authorized.permit.profile_digest,
                    "selected_runner": authorized.permit.selected_runner,
                }
            )
        if not mutations:
            return result
        artifact = getattr(result, "artifact", None)
        payload = dict(artifact) if isinstance(artifact, dict) else {}
        payload["puddingclaw_shell_mutations"] = mutations
        return result.model_copy(update={"artifact": payload})

    async def _invoke_handler_with_execution_permit(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        authorized = self._compile_kernel_execution(request)
        if authorized is None:
            with self._browser_authorization_context(request):
                return await handler(request)
        before = self._shell_mutation_before(authorized)
        with bind_authorized_execution(authorized):
            with self._browser_authorization_context(request):
                result = await handler(request)
        return self._attach_shell_mutation_evidence(
            result,
            authorized=authorized,
            before=before,
        )

    def _invoke_sync_handler_with_execution_permit(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        authorized = self._compile_kernel_execution(request)
        if authorized is None:
            with self._browser_authorization_context(request):
                return handler(request)
        before = self._shell_mutation_before(authorized)
        with bind_authorized_execution(authorized):
            with self._browser_authorization_context(request):
                result = handler(request)
        return self._attach_shell_mutation_evidence(
            result,
            authorized=authorized,
            before=before,
        )

    @staticmethod
    def _browser_authorization_context(request: ToolCallRequest):
        if str(request.tool_call.get("name") or "") != "browser":
            return nullcontext()
        args = request.tool_call.get("args") or {}
        try:
            from connectors.kimi_webbridge.models import BrowserCommand

            command = BrowserCommand.model_validate(args)
        except Exception:
            return nullcontext()
        runtime = request.runtime.context if request.runtime is not None else {}
        context = runtime if isinstance(runtime, dict) else {}
        return bind_authorized_browser_action(
            AuthorizedBrowserAction(
                session_id=str(context.get("session_id") or ""),
                run_id=str(context.get("run_id") or ""),
                tool_call_id=str(request.tool_call.get("id") or ""),
                action=command.action,
                args_digest=browser_action_digest(command.action, command.args),
            )
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

    @staticmethod
    def _managed_authorization_error(message: Any) -> dict[str, Any] | None:
        """Return a terminal managed-auth failure envelope, if present.

        These failures describe Backend state-machine or provider-initiation
        boundaries. Letting the model improvise another command (for example
        ``auth resume`` before a user-consent attempt exists) only obscures the
        original failure and can roll the user back to an earlier phase.
        """

        if not isinstance(message, ToolMessage) or message.name != "execute":
            return None
        try:
            value = json.loads(str(message.content or ""))
        except (TypeError, ValueError):
            return None
        if not isinstance(value, dict) or value.get("managed_by") != "managed_cli":
            return None
        error = str(value.get("error") or "")
        if error in {
            "managed_authorization_failed",
            "authorization_prerequisite_failed",
            "authorization_flow_missing",
            "lark_user_authorization_failed",
            "lark_user_authorization_invalid_response",
            "lark_user_authorization_untrusted_url",
        }:
            return value
        return None

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
                    updated = message.model_copy(
                        update={"content": json.dumps(value, ensure_ascii=False, sort_keys=True)}
                    )
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
        authorization_error: dict[str, Any] | None = None
        for message in reversed(messages[:-1]):
            if isinstance(message, ToolMessage):
                if self._awaiting_user_browser(message):
                    boundary = message
                else:
                    authorization_error = self._managed_authorization_error(message)
                break
            if isinstance(message, AIMessage):
                break
        if boundary is None and authorization_error is None:
            return None
        if authorization_error is not None:
            if not latest.tool_calls:
                return {"jump_to": "end"}
            message = str(authorization_error.get("message") or "托管授权流程未能继续。")
            replacement = latest.model_copy(
                update={
                    "content": f"{message} 已停止本轮操作；不会尝试其他授权命令或回退到上一步。",
                    "tool_calls": [],
                }
            )
            return {"messages": [replacement], "jump_to": "end"}
        assert boundary is not None
        if not latest.tool_calls:
            return {"jump_to": "end"}
        try:
            payload = json.loads(str(boundary.content or ""))
        except (TypeError, ValueError):
            payload = {}
        request = payload.get("authorization_request") if isinstance(payload, dict) else None
        phase = request.get("phase") if isinstance(request, dict) else None
        step = phase.get("step") if isinstance(phase, dict) else None
        total = phase.get("total") if isinstance(phase, dict) else None
        title = phase.get("title") if isinstance(phase, dict) else None
        step_label = f"第 {step}/{total} 步 · {title}" if step and total and title else "当前授权步骤"
        replacement = latest.model_copy(
            update={
                "content": (
                    f"{step_label}已启动，但尚未完成。请使用上方卡片中的二维码或链接完成浏览器操作。"
                    "完成后直接告诉我即可；Backend 验证通过后才会进入下一步。"
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
        return any(lowered[index : index + 2] == ["skills", "add"] for index in range(max(0, len(lowered) - 1)))

    @classmethod
    def _contains_npx_skills_add(cls, command: str) -> bool:
        try:
            parsed_match = any(
                cls._segment_is_npx_skills_add(segment) for segment in ShellPolicyAnalyzer.parse_segments(command)
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
                index for index in range(1, len(tokens) - 1) if lowered[index : index + 2] == ["skills", "add"]
            )
        except StopIteration:
            return None
        args = tokens[skills_index + 2 :]
        source = ""
        skill_names: list[str] = []
        yes = any(token.lower() in {"-y", "--yes"} for token in tokens[1:skills_index])
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
        *,
        destructive_approval: str | None = None,
    ) -> ToolMessage | Command[Any]:
        if managed_cli is not None:
            if self.managed_cli_service is None:
                return ToolMessage(
                    content=json.dumps(
                        {
                            "ok": False,
                            "managed_by": "managed_cli",
                            "error": "managed_cli_service_unavailable",
                            "message": (
                                "Managed CLI execution has no compatible managed command runner "
                                "in the current execution mode."
                            ),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    name="execute",
                    tool_call_id=str(request.tool_call.get("id") or ""),
                    status="error",
                )
            context = {
                **self._context(request),
                "_managed_cli_destructive_approval": destructive_approval,
            }
            result = await asyncio.to_thread(self.managed_cli_service.execute, managed_cli, context)
            managed_payload = getattr(result, "payload", {})
            if (
                destructive_approval is None
                and isinstance(managed_payload, dict)
                and managed_payload.get("status") == "confirmation_required"
            ):
                return await self._request_managed_cli_destructive_confirmation(
                    request,
                    handler,
                    managed_cli,
                    managed_payload,
                )
            model_content = result.content
            artifact = None
            if (
                isinstance(managed_payload, dict)
                and managed_payload.get("status") == "awaiting_user_browser"
                and isinstance(managed_payload.get("authorization_request"), dict)
            ):
                request_payload = managed_payload["authorization_request"]
                model_request = {
                    key: request_payload.get(key)
                    for key in (
                        "type",
                        "flow_id",
                        "revision",
                        "attempt",
                        "provider",
                        "profile_id",
                        "status",
                        "phase",
                        "completion_hint",
                    )
                    if request_payload.get(key) is not None
                }
                model_content = json.dumps(
                    {
                        "ok": True,
                        "managed_by": "managed_cli",
                        "status": "awaiting_user_browser",
                        "authorization_completed": False,
                        "authorization_request": model_request,
                        "output": managed_payload.get("output"),
                        "next_action": managed_payload.get("next_action"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                # DeepAgents persists/adapts this raw artifact for the UI and
                # strips it from serialized model context. Thus browser URLs
                # and QR material never need to enter ToolMessage content.
                artifact = {"puddingclaw_raw_tool_output": result.content}
            return ToolMessage(
                content=model_content,
                name="execute",
                tool_call_id=str(request.tool_call.get("id") or ""),
                status="success" if result.exit_code == 0 else "error",
                artifact=artifact,
            )
        if managed_add is None:
            request = self._materialize_cited_write_request(request)
            return await self._invoke_handler_with_execution_permit(request, handler)
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

    def _materialize_cited_write_request(self, request: ToolCallRequest) -> ToolCallRequest:
        """Make full Markdown/HTML writes independent from the chat renderer."""

        tool_name = str(request.tool_call.get("name") or "")
        if tool_name not in {"write_file", "replace_file"}:
            return request
        args = request.tool_call.get("args")
        if not isinstance(args, dict):
            return request
        file_path = str(args.get("file_path") or "")
        content = args.get("content")
        if not isinstance(content, str) or Path(file_path).suffix.lower() not in {
            ".md",
            ".markdown",
            ".html",
            ".htm",
        }:
            return request
        session_id = str(self._context(request).get("session_id") or "")
        if not session_id:
            return request
        sources = dedupe_sources(
            [
                source
                for message in session_manager.load_session(session_id)
                for source in message.get("sources", []) or []
                if isinstance(source, dict)
            ]
        )
        rendered, report = materialize_artifact_citations(
            content,
            sources,
            file_path=file_path,
        )
        if rendered == content:
            return request
        if report.get("unresolved_source_ids"):
            logger.warning(
                "Artifact citation materialization has unresolved sources: session=%s file=%s ids=%s",
                session_id,
                file_path,
                report["unresolved_source_ids"],
            )
        updated_args = {**args, "content": rendered}
        return request.override(tool_call={**request.tool_call, "args": updated_args})

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
                managed_cli = (
                    self.managed_cli_service.plan_command(command, self._context(request))
                    if self.managed_cli_service is not None
                    else self.managed_cli_registry.match(command)
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
        # A claimed Managed CLI command has already been parsed into a frozen
        # Adapter plan and never reaches the project shell runner. Re-parsing
        # its payload as shell text is both redundant and incorrect: ordinary
        # message bodies such as ``ready / token`` can otherwise be mistaken
        # for a request to mount the host root directory. Managed runners
        # receive only the workspace and the exact Toolchain revision.
        if managed_cli is None:
            shell_authority_result = self._require_external_shell_authority(request)
            if shell_authority_result is not None:
                return shell_authority_result
        result = (
            self._managed_cli_preflight(managed_cli) if managed_cli is not None else await self._apreflight(request)
        )
        if result.decision == PolicyDecision.ALLOW:
            self._record_reviewer_decision(request, result)
            delta_denial = self._delta_repair_denial(request)
            if delta_denial is not None:
                return delta_denial
            fallback_error = await self._ensure_execution_backend(request)
            if fallback_error is not None:
                return fallback_error
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
        fingerprint_command = self._permission_fingerprint_command(request, command)
        fingerprint = permission_resume_registry.tool_action_fingerprint(
            tool_name=tool_name,
            command=fingerprint_command,
            reason=result.reason,
        )
        session_scope = self._session_grant_scope(request)
        required_capabilities = self._required_capabilities(request, managed_cli=managed_cli)
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
            return await self._invoke_authorized(
                request,
                handler,
                managed_add,
                managed_cli,
                destructive_approval=(
                    managed_cli.destructive_approval_binding()
                    if managed_cli is not None
                    and getattr(getattr(managed_cli, "match", managed_cli), "destructive", False)
                    and hasattr(managed_cli, "destructive_approval_binding")
                    else None
                ),
            )
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
            fingerprint_command=fingerprint_command,
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
        decision = self._permission_resume_decision(decision)
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
                return await self._invoke_authorized(
                    request,
                    handler,
                    managed_add,
                    managed_cli,
                    destructive_approval=(
                        managed_cli.destructive_approval_binding()
                        if managed_cli is not None
                        and getattr(getattr(managed_cli, "match", managed_cli), "destructive", False)
                        and hasattr(managed_cli, "destructive_approval_binding")
                        else None
                    ),
                )
        return self._denied_message(request, result)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        managed_cli: Any | None = None
        if str(request.tool_call.get("name") or "") == "execute":
            try:
                managed_cli = (
                    self.managed_cli_service.plan_command(self._command(request), self._context(request))
                    if self.managed_cli_service is not None
                    else self.managed_cli_registry.match(self._command(request))
                )
            except UnsupportedManagedCliCommand as exc:
                return self._managed_cli_rejection(request, str(exc))
            except Exception as exc:  # noqa: BLE001
                return self._managed_cli_rejection(
                    request,
                    f"Managed CLI planning failed: {type(exc).__name__}: {exc}",
                )
        if managed_cli is None:
            shell_authority_result = self._require_external_shell_authority(request)
            if shell_authority_result is not None:
                return shell_authority_result
        result = self._managed_cli_preflight(managed_cli) if managed_cli is not None else self._preflight(request)
        if result.decision == PolicyDecision.ALLOW:
            delta_denial = self._delta_repair_denial(request)
            if delta_denial is not None:
                return delta_denial
            if str(request.tool_call.get("name") or "") == "execute" and bool(
                getattr(self.workspace_backend, "kernel_unavailable", False)
            ):
                return self._kernel_fallback_error(
                    request,
                    reason="Kernel fallback requires the interactive Run stream; unattended execution is fail-closed.",
                )
            if managed_cli is not None:
                if self.managed_cli_service is None:
                    return self._managed_cli_rejection(
                        request,
                        "Managed CLI execution has no compatible managed command runner in the current execution mode.",
                    )
                managed = self.managed_cli_service.execute(managed_cli, self._context(request))
                return ToolMessage(
                    content=managed.content,
                    name="execute",
                    tool_call_id=str(request.tool_call.get("id") or ""),
                    status="success" if managed.exit_code == 0 else "error",
                )
            return self._invoke_sync_handler_with_execution_permit(request, handler)
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
            forbidden.update(name for name in self.known_tools if name.startswith("database_"))
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

    async def _request_managed_cli_destructive_confirmation(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
        managed_cli: Any,
        payload: dict[str, Any],
    ) -> ToolMessage | Command[Any]:
        """Turn a dynamic Lark exit-10 delete gate into the normal HITL flow."""

        confirmation = payload.get("confirmation")
        if not isinstance(confirmation, dict):
            return self._managed_cli_rejection(request, "Invalid managed CLI confirmation payload.")
        approval_binding = str(confirmation.get("approval_binding") or "")
        if not approval_binding:
            return self._managed_cli_rejection(request, "Managed CLI confirmation is not bound to its plan.")
        context = self._context(request)
        session_id = str(context.get("session_id") or "")
        query_id = str(context.get("query_id") or "")
        run_id = str(context.get("run_id") or "")
        command = json.dumps(
            {
                "plan": managed_cli.approval_preview(),
                "confirmation": confirmation,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        reason = "managed_cli_destructive_action"
        fingerprint = permission_resume_registry.tool_action_fingerprint(
            tool_name="execute",
            command=command,
            reason=reason,
        )
        required_capabilities = self._required_capabilities(request, managed_cli=managed_cli)
        if "destructive_write" not in required_capabilities:
            required_capabilities.append("destructive_write")
        preview = permission_resume_registry.create_tool_action_request(
            session_id=session_id,
            query_id=query_id,
            tool_call_id=str(request.tool_call.get("id") or ""),
            tool_name="execute",
            command=command,
            reason=reason,
            risk="high",
            run_id=run_id,
            grant_bindings=self.permission_context.grant_bindings(),
            required_capabilities=required_capabilities,
            policy_source="managed_cli",
            policy_explanation="删除类飞书操作需要用户明确确认。",
            control_descriptor=self._control_descriptor("execute"),
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
        decision = self._permission_resume_decision(decision)
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
                required_bindings=self.permission_context.grant_bindings(),
                required_capabilities=required_capabilities,
                current_run_id=run_id,
            ):
                return await self._invoke_authorized(
                    request,
                    handler,
                    None,
                    managed_cli,
                    destructive_approval=approval_binding,
                )
        return self._denied_message(
            request,
            ToolPolicyResult(PolicyDecision.ASK, reason, "high", source="managed_cli"),
        )

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
            if tool_name in self.mcp_tool_names:
                return ToolPolicyResult(
                    PolicyDecision.ASK,
                    "mcp_tool_requires_user_approval",
                    "high",
                    explanation=("MCP 工具来自已启用的外部 MCP Server，但没有静态控制描述；首次执行需要用户确认。"),
                )
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
        if tool_name == "browser":
            args = request.tool_call.get("args") or {}
            try:
                from connectors.kimi_webbridge.models import BrowserCommand
                from connectors.kimi_webbridge.policy import classify_browser_command, sanitize_action_args

                command = BrowserCommand.model_validate(args)
                sanitize_action_args(command.action, command.args)
            except Exception:
                return ToolPolicyResult(
                    PolicyDecision.DENY,
                    "browser_action_invalid_arguments",
                    "critical",
                    explanation=(
                        "browser 动作或参数不符合 WebBridge 契约；快照中的 @e 引用应作为 "
                        "click/fill 的 args.selector 传入。"
                    ),
                )
            browser_policy = classify_browser_command(command.action, command.args)
            if browser_policy.decision == "deny":
                return ToolPolicyResult(
                    PolicyDecision.DENY,
                    browser_policy.reason,
                    browser_policy.risk,
                )
            if browser_policy.decision == "ask":
                if self.permission_context.smart and command.action in {"click", "fill"}:
                    return ToolPolicyResult(
                        PolicyDecision.ALLOW,
                        "smart_browser_interaction",
                        "browser_interaction",
                        explanation=(
                            "当前 Run 使用智能模式；click/fill 由 Harness 绑定到本次 Run、"
                            "当前 tab 和完整参数后自动授权。"
                        ),
                    )
                return ToolPolicyResult(
                    PolicyDecision.ASK,
                    browser_policy.reason,
                    browser_policy.risk,
                    explanation=(
                        "浏览器交互动作必须绑定本次 Run、当前 tab 状态和一次性用户确认；"
                        "批准不会产生 Session 级或永久授权。"
                    ),
                )
            return ToolPolicyResult(
                PolicyDecision.ALLOW,
                browser_policy.reason,
                browser_policy.risk,
            )
        if tool_name == "execute":
            command = self._command(request)
            if self._contains_webbridge_direct_access(command):
                return ToolPolicyResult(
                    PolicyDecision.DENY,
                    "webbridge_daemon_direct_access_forbidden",
                    "critical",
                )
            if self._contains_webbridge_indirect_access(command):
                return ToolPolicyResult(
                    PolicyDecision.DENY,
                    "webbridge_daemon_indirect_access_forbidden",
                    "critical",
                )
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
                if tool_name in {"web_search", "tavily_search"}:
                    return ToolPolicyResult(
                        PolicyDecision.ALLOW,
                        f"smart_controlled_network:{tool_name}",
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
        if tool_name in self.SEMANTIC_COMMIT_TOOLS:
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "digest_bound_semantic_definition_publish",
                "managed_definition_write",
                explanation=("发布会替换用户语义定义；批准只绑定本次调用中的 plan_id 和 plan_digest。"),
            )
        if tool_name == "install_packages":
            if self.permission_context.backend_mode not in {"spawn", "kernel", "adaptive", "docker"}:
                return ToolPolicyResult(
                    PolicyDecision.DENY,
                    "package_install_requires_host_runtime",
                    "critical",
                )
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "package_management:install_packages",
                "package_install",
            )
        if tool_name == "request_skill_runtime":
            if self.permission_context.backend_mode != "adaptive":
                return ToolPolicyResult(
                    PolicyDecision.DENY,
                    "explicit_docker_skill_requires_adaptive_runtime",
                    "critical",
                )
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "explicit_skill_runtime_selection",
                "high",
            )
        if tool_name == "execute_external_directory":
            if self.permission_context.backend_mode not in {
                "spawn",
                "kernel",
                "docker",
                "adaptive",
            } or self._effective_backend_mode() not in {
                "kernel",
                "spawn",
                "docker",
                "adaptive",
            }:
                return ToolPolicyResult(
                    PolicyDecision.DENY,
                    "external_directory_command_requires_sandbox",
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
            if self._effective_backend_mode() == "spawn":
                if (
                    not effects.workspace_write
                    and not effects.destructive
                    and self._provable_spawn_read_command(command)
                ):
                    return ToolPolicyResult(
                        PolicyDecision.ALLOW,
                        "spawn_external_directory_read_only",
                        "low",
                    )
                return ToolPolicyResult(
                    PolicyDecision.DENY,
                    "external_directory_read_only_effect_unprovable",
                    "critical",
                )
            if self._registered_external_validator(command):
                if "/opt/puddingclaw/bin/validate-html-report-e2e.mjs" in command and not self._browser_e2e_required(
                    request
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
        workspace_path = str(context.get("workspace_path") or ".")
        command = self._command(request)
        policy_command = self._policy_command(command)
        raw_requirements = self._execution_requirements(
            command,
            workspace_path=workspace_path,
        )
        if self._smart_local_filesystem_unrestricted and self._persistence_write(
            command,
            raw_requirements,
        ):
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "persistence_write",
                "high",
                explanation="写入 Shell 启动、LaunchAgents、crontab 或 authorized_keys 仍属于持久化副作用。",
            )
        if self._smart_local_filesystem_unrestricted and self._sensitive_host_read(
            command,
            raw_requirements,
        ):
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "sensitive_host_read",
                "high",
            )
        if self._smart_local_filesystem_unrestricted and (
            _EMBEDDED_DESTRUCTIVE_API_PATTERN.search(command)
            or _OPAQUE_DYNAMIC_CODE_PATTERN.search(command)
            or _CRITICAL_EXECUTION_ENV_OVERRIDE_PATTERN.search(command)
        ):
            # Full filesystem authority must not erase independent effect
            # policy.  Dynamic imports and executable-resolution overrides can
            # hide deletion, network, installation, or arbitrary native code
            # behind an otherwise ordinary local path.
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "local_dynamic_effect_unprovable",
                "high",
            )
        violation = self._baseline_filesystem_violation(raw_requirements)
        if violation is not None:
            return ToolPolicyResult(
                PolicyDecision.DENY,
                "filesystem_grant_access_denied",
                "critical",
                explanation=(f"Runner grant permits no {violation.access} access to {violation.path}."),
            )
        raw_authority = self._external_authority_requirements(raw_requirements)
        if (
            self._effective_backend_mode() in {"spawn", "kernel"}
            and raw_authority.filesystem_intents
            and all(intent.access == "read" for intent in raw_authority.filesystem_intents)
            and not raw_requirements.capabilities.network
            and not raw_requirements.capabilities.workspace_write
            and not raw_requirements.capabilities.package_install
            and not raw_requirements.capabilities.destructive
            and not self._provable_spawn_read_command(command)
        ):
            if self.permission_context.smart:
                if self._sensitive_host_read(command, raw_authority):
                    return ToolPolicyResult(
                        PolicyDecision.ASK,
                        "sensitive_host_read",
                        "high",
                    )
                if (
                    _EMBEDDED_DESTRUCTIVE_API_PATTERN.search(command)
                    or _OPAQUE_CRITICAL_ACTION_PATTERN.search(command)
                    or _OPAQUE_DYNAMIC_CODE_PATTERN.search(command)
                    or _CRITICAL_EXECUTION_ENV_OVERRIDE_PATTERN.search(command)
                    or self._contains_credential_literal(command)
                ):
                    return ToolPolicyResult(
                        PolicyDecision.ASK,
                        "spawn_external_dynamic_effect_unprovable",
                        "high",
                    )
                # Spawn + Smart is the trusted-local-work profile. Match the
                # ergonomics of Codex on-request/OpenCode auto: command shape
                # alone is not an approval boundary. Explicit destructive,
                # credential, network, package, and sensitive-read signals are
                # still handled above or by the normal policy path below.
                return ToolPolicyResult(
                    PolicyDecision.ALLOW,
                    f"smart_{self._effective_backend_mode()}_external_local_execute",
                    "low",
                )
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "spawn_external_dynamic_effect_unprovable",
                "high",
            )
        allowed_external_paths = (
            *self._authorized_external_shell_paths(request),
            *self._spawn_read_only_external_paths(
                command=command,
                workspace_path=workspace_path,
            ),
            *self._filesystem_roots("read"),
            *self._filesystem_roots("write"),
            *self._filesystem_roots("delete"),
        )
        analyzer = ShellPolicyAnalyzer(
            workspace_path=workspace_path,
            backend_mode=self.backend_mode,
            filesystem_mode=self._filesystem_mode,
            allowed_external_paths=allowed_external_paths,
            path_resolver=self._execution_path_resolver(),
        )
        result = analyzer.analyze(policy_command)
        external_shell_result = self._granted_external_shell_fast_path(
            request,
            result,
        )
        if external_shell_result is not None:
            return external_shell_result
        effects = analyzer.capabilities(
            policy_command,
            workspace_path=workspace_path,
        )
        credential_network_result = self._credential_network_result(
            request=request,
            command=command,
            effects=effects,
        )
        if credential_network_result is not None:
            return credential_network_result
        rule_result = self._permission_rule_result(
            request=request,
            command=command,
            result=result,
            effects=effects,
        )
        if rule_result is not None:
            return rule_result
        if (
            effects.destructive
            and result.decision is not PolicyDecision.DENY
            and not (
                result.reason.startswith("destructive_")
                or result.reason in _SMART_DOCKER_DESTRUCTIVE_REASONS
                or result.reason.startswith("managed_workspace_write:find:")
                or result.reason.startswith("managed_git_write:")
            )
        ):
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "destructive_shell_effect",
                "high",
                explanation="Shell 控制流中包含递归删除或其他破坏性文件效果。",
            )
        if (
            effects.network
            and result.decision is not PolicyDecision.DENY
            and not result.reason.startswith(
                (
                    "network_access:",
                    "git_network",
                    "package_management",
                    "credential_network_coupling:",
                )
            )
        ):
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "network_access:embedded_command",
                "network",
            )
        smart_network_result = self._smart_network_result(
            command=command,
            result=result,
            effects=effects,
        )
        if smart_network_result is not None:
            return smart_network_result
        smart_result = self._smart_sandbox_result(
            command=command,
            result=result,
            effects=effects,
        )
        if smart_result is not None:
            return smart_result
        if result.decision is PolicyDecision.ASK and self._smart_external_local_read_allowed(
            command,
            raw_requirements,
            raw_authority,
        ):
            # Capability and path analysis has proved an ordinary local read,
            # while every stronger policy layer above (credential coupling,
            # typed user rules, network/package/destructive checks, and the
            # normal Smart sandbox classifier) declined to allow it.  At this
            # point an executable-name miss is not a reason to invoke a
            # probabilistic reviewer or interrupt the user.
            return ToolPolicyResult(
                PolicyDecision.ALLOW,
                f"smart_{self._effective_backend_mode()}_external_local_execute",
                "low",
            )
        return result

    def _credential_network_result(
        self,
        *,
        request: ToolCallRequest,
        command: str,
        effects: ShellCapabilities,
    ) -> ToolPolicyResult | None:
        """Keep injected Skill credentials and network authorization atomic."""

        if not effects.network:
            return None
        if self._contains_credential_literal(command):
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "credential_network_coupling:literal_secret",
                "high",
                explanation="命令同时包含凭证材料并访问网络；必须走一次性耦合审批。",
            )
        backend_mode = self._effective_backend_mode()
        if backend_mode not in {"kernel", "adaptive"} or self.workspace_backend is None:
            return None
        context = self._context(request)
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        if not session_id or not run_id:
            return None
        try:
            active_skill_ids = tuple(
                dict.fromkeys(
                    str(item.get("skill_id") or "")
                    for item in session_manager.get_effective_run_skill_activations(session_id, run_id)
                    if str(item.get("skill_id") or "")
                )
            )
            if not active_skill_ids:
                return None
            prepare_host = getattr(self.workspace_backend, "prepare_host_execution", None)
            if not callable(prepare_host):
                return None
            projection = prepare_host(command, active_skill_ids=active_skill_ids)
            if not tuple(getattr(projection, "secret_values", ()) or ()):
                return None
        except Exception:  # noqa: BLE001
            # Failing to inspect the credential projection must not turn a
            # potentially tainted network command into an automatic allow.
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "credential_network_coupling:projection_unavailable",
                "high",
                explanation="无法证明本次联网命令未携带 Skill 凭证，必须由用户确认一次。",
            )
        return ToolPolicyResult(
            PolicyDecision.ASK,
            "credential_network_coupling:skill_secret_injected",
            "high",
            explanation="本次命令同时使用 Skill 凭证并访问网络；两项授权必须绑定，不能分别复用。",
        )

    @staticmethod
    def _contains_credential_literal(command: str) -> bool:
        return bool(
            re.search(
                r"(?i)(?:authorization\s*:\s*(?:bearer|basic)\s+\S+|"
                r"(?:cookie|x-api-key|api-key|token|password|passwd|secret)\s*[:=]\s*\S+|"
                r"--(?:token|password|passwd|api[-_]key|secret)(?:=|\s+)\S+)",
                str(command or ""),
            )
        )

    @staticmethod
    def _persistence_write(command: str, requirements: ExecutionRequirements) -> bool:
        has_write_intent = any(intent.access in {"write", "delete"} for intent in requirements.filesystem_intents)
        if not has_write_intent and (not requirements.opaque or not requirements.capabilities.workspace_write):
            return False
        return bool(_PERSISTENCE_TARGET_PATTERN.search(str(command or "")))

    def _permission_rule_result(
        self,
        *,
        request: ToolCallRequest,
        command: str,
        result: ToolPolicyResult,
        effects: ShellCapabilities,
    ) -> ToolPolicyResult | None:
        """Apply typed rules without allowing them to erase hard policy."""

        rules = self.permission_context.rules
        if not rules:
            return None
        tool_name = str(request.tool_call.get("name") or "")
        pattern = self._command_pattern(command)
        if pattern is None:
            return None
        workspace_path = str(self._context(request).get("workspace_path") or ".")
        scope = "none"
        try:
            requirements = self._execution_requirements(command, workspace_path=workspace_path)
            scopes: set[str] = set()
            workspace = Path(workspace_path).expanduser().resolve()
            scratch = (
                Path(str(getattr(self.workspace_backend, "scratch_path", ""))).expanduser().resolve()
                if getattr(self.workspace_backend, "scratch_path", None)
                else None
            )
            for intent in requirements.filesystem_intents:
                candidate = Path(intent.path).expanduser().resolve(strict=False)
                if candidate == workspace or workspace in candidate.parents:
                    scopes.add("workspace")
                elif scratch is not None and (candidate == scratch or scratch in candidate.parents):
                    scopes.add("scratch")
                else:
                    scopes.add("external")
            if scopes:
                scope = next(iter(scopes)) if len(scopes) == 1 else "mixed"
        except (OSError, ValueError):
            scope = "mixed"
        rule_decision = evaluate_permission_rules(
            rules,
            tool=tool_name,
            pattern=pattern,
            effects={
                "network": effects.network,
                "credentials": self._contains_credential_literal(command),
                "destructive": effects.destructive,
                "package_install": effects.package_install,
                "write_scope": scope if effects.workspace_write else "none",
            },
        )
        if rule_decision is None:
            return None
        if rule_decision is PermissionRuleDecision.DENY:
            return ToolPolicyResult(
                PolicyDecision.DENY,
                f"permission_rule_deny:{pattern}",
                "critical",
                source="permission_rule",
            )
        if rule_decision is PermissionRuleDecision.ASK:
            return ToolPolicyResult(
                PolicyDecision.ASK,
                f"permission_rule_ask:{pattern}",
                "medium",
                source="permission_rule",
            )
        if result.decision is PolicyDecision.DENY:
            return None
        return ToolPolicyResult(
            PolicyDecision.ALLOW,
            f"permission_rule_allow:{pattern}",
            "low" if not (effects.network or effects.package_install or effects.destructive) else "medium",
            source="permission_rule",
        )

    @staticmethod
    def _command_pattern(command: str) -> str | None:
        """Return a stable user-facing program/subcommand pattern."""

        try:
            segments = ShellPolicyAnalyzer.parse_segments(command)
        except ValueError:
            return None
        if len(segments) != 1:
            return None
        tokens = ShellPolicyAnalyzer.unwrap_command(segments[0])
        if not tokens:
            return None
        executable = Path(tokens[0]).name.lower()
        prefix = [executable]
        for token in tokens[1:]:
            if token.startswith("-"):
                prefix.append(token)
                continue
            if len(prefix) == 1:
                prefix.append(token)
            break
        return " ".join(prefix) + " *"

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
            action=self._redact_shell_preview(self._review_action(command, context)),
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
            or self.permission_context.backend_mode not in {"spawn", "docker", "adaptive", "kernel"}
            or self.backend_mode not in {"spawn", "docker", "adaptive", "kernel"}
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

    def _smart_sandbox_result(
        self,
        *,
        command: str,
        result: ToolPolicyResult,
        effects: ShellCapabilities,
    ) -> ToolPolicyResult | None:
        """Auto-approve ordinary work inside the selected execution profile.

        Smart mode is intended to remove approval noise for computation,
        validation, and normal project writes.  The deterministic analyzer has
        already denied privilege escalation and fixed dangerous effects before
        this hook runs.  Spawn receives the same policy convenience as Kernel;
        this is not an OS isolation claim. Network/package capabilities and
        explicitly destructive workspace operations remain user-controlled.
        """

        if (
            not self.permission_context.smart
            or self.permission_context.backend_mode not in {"spawn", "docker", "adaptive", "kernel"}
            or self.backend_mode not in {"spawn", "docker", "adaptive", "kernel"}
            or result.decision != PolicyDecision.ASK
        ):
            return None
        if effects.network or effects.package_install or effects.destructive:
            return None
        if result.reason in _SMART_DOCKER_DESTRUCTIVE_REASONS and not (
            self._smart_local_filesystem_unrestricted
            and result.reason == "managed_workspace_write:chmod"
            and self._smart_chmod_allowed(command)
        ):
            return None
        if result.reason.startswith("managed_workspace_write:find:"):
            return None
        script_entrypoint = self._script_entrypoint(command)
        # Unknown native programs remain a reviewer gray zone. Script entry
        # points are ordinary sandboxed computation: their effects are bounded
        # by the same profile as inline Python/Node and do not need a separate
        # executable-name allowlist.
        if (
            result.reason.startswith(
                (
                    "unknown_command:",
                    "shell_parse_failed",
                    "wrapper_without_command",
                    "node_command",
                    "python_tool:",
                )
            )
            and script_entrypoint is None
            and not self._smart_local_filesystem_unrestricted
        ):
            return None
        if self._smart_local_filesystem_unrestricted and result.reason.startswith(
            ("arbitrary_shell:", "unreadable_shell_script:", "dynamic_shell_execution:")
        ):
            # A shell reading commands from stdin, eval/source, or an
            # unreadable script has no statically reviewable effect surface.
            # Full filesystem authority is not permission to erase the
            # independent destructive/network/persistence effect boundary.
            return None
        if result.reason.startswith("managed_git_write:"):
            if not self._smart_git_write_allowed(command, result.reason):
                return None
        if (
            result.reason == "managed_workspace_write:mv"
            and not self._smart_local_filesystem_unrestricted
            and not self._smart_move_allowed(command)
        ):
            return None
        if result.reason.startswith(("external_command_hook:", "git_network")):
            return None

        if result.reason == "complex_shell_expansion" and self._smart_local_filesystem_unrestricted:
            expansion_result = self._smart_shell_expansion_policy(command)
            if expansion_result is not None:
                return expansion_result
        # Restricted profiles retain the conservative syntax boundary. Smart
        # trusted-local mode evaluates simple substitutions above and treats
        # newlines/heredocs as shell syntax, not as filesystem authority.
        if re.search(r"`|\$\(|\$\{", command) and not self._smart_local_filesystem_unrestricted:
            return None
        if _EMBEDDED_DESTRUCTIVE_API_PATTERN.search(command):
            return None

        return ToolPolicyResult(
            PolicyDecision.ALLOW,
            "smart_sandbox_workspace_write" if effects.workspace_write else "smart_sandbox_execute",
            "managed_write" if effects.workspace_write else "low",
        )

    def _smart_shell_expansion_policy(self, command: str) -> ToolPolicyResult | None:
        """Return an effect prompt only when a shell expansion is not safe local work."""

        arithmetic_expansions = re.findall(r"\$\(\(([^()]*)\)\)", command, re.DOTALL)
        if command.count("$((") != len(arithmetic_expansions) or any(
            not re.fullmatch(r"[A-Za-z0-9_+\-*/%<>=!&|^~?: \t.]*", expression) for expression in arithmetic_expansions
        ):
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "shell_effect_unprovable",
                "high",
                explanation="Shell 算术展开包含嵌套命令或无法确定的语法。",
            )
        command_without_arithmetic = re.sub(r"\$\(\([^()]*\)\)", "", command)
        dollar_substitutions = re.findall(r"\$\(([^()]*)\)", command_without_arithmetic, re.DOTALL)
        backtick_substitutions = re.findall(r"`([^`\n]*)`", command_without_arithmetic)
        process_substitutions = re.findall(r"[<>]\(([^()]*)\)", command_without_arithmetic, re.DOTALL)
        if (
            command_without_arithmetic.count("$(") != len(dollar_substitutions)
            or command_without_arithmetic.count("`") != 2 * len(backtick_substitutions)
            or len(re.findall(r"[<>]\(", command_without_arithmetic)) != len(process_substitutions)
        ):
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "shell_effect_unprovable",
                "high",
                explanation="命令包含嵌套或无法完整解析的 Shell/进程替换，无法确定其真实副作用。",
            )
        parameter_expansions = re.findall(r"\$\{([^{}\n]+)\}", command)
        if command.count("${") != len(parameter_expansions):
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "shell_effect_unprovable",
                "high",
                explanation="命令包含无法完整解析的 Shell 参数展开。",
            )
        if any(
            not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*(?:(?::?[-+?=])[^{}]*)?",
                expression,
            )
            for expression in parameter_expansions
        ):
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "shell_effect_unprovable",
                "high",
                explanation="Shell 参数展开超出确定性支持范围。",
            )
        heredoc_matches = list(
            re.finditer(
                r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n(.*?)\n\2(?:\n|$)",
                command,
                re.DOTALL,
            )
        )
        heredoc_operator_count = len(re.findall(r"<<-?(?!<)", command))
        if heredoc_operator_count != len(heredoc_matches):
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "shell_effect_unprovable",
                "high",
                explanation="命令包含无法完整解析的 heredoc。",
            )
        shell_heredoc_bodies: list[str] = []
        for match in heredoc_matches:
            prefix = command[: match.start()]
            try:
                prefix_segments = ShellPolicyAnalyzer.parse_segments(prefix)
            except ValueError:
                prefix_segments = []
            if prefix_segments:
                prefix_tokens = ShellPolicyAnalyzer.unwrap_command(prefix_segments[-1])
                if prefix_tokens and Path(prefix_tokens[0]).name.lower() in _SHELLS:
                    shell_heredoc_bodies.append(match.group(3))

        for nested_command in (
            *dollar_substitutions,
            *backtick_substitutions,
            *process_substitutions,
            *shell_heredoc_bodies,
        ):
            nested_effects = ShellPolicyAnalyzer.capabilities(nested_command)
            if nested_effects.network:
                return ToolPolicyResult(
                    PolicyDecision.ASK,
                    "network_access:shell_expansion",
                    "network",
                    explanation="Shell 命令替换内部需要联网。",
                )
            if nested_effects.package_install:
                return ToolPolicyResult(
                    PolicyDecision.ASK,
                    "package_management:shell_expansion",
                    "package_install",
                    explanation="Shell 命令替换内部会安装或更新依赖。",
                )
            if nested_effects.destructive or _OPAQUE_CRITICAL_ACTION_PATTERN.search(nested_command):
                return ToolPolicyResult(
                    PolicyDecision.ASK,
                    "destructive_shell_expansion",
                    "high",
                    explanation="Shell 命令替换内部包含删除或高风险系统修改。",
                )
            nested_result = ShellPolicyAnalyzer(
                workspace_path="/__puddingclaw_shell_expansion__",
                backend_mode=self._effective_backend_mode(),
                filesystem_mode="unrestricted",
            ).analyze(nested_command)
            ordinary_nested_write = (
                nested_result.decision is PolicyDecision.ASK
                and (
                    nested_result.reason == "shell_redirection"
                    or nested_result.reason.startswith("managed_workspace_write:")
                )
                and nested_result.reason not in _SMART_DOCKER_DESTRUCTIVE_REASONS
            )
            if nested_result.decision is not PolicyDecision.ALLOW and not ordinary_nested_write:
                return ToolPolicyResult(
                    PolicyDecision.ASK,
                    "shell_effect_unprovable",
                    "high",
                    explanation="Shell 命令替换内部效果无法被确定性分析。",
                )
        return None

    def _smart_network_result(
        self,
        *,
        command: str,
        result: ToolPolicyResult,
        effects: ShellCapabilities,
    ) -> ToolPolicyResult | None:
        """Allow a narrow public HTTPS read without turning on raw-network trust."""

        if (
            not self.permission_context.smart
            or self.permission_context.backend_mode not in {"spawn", "docker", "adaptive", "kernel"}
            or self.backend_mode not in {"spawn", "docker", "adaptive", "kernel"}
            or not effects.network
            or effects.package_install
            or effects.destructive
            or result.decision is PolicyDecision.DENY
            or not self._smart_public_curl_read(command)
        ):
            return None
        return ToolPolicyResult(
            PolicyDecision.ALLOW,
            "smart_controlled_network:curl_public_https_read",
            "network",
        )

    @staticmethod
    def _script_entrypoint(command: str) -> str | None:
        try:
            segments = ShellPolicyAnalyzer.parse_segments(command)
        except ValueError:
            return None
        if len(segments) != 1:
            return None
        tokens = ShellPolicyAnalyzer.unwrap_command(segments[0])
        if not tokens:
            return None
        executable = Path(tokens[0]).name.lower()
        if executable in {"python", "python3", "node", "ruby", "perl", "php", "sh", "bash", "zsh"}:
            return executable
        if Path(tokens[0]).suffix.lower() in {".py", ".js", ".mjs", ".cjs", ".rb", ".pl", ".php", ".sh", ".zsh"}:
            return executable
        return None

    @classmethod
    def _smart_public_curl_read(cls, command: str) -> bool:
        """Recognize a public curl download plus inert local verification.

        ``curl -L`` is the normal spelling for downloads from arXiv, GitHub,
        and object stores.  Redirect syntax alone is not a WebBridge bypass:
        a GET/HEAD request cannot invoke the daemon's POST-only command API.
        Keep credentials, request bodies, proxy/target overrides, and trusted
        redirect forwarding outside this fast path, while allowing a download
        to be followed by deterministic ``file``/``ls``/hash checks.
        """

        if any(marker in command for marker in ("$", "`", "\n", "\r")):
            return False
        try:
            segments, operators, has_redirect = ShellPolicyAnalyzer._requirements_structure(command)
        except ValueError:
            return False
        if not segments or has_redirect or any(operator != "&&" for operator in operators):
            return False
        tokens = ShellPolicyAnalyzer.unwrap_command(segments[0])
        if not cls._smart_public_curl_tokens_read(tokens):
            return False
        verification_commands = {"file", "ls", "stat", "wc", "shasum", "sha256sum", "md5"}
        for raw_segment in segments[1:]:
            verification_tokens = ShellPolicyAnalyzer.unwrap_command(raw_segment)
            if (
                not verification_tokens
                or Path(verification_tokens[0]).name.lower() not in verification_commands
            ):
                return False
            effects = ShellPolicyAnalyzer.capabilities(shlex.join(verification_tokens))
            if effects.network or effects.workspace_write or effects.package_install or effects.destructive:
                return False
        return True

    @classmethod
    def _smart_public_curl_tokens_read(cls, tokens: list[str]) -> bool:
        if not tokens or Path(tokens[0]).name.lower() != "curl":
            return False
        lowered = [item.lower() for item in tokens[1:]]
        forbidden_flags = {
            "-d",
            "--data",
            "--data-ascii",
            "--data-binary",
            "--data-raw",
            "--data-urlencode",
            "-f",
            "--form",
            "--form-string",
            "-t",
            "--upload-file",
            "--json",
            "-u",
            "--user",
            "-h",
            "--header",
            "-b",
            "--cookie",
            "-c",
            "--cookie-jar",
            "-k",
            "--config",
            "--netrc",
            "--netrc-file",
            "--cert",
            "--key",
            "--oauth2-bearer",
            "--aws-sigv4",
            "--location-trusted",
            "--proxy",
            "--proxy-user",
            "--preproxy",
            "--socks4",
            "--socks4a",
            "--socks5",
            "--socks5-hostname",
            "-x",
            "--resolve",
            "--connect-to",
            "--url",
            "--request-target",
            "--unix-socket",
            "--abstract-unix-socket",
        }
        unsafe_attached_short_flags = ("-d", "-F", "-T", "-u", "-H", "-b", "-c", "-K", "-k", "-x", "-X", "-E", "-U")
        if any(
            item in forbidden_flags
            or any(item.startswith(f"{flag}=") for flag in forbidden_flags if flag.startswith("--"))
            or item.startswith("@")
            or any(
                original.startswith(flag)
                for flag in unsafe_attached_short_flags
            )
            for item, original in zip(lowered, tokens[1:], strict=True)
        ):
            return False
        for index, item in enumerate(lowered):
            if item in {"-x", "--request"}:
                if index + 1 >= len(lowered) or lowered[index + 1].upper() not in {"GET", "HEAD"}:
                    return False
            if item.startswith("--request=") and item.partition("=")[2].upper() not in {"GET", "HEAD"}:
                return False
        urls = [item for item in tokens[1:] if item.lower().startswith(("http://", "https://"))]
        if not urls:
            return False
        intent = ShellPolicyAnalyzer.network_intent(shlex.join(tokens))
        if not intent.target_known or intent.remote_effect != "read" or len(intent.origins) != len(urls):
            return False
        return all(cls._public_https_url(url) for url in urls)

    @staticmethod
    def _curl_follows_redirects(tokens: list[str]) -> bool:
        for token in tokens[1:]:
            if token in {"-L", "--location", "--location-trusted"}:
                return True
            if token.startswith("--location=") or token.startswith("--location-trusted="):
                return True
            if token.startswith("-") and not token.startswith("--") and "L" in token[1:]:
                return True
        return False

    @staticmethod
    def _public_https_url(raw_url: str) -> bool:
        try:
            parsed = urlsplit(raw_url)
            port = parsed.port
        except ValueError:
            return False
        if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
            return False
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname == "localhost" or hostname.endswith(
            (
                ".localhost",
                ".local",
                ".internal",
                ".home",
                ".lan",
                ".home.arpa",
                ".test",
                ".example",
                ".invalid",
                ".onion",
            )
        ):
            return False
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            literal = None
        if literal is not None:
            if getattr(literal, "ipv4_mapped", None) is not None:
                literal = literal.ipv4_mapped
            if not literal.is_global:
                return False
        elif "." not in hostname:
            # Single-label names commonly resolve only through a local search
            # domain and are not a statically identifiable public target.
            return False
        return port in {None, 443}

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

    @staticmethod
    def _smart_chmod_allowed(command: str) -> bool:
        """Allow one reversible, non-recursive chmod on an ordinary target."""

        try:
            segments = ShellPolicyAnalyzer.parse_segments(command)
        except ValueError:
            return False
        found = False
        protected_roots = {
            "/",
            "/Users",
            "/home",
            str(Path.home().expanduser().resolve()),
        }
        for segment in segments:
            tokens = ShellPolicyAnalyzer.unwrap_command(segment)
            if not tokens or Path(tokens[0]).name.lower() != "chmod":
                continue
            found = True
            args = tokens[1:]
            if any(item in {"-R", "--recursive"} or (item.startswith("-") and "R" in item[1:]) for item in args):
                return False
            positional = [item for item in args if not item.startswith("-")]
            if len(positional) != 2:
                return False
            mode, target = positional
            if not (
                re.fullmatch(r"[0-7]{3,4}", mode)
                or re.fullmatch(r"[ugoa]*[+=-][rwxXstugo]+(?:,[ugoa]*[+=-][rwxXstugo]+)*", mode)
            ):
                return False
            if len(mode) == 4 and mode.isdigit() and mode[0] != "0":
                # setuid/setgid/sticky changes stay an explicit effect.
                return False
            normalized_target = target.replace("\\", "/").rstrip("/") or "/"
            if (
                normalized_target in protected_roots
                or any(char in normalized_target for char in "*?[")
                or ".." in Path(normalized_target).parts
            ):
                return False
        return found

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
            if isinstance(run, dict) and isinstance(run.get("verification_contract"), dict)
            else {}
        )
        return bool(contract.get("browser_e2e_required"))

    @staticmethod
    def _managed_container_path(raw: str) -> bool:
        normalized = raw.replace("\\", "/")
        if any(char in normalized for char in "*?[") or ".." in Path(normalized).parts:
            return False
        if normalized.startswith("/"):
            return (
                normalized == "/workspace"
                or normalized.startswith("/workspace/")
                or normalized == "/scratch"
                or normalized.startswith("/scratch/")
            )
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

    @staticmethod
    def _contains_webbridge_direct_access(command: str) -> bool:
        """Hard-deny shell attempts to bypass the structured browser tool."""

        candidate = str(command or "")
        for _ in range(4):
            lowered = candidate.lower()
            if re.search(r"(?:^|[\s/])(?:kimi-webbridge|kimi_webbridge|qweb-bridge|qweb_bridge)(?:[\s/]|$)", lowered):
                return True
            if re.search(
                r"\b(?:python(?:\d+(?:\.\d+)*)?|node|deno)\b[^\n;&|]*\s(?:-c|-e|--eval(?:=|\s))", lowered
            ) and re.search(r"\b(?:base64|b64decode|buffer\.from|atob)\b", lowered):
                # Obfuscated inline interpreters cannot be inspected for a
                # loopback target by the shell policy. Keep them out of the
                # bypass path instead of turning them into ordinary ASK.
                return True
            if "10086" not in lowered and "%31%30%30%38%36" not in lowered:
                decoded = unquote(candidate)
                if decoded == candidate:
                    break
                candidate = decoded
                continue
            # Literal and encoded loopback spellings, including IPv4 integer
            # and hexadecimal forms accepted by common URL parsers.
            loopback_tokens = (
                "127.0.0.1",
                "127.0.0.1.",
                "localhost",
                "::1",
                "[::1]",
                "0:0:0:0:0:0:0:1",
                "[0:0:0:0:0:0:0:1]",
                "::ffff:127.0.0.1",
                "2130706433",
                "0x7f000001",
            )
            if any(token in lowered for token in loopback_tokens):
                return True
            decoded = unquote(candidate)
            if decoded == candidate:
                break
            candidate = decoded
        return False

    @classmethod
    def _contains_webbridge_indirect_access(cls, command: str) -> bool:
        """Reject shell network indirection that can hide the daemon target."""

        candidate = str(command or "")
        for _ in range(3):
            decoded = unquote(candidate)
            if decoded == candidate:
                break
            candidate = decoded
        try:
            parsed_segments = ShellPolicyAnalyzer.parse_segments(candidate)
        except ValueError:
            # Unparseable shell stays fail-closed at this daemon-bypass
            # boundary.  Parsed compound commands are assessed per segment so
            # an unrelated later ``echo "$(cat file)"`` cannot make an
            # ordinary Python/Node file operation look like hidden network
            # access.
            parsed_segments = []
        if not parsed_segments:
            # Preserve fail-closed handling for malformed commands that name
            # a network-capable executable anywhere in the opaque surface.
            lowered = candidate.lower()
            return bool(
                re.search(r"\b(curl|wget|httpie|python|python3|node|deno)\b", lowered)
                and re.search(
                    r"(?:--config(?:=|\s)|\s-k(?:\s|$)|\s-k\S|--input-file(?:=|\s)|"
                    r"--proxy(?:=|\s)|--resolve(?:=|\s)|--connect-to(?:=|\s)|"
                    r"--location-trusted(?:=|\s|$)|\$\(|`|\$\{)",
                    lowered,
                )
            )
        for raw_segment in parsed_segments:
            raw_lowered = shlex.join(raw_segment).lower()
            if re.search(
                r"(?:\$\(|`)\s*(?:(?:env|command|sudo)\s+)*(?:curl|wget|httpie|python|python3|node|deno)\b",
                raw_lowered,
            ):
                return True
            segment_tokens = ShellPolicyAnalyzer.unwrap_command(raw_segment)
            if not segment_tokens:
                continue
            executable = Path(segment_tokens[0]).name.lower()
            if executable not in {"curl", "wget", "httpie", "python", "python3", "node", "deno"}:
                continue
            if (
                executable == "curl"
                and cls._curl_follows_redirects(segment_tokens)
                and not cls._smart_public_curl_tokens_read(segment_tokens)
            ):
                # Redirects are safe on the no-credential GET/HEAD fast path.
                # Any request body, auth material, target override, or private
                # destination keeps the original fail-closed behavior.
                return True
            lowered = shlex.join(segment_tokens).lower()
            # These options move the destination out of the command text or
            # make redirects/proxies authoritative, so lexical inspection
            # cannot prove that this specific network-capable segment will
            # not reach loopback:10086.
            if re.search(
                r"(?:--config(?:=|\s)|\s-k(?:\s|$)|\s-k\S|--input-file(?:=|\s)|"
                r"--proxy(?:=|\s)|--resolve(?:=|\s)|--connect-to(?:=|\s)|"
                r"--location-trusted(?:=|\s|$)|\$\(|`|\$\{)",
                lowered,
            ):
                return True
        return False

    def _action_preview(self, request: ToolCallRequest) -> str:
        tool_name = str(request.tool_call.get("name") or "")
        if tool_name == "execute":
            return self._redact_shell_preview(self._command(request))
        args = request.tool_call.get("args") or {}
        if tool_name == "browser" and isinstance(args, dict):
            try:
                from connectors.kimi_webbridge.policy import redact_browser_args

                args = {
                    **args,
                    "args": redact_browser_args(args.get("args") or {}),
                }
            except Exception:
                args = {"action": str(args.get("action") or "browser"), "args": "<redacted>"}
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

    @staticmethod
    def _redact_shell_preview(command: str) -> str:
        """Remove literal credentials before an approval/UI/audit boundary."""

        redacted = str(command or "")
        redacted = re.sub(
            r"(?i)(\b(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|api-key|token|password|passwd|secret)\s*[:=]\s*)(?:(?:bearer|basic)\s+)?([^\s,;]+)",
            r"\1<redacted>",
            redacted,
        )
        redacted = re.sub(
            r"(?i)(\b(?:bearer|basic)\s+)([^\s'\"]+)",
            r"\1<redacted>",
            redacted,
        )
        redacted = re.sub(
            r"(?i)(--?(?:password|passwd|token|api[-_]key|secret)(?:=|\s+))([^\s'\"]+)",
            r"\1<redacted>",
            redacted,
        )
        return redacted

    @staticmethod
    def _permission_fingerprint_command(request: ToolCallRequest, preview: str) -> str:
        """Hash the complete browser action while keeping its preview redacted."""

        if str(request.tool_call.get("name") or "") != "browser":
            return preview
        args = request.tool_call.get("args") or {}
        try:
            from connectors.kimi_webbridge.models import BrowserCommand

            command = BrowserCommand.model_validate(args)
            canonical = command.model_dump(mode="json")
            return json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return repr(args)

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
    def _managed_cli_preflight(cls, plan: Any) -> ToolPolicyResult:
        """Apply the frozen Adapter decision instead of re-parsing managed argv.

        Managed Provider operations run in the credential control plane, where
        networking is an execution requirement rather than a per-command
        permission prompt.  Installer mutations and deletion semantics retain
        their explicit approval boundaries.
        """

        match = getattr(plan, "match", plan)
        route = str(getattr(getattr(match, "route", None), "value", ""))
        requires_profile = bool(getattr(match, "requires_profile", False))
        requires_network = bool(getattr(match, "requires_network", False))
        credential_state = getattr(match, "credential_state", None)
        if requires_profile and credential_state is None:
            return ToolPolicyResult(
                PolicyDecision.DENY,
                "managed_cli_credential_contract_missing",
                "critical",
                explanation="该 Managed CLI 声明需要凭证，但 Adapter 没有提供受约束的凭证状态契约。",
            )
        if credential_state is not None and not requires_profile:
            return ToolPolicyResult(
                PolicyDecision.DENY,
                "managed_cli_credential_contract_unexpected",
                "critical",
                explanation="Adapter 不能在未声明 profile 需求时注入凭证状态。",
            )
        if requires_profile and requires_network:
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "managed_cli_credential_network_authorization",
                "high",
                explanation=(
                    "本次 Managed CLI 同时使用用户凭证和网络；必须由用户批准这对绑定，网络授权或凭证授权不能单独复用。"
                ),
            )
        if route == "installer":
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "managed_cli_toolchain_install",
                "package_install",
                explanation="受管 Toolchain 安装或更新需要确认。",
            )
        if bool(getattr(match, "destructive", False)):
            return ToolPolicyResult(
                PolicyDecision.ASK,
                "managed_cli_destructive_action",
                "high",
                explanation="受管 CLI 删除或撤销操作仍需用户明确确认。",
            )
        return ToolPolicyResult(
            PolicyDecision.ALLOW,
            "managed_cli_personal_autonomy",
            "managed_write",
            explanation="受管 CLI 非删除操作按 Adapter 契约执行。",
        )

    def _required_capabilities(
        self,
        request: ToolCallRequest,
        *,
        managed_cli: Any | None = None,
    ) -> list[str]:
        if managed_cli is not None:
            match = getattr(managed_cli, "match", managed_cli)
            route = str(getattr(getattr(match, "route", None), "value", ""))
            capabilities = ["execute"]
            if bool(getattr(match, "requires_network", False)):
                capabilities.append("network_access")
            credential_state = getattr(match, "credential_state", None)
            if bool(getattr(match, "requires_profile", False)) and credential_state is not None:
                capabilities.append(f"credential_profile:{credential_state.fingerprint}")
            if route == "installer":
                capabilities.extend(["package_install", "temporary_network"])
            if bool(getattr(match, "workspace_writable", False)):
                capabilities.append("managed_write")
            if bool(getattr(match, "destructive", False)):
                capabilities.append("destructive_write")
            return list(dict.fromkeys(capabilities))
        tool_name = str(request.tool_call.get("name") or "")
        if tool_name == "execute" and self._managed_npx_skills_add(self._command(request)) is not None:
            return ["execute", "temporary_network"]
        if tool_name == "install_packages":
            return ["execute", "package_install", "temporary_network"]
        if tool_name == "browser":
            args = request.tool_call.get("args") or {}
            action = str(args.get("action") or "") if isinstance(args, dict) else ""
            capabilities = ["execute", "browser_control"]
            if action in {"click", "fill", "close_tab", "close_session"}:
                capabilities.append("browser_final_action")
            elif action == "navigate":
                capabilities.append("browser_navigation")
            return capabilities
        if tool_name in {"prepare_skill_install", "prepare_skill_update"}:
            return ["execute", "temporary_network"]
        if tool_name in self.SKILL_COMMIT_TOOLS:
            return ["execute", "managed_skill_write"]
        if tool_name in {"fetch_url", "web_search", "tavily_search"}:
            return ["execute", "network_access"]
        if tool_name in self.mcp_tool_names:
            return ["execute", "network_access"]
        capabilities = ["execute"]
        if tool_name in {"execute", "execute_external_directory"}:
            context = self._context(request)
            effects = ShellPolicyAnalyzer.capabilities(
                self._command(request),
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
                or path_value == "/opt/puddingclaw/bin/validate-html-report-e2e.mjs"
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
            return (len(args) >= 2 and args[0] == "--check") or (
                len(args) == 2
                and args[0] == "/opt/puddingclaw/bin/validate-html-report-e2e.mjs"
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
        if any(marker in command for marker in ("$", "`", "\n", "\r", ">", "<", "|", ";")):
            return False
        if "&" in command.replace("&&", ""):
            return False
        try:
            segments = ShellPolicyAnalyzer.parse_segments(command)
        except ValueError:
            return False
        if len(segments) < 2 or "&&" not in command:
            return False
        argv_segments = [ShellPolicyAnalyzer.unwrap_command(segment) for segment in segments]
        if any(not argv for argv in argv_segments):
            return False
        if any(not cls._safe_external_argv_paths(argv) for argv in argv_segments):
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
                    token == "/external-workspace" or token.startswith("/external-workspace/")
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
        if tool_name in {"web_search", "tavily_search"}:
            return {
                "target_kind": "network_profile",
                "target": "web_search:configured",
                "label": "本 Session 允许使用已配置的联网搜索服务",
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
            # A dependency mutation is bound to one exact Skill + complete
            # desired-set diff.  A reusable capability grant would allow a
            # later unrelated package set to ride the first approval.
            return None
        # Browser approvals are intentionally once-only. A Session grant must
        # never turn into standing authority over the user's live tab state.
        if tool_name == "browser":
            return None
        if tool_name != "execute":
            return None

        command = str(request.tool_call.get("args", {}).get("command") or "").strip()
        if not command:
            return None
        try:
            capabilities = ShellPolicyAnalyzer.capabilities(
                command,
                workspace_path=self.base_dir,
            )
        except (OSError, RuntimeError, ValueError):
            return None

        # Pattern reuse is deliberately limited to commands whose effects are
        # local and non-destructive. Network, credentials, package mutation,
        # and destructive shell actions remain exact-once even when the
        # command text looks stable.
        if capabilities.network or capabilities.destructive or capabilities.package_install:
            return None
        # A stable-looking interpreter prefix is not a stable effect
        # identity.  `python/node -c`, a script path, or a shell wrapper can
        # change arbitrary code while keeping the same executable.  These
        # actions remain exact-once; typed rules may still ask explicitly.
        if self._script_entrypoint(command) is not None:
            return None
        pattern = self._command_pattern(command)
        if pattern is None:
            return None
        return {
            "target_kind": "command_pattern",
            "target": pattern,
            "label": f"本 Session 允许重复执行命令模式 {pattern}",
        }

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
