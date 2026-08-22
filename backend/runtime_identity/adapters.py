"""Adapter-first classification for trusted shared CLIs.

This module never executes a command.  It turns a narrowly supported,
standalone shell surface into immutable argv and routing metadata.  Unknown or
compound syntax remains outside the managed identity control plane.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit


class ManagedCliRoute(StrEnum):
    INSTALLER = "installer"
    PROVIDER = "provider_runner"
    BROWSER_AUTH = "browser_auth_runner"


class ManagedCliAction(StrEnum):
    INSTALL = "install"
    LOCAL_INSPECTION = "local_inspection"
    CREDENTIAL_READ = "credential_read"
    CREDENTIAL_WRITE = "credential_write"
    PROVIDER_OPERATION = "provider_operation"
    BROWSER_AUTH = "browser_auth"
    AUTHORIZATION_RESUME = "authorization_resume"


@dataclass(frozen=True)
class ToolchainPackageSpec:
    """Trusted installation contract for one Adapter executable."""

    ecosystem: str
    package: str
    executable: str
    compatibility: str = ""
    verification_argv: tuple[str, ...] = ("--version",)
    expected_integrity: str = ""

    @property
    def fingerprint(self) -> str:
        payload = {
            "ecosystem": self.ecosystem,
            "package": self.package,
            "executable": self.executable,
            "compatibility": self.compatibility,
            "verification_argv": list(self.verification_argv),
            "expected_integrity": self.expected_integrity,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def __post_init__(self) -> None:
        if self.ecosystem not in {"node"}:
            raise ValueError("managed Toolchain ecosystem is unsupported")
        if not re.fullmatch(
            r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*",
            self.package,
        ):
            raise ValueError("managed Toolchain package name is invalid")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.executable):
            raise ValueError("managed Toolchain executable is invalid")
        if not self.verification_argv or any(not value or "\x00" in value for value in self.verification_argv):
            raise ValueError("managed Toolchain verification argv is invalid")
        if self.expected_integrity and not re.fullmatch(
            r"sha512-[A-Za-z0-9+/]+={0,2}",
            self.expected_integrity,
        ):
            raise ValueError("managed Toolchain integrity must be an npm sha512 value")


@dataclass(frozen=True)
class ManagedConnectorSpec:
    """Product catalog metadata owned by one trusted Adapter."""

    connector_id: str
    display_name: str
    description: str
    capabilities: tuple[str, ...] = ()
    skill_prefix: str = ""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", self.connector_id):
            raise ValueError("managed Connector id is invalid")
        if not self.display_name.strip() or not self.description.strip():
            raise ValueError("managed Connector display metadata is incomplete")
        if any(not item.strip() for item in self.capabilities):
            raise ValueError("managed Connector capabilities are invalid")
        if self.skill_prefix and not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", self.skill_prefix):
            raise ValueError("managed Connector skill prefix is invalid")


@dataclass(frozen=True)
class CredentialStateSpec:
    """Adapter-owned provider state injected only into managed runners.

    ``paths`` are normalized paths relative to the runner's HOME. They are the
    complete allow-list for archive import/export and Vault validation. ``env``
    contains Backend-owned variables that keep provider-native state inside
    those roots; an Agent command cannot override them.
    """

    paths: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()
    schema_version: int = 1

    @property
    def fingerprint(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "paths": list(self.paths),
            "env": [list(item) for item in self.env],
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def __post_init__(self) -> None:
        if self.schema_version < 1:
            raise ValueError("credential state schema version must be positive")
        if not self.paths:
            raise ValueError("credential state requires at least one path")
        normalized_paths: list[str] = []
        for raw_path in self.paths:
            value = str(raw_path or "")
            path = PurePosixPath(value)
            if (
                not value
                or value.startswith("/")
                or "\\" in value
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
                or value != path.as_posix()
                or path in {PurePosixPath("."), PurePosixPath("")}
                or any(part in {"", ".", ".."} for part in path.parts)
                or any(part.startswith("-") for part in path.parts)
            ):
                raise ValueError("credential state paths must be normalized HOME-relative paths")
            normalized_paths.append(value)
        if len(set(normalized_paths)) != len(normalized_paths):
            raise ValueError("credential state paths must be unique")
        for index, path in enumerate(PurePosixPath(item) for item in normalized_paths):
            for other in (PurePosixPath(item) for item in normalized_paths[index + 1 :]):
                if path == other or path in other.parents or other in path.parents:
                    raise ValueError("credential state paths must not overlap")
        env_names: set[str] = set()
        reserved_env = {"HOME", "PATH", "NODE_PATH", "NODE_OPTIONS"}
        for name, value in self.env:
            if (
                not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
                or name in reserved_env
                or name.startswith("LD_")
                or not value
                or "\x00" in value
            ):
                raise ValueError("credential state environment is invalid")
            if name in env_names:
                raise ValueError("credential state environment names must be unique")
            env_names.add(name)
            if value.startswith("/"):
                state_prefixes = tuple(f"/home/puddingclaw/{path}" for path in normalized_paths)
                if not any(value == prefix or value.startswith(f"{prefix}/") for prefix in state_prefixes):
                    raise ValueError("credential state path environment must stay inside a declared root")


@dataclass(frozen=True)
class ManagedCliMatch:
    adapter_id: str
    action: ManagedCliAction
    route: ManagedCliRoute
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()
    provider: str | None = None
    requires_profile: bool = False
    requires_network: bool = False
    workspace_writable: bool = False
    distribution: str | None = None
    destructive: bool = False
    credential_state: CredentialStateSpec | None = None
    authorization_phase: str | None = None
    requested_identity: str | None = None


class UnsupportedManagedCliCommand(ValueError):
    """A managed executable was mentioned using an unsafe/unknown shape."""


_ENV_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_LARK_ALLOWED_ENV = frozenset(
    {
        "LARKSUITE_CLI_NO_SKILLS_NOTIFIER",
        "LARKSUITE_CLI_NO_UPDATE_NOTIFIER",
    }
)
_LARK_PACKAGE = re.compile(r"^@larksuite/cli(?:@([0-9A-Za-z][0-9A-Za-z.+_-]*))?$")
_GENERIC_NODE_CLI_PACKAGE = re.compile(
    r"^((?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*)"
    r"(?:@(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?))?$"
)
_DISPLAY_EXIT_DIAGNOSTIC = re.compile(
    # Agents habitually append a display-only fallback such as
    # `2>&1 || echo "EXIT:$?"` (with arbitrary echo wording).  The managed
    # runner already captures stderr and the real exit code, so the suffix
    # carries no information; strip it wholesale.  The stripped text never
    # reaches argv, and the remainder must still pass _shell_surface_error.
    r"(?:\s+2\s*>\s*&\s*1)?\s*\|\|\s*echo\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s*$"
)
_DISPLAY_REDIRECT = re.compile(r"\s+2\s*>\s*&\s*1\s*$")
_DESTRUCTIVE_LARK_TERMS = frozenset(
    {
        "clear",
        "delete",
        "destroy",
        "purge",
        "trash",
    }
)
_LARK_CREDENTIAL_STATE = CredentialStateSpec(
    # Migration-only contract for credential archives written by PuddingClaw
    # releases before the host-native runtime. New Lark calls do not inject
    # this env or export these paths; changing the fingerprint would prevent a
    # safe one-time import of an existing encrypted archive.
    paths=(".lark-cli", ".local/share/lark-cli"),
    env=(("LARKSUITE_CLI_DATA_DIR", "/home/puddingclaw/.lark-cli/.credential-data"),),
)
_LARK_TOOLCHAIN_PACKAGE = ToolchainPackageSpec(
    ecosystem="node",
    package="@larksuite/cli",
    executable="lark-cli",
    compatibility=">=1.0.0 <2.0.0",
)
_LARK_OPAQUE_VALUE_OPTIONS = frozenset(
    {
        "--body",
        "--content",
        "--data",
        "--description",
        "--markdown",
        "--text",
        "--title",
    }
)


def _lark_control_tokens(argv: tuple[str, ...] | list[str]) -> list[str]:
    """Return command/option tokens while excluding opaque content values."""

    values = [str(item).lower() for item in argv[1:]]
    controls: list[str] = []
    index = 0
    while index < len(values):
        value = values[index]
        option = value.partition("=")[0]
        if option in _LARK_OPAQUE_VALUE_OPTIONS:
            # ``--markdown=<value>`` contains its opaque value in this token;
            # the two-argv form consumes exactly the following value.
            index += 1 if "=" in value else 2
            continue
        controls.append(value)
        index += 1
    return controls


def is_lark_destructive_argv(argv: tuple[str, ...] | list[str]) -> bool:
    """Conservatively identify deletion/revocation semantics in Lark argv.

    Personal autonomy deliberately allows every non-delete provider action.
    False positives only add a confirmation; false negatives are additionally
    caught from lark-cli's structured exit-10 action before ``--yes`` retry.
    """

    values = _lark_control_tokens(argv)
    if values[:2] in (["config", "remove"], ["auth", "logout"]):
        return True
    for value in values:
        if value.startswith("--method=") and value.partition("=")[2] == "delete":
            return True
        if value == "--method":
            continue
        normalized = value.lstrip("+")
        terms = {part for part in re.split(r"[.:/_-]+", normalized) if part}
        if terms & _DESTRUCTIVE_LARK_TERMS:
            return True
    for index, value in enumerate(values[:-1]):
        if value == "--method" and values[index + 1] == "delete":
            return True
    return False


def _shell_surface_error(value: str) -> str | None:
    """Explain why raw text is not provably standalone argv, or return None.

    The managed runner executes normalized argv directly with no shell
    involved, so characters that are only dangerous under shell re-parsing
    (newlines, ``;``, ``|``, backticks, ``$( )`` inside quotes) are inert
    payload text and must round-trip byte-for-byte — Markdown messages
    legitimately contain backticks and ``$VAR`` fragments.  Only shell
    syntax OUTSIDE quotes is rejected: that is the only place it could
    chain commands.  This module's invariant is that normalized argv never
    reaches a shell; do not re-parse it with one downstream.
    """

    state = "outside"  # outside | single | double
    index = 0
    length = len(value)
    while index < length:
        char = value[index]
        nxt = value[index + 1] if index + 1 < length else ""
        if state == "outside":
            if char == "\\":
                index += 2  # escaped character is literal in argv too
                continue
            if char == "'":
                state = "single"
            elif char == '"':
                state = "double"
            elif char == "$" and nxt == "'":
                return (
                    "ANSI-C quoting $'...' is not supported in managed commands "
                    "(it corrupts payload fidelity); use plain double quotes"
                )
            elif char in "\n\r;&|<>":
                name = {"\n": "a newline", "\r": "a carriage return"}.get(char, repr(char))
                if char in "<>":
                    advice = (
                        "redirection is not supported in managed commands; "
                        "stdout and stderr are captured automatically, so drop "
                        "the redirect and run the bare command"
                    )
                else:
                    advice = (
                        "run exactly one command per call — chaining, pipes, and "
                        "echo separators are not supported; issue separate tool "
                        "calls and keep payload text inside quotes"
                    )
                return f"command contains {name} outside quotes, which would chain commands under a shell; {advice}"
            elif char == "`" or (char == "$" and nxt in "({"):
                return "command substitution is not allowed in managed commands"
        elif state == "single":
            if char == "'":
                state = "outside"
        else:  # double — inert payload until the closing quote
            if char == "\\":
                index += 2
                continue
            if char == '"':
                state = "outside"
        index += 1
    if state != "outside":
        return "unterminated quote in command"
    return None


def _standalone_argv(command: str) -> tuple[list[str], dict[str, str]] | None:
    value = str(command or "").strip()
    # Common Agent diagnostic plumbing (`... || echo "<wording>"`). The
    # managed runner already captures stderr and the real exit code, so
    # executing this shell fallback would add no information and would
    # incorrectly turn failures into shell success.  Strip the suffix;
    # arbitrary ``||`` commands with anything but a bare echo stay denied.
    value = _DISPLAY_EXIT_DIAGNOSTIC.sub("", value)
    # Trailing 2>&1 is display plumbing, not payload.  A match can only sit at
    # the absolute end of the string, which is necessarily outside quotes
    # (a quoted payload would end with its closing quote instead).
    value = _DISPLAY_REDIRECT.sub("", value)
    if not value or _shell_surface_error(value) is not None:
        return None
    try:
        tokens = shlex.split(value, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    if tokens[0] == "command":
        tokens = tokens[1:]
    if tokens[:1] == ["env"]:
        tokens = tokens[1:]
    env: dict[str, str] = {}
    while tokens:
        assignment = _ENV_ASSIGNMENT.fullmatch(tokens[0])
        if assignment is None:
            break
        env[assignment.group(1)] = assignment.group(2)
        tokens = tokens[1:]
    return (tokens, env) if tokens else None


def _npm_lark_install(tokens: list[str], env: dict[str, str]) -> ManagedCliMatch | None:
    if env or not tokens or Path(tokens[0]).name.lower() != "npm":
        return None
    args = tokens[1:]
    if not args or args[0].lower() not in {"install", "add", "i"}:
        return None
    global_seen = False
    packages: list[str] = []
    index = 1
    while index < len(args):
        token = args[index]
        lowered = token.lower()
        if lowered in {"-g", "--global"}:
            global_seen = True
        elif lowered in {"--save", "--no-save"}:
            pass
        elif lowered == "--ignore-scripts":
            raise UnsupportedManagedCliCommand(
                "managed Toolchain lifecycle-script policy is Adapter-owned; "
                "--ignore-scripts cannot be silently rewritten"
            )
        elif lowered.startswith("-"):
            return None
        else:
            packages.append(token)
        index += 1
    if not global_seen or len(packages) != 1 or _LARK_PACKAGE.fullmatch(packages[0]) is None:
        return None
    return ManagedCliMatch(
        adapter_id="lark-cli",
        action=ManagedCliAction.INSTALL,
        route=ManagedCliRoute.INSTALLER,
        argv=("npm", "install", "--global", packages[0]),
        requires_network=True,
        distribution=packages[0],
    )


def generic_node_cli_install(command: str) -> tuple[str, str] | None:
    """Parse one standalone, credentialless npm CLI installation request.

    This surface intentionally accepts only a registry package name, optionally
    pinned to an exact version. URLs, tags, ranges, package-manager flags,
    environment overrides, and compound shell syntax remain unsupported.
    """

    parsed = _standalone_argv(command)
    if parsed is None:
        return None
    tokens, env = parsed
    if env or not tokens or Path(tokens[0]).name.lower() != "npm":
        return None
    args = tokens[1:]
    if not args or args[0].lower() not in {"install", "add", "i"}:
        return None
    global_seen = False
    packages: list[str] = []
    for token in args[1:]:
        lowered = token.lower()
        if lowered in {"-g", "--global"}:
            global_seen = True
        elif token.startswith("-"):
            return None
        else:
            packages.append(token)
    if not global_seen or len(packages) != 1:
        return None
    matched = _GENERIC_NODE_CLI_PACKAGE.fullmatch(packages[0])
    if matched is None:
        return None
    return packages[0], matched.group(1)


def _claims_unregistered_toolchain_mutation(tokens: list[str]) -> bool:
    if not tokens:
        return False
    executable = Path(tokens[0]).name.lower()
    lowered = [item.lower() for item in tokens[1:]]
    if executable in {"npm", "pnpm", "yarn"}:
        return any(item in {"-g", "--global"} for item in lowered) and any(
            item in {"install", "add", "i", "remove", "uninstall", "update", "upgrade"} for item in lowered
        )
    if executable == "pipx":
        return bool(lowered and lowered[0] in {"install", "uninstall", "upgrade", "upgrade-all"})
    if executable == "uv":
        return bool(lowered[:1] == ["tool"] and len(lowered) > 1 and lowered[1] in {"install", "uninstall", "upgrade"})
    if executable == "cargo":
        return bool(lowered[:1] == ["install"])
    return False


def _lark_command(tokens: list[str], env: dict[str, str]) -> ManagedCliMatch | None:
    if not tokens or Path(tokens[0]).name.lower() != "lark-cli":
        return None
    if any(name not in _LARK_ALLOWED_ENV for name in env):
        raise UnsupportedManagedCliCommand("unsupported environment variable for managed lark-cli")
    if any(value not in {"1", "true"} for value in env.values()):
        raise UnsupportedManagedCliCommand("managed lark-cli environment flags must use a fixed enabled value")
    argv = tuple(["lark-cli", *tokens[1:]])
    lowered = [item.lower() for item in tokens[1:]]
    control_lowered = _lark_control_tokens(argv)
    requested_identity: str | None = None
    for index, item in enumerate(control_lowered):
        if item == "--as" and index + 1 < len(control_lowered):
            requested_identity = control_lowered[index + 1]
        elif item.startswith("--as="):
            requested_identity = item.partition("=")[2]
    if requested_identity not in {None, "bot", "user", "auto"}:
        raise UnsupportedManagedCliCommand("managed lark-cli --as must be bot, user, or auto")
    if any(item == "--profile" or item.startswith("--profile=") for item in lowered):
        raise UnsupportedManagedCliCommand(
            "lark-cli --profile is controlled by PuddingClaw Credential Profile selection"
        )
    if any(item in {"--yes", "-y"} or item.startswith("--yes=") or item.startswith("-y=") for item in lowered):
        raise UnsupportedManagedCliCommand(
            "lark-cli confirmation flags are added only by the managed high-risk action flow"
        )
    if (
        not lowered
        or any(item in {"-h", "--help"} for item in lowered)
        or lowered[0]
        in {
            "help",
            "version",
            "--version",
            "schema",
        }
    ):
        return ManagedCliMatch(
            adapter_id="lark-cli",
            action=ManagedCliAction.LOCAL_INSPECTION,
            route=ManagedCliRoute.PROVIDER,
            argv=argv,
            env=tuple(sorted(env.items())),
        )
    if lowered[:2] == ["config", "init"] and lowered != ["config", "init", "--new"]:
        raise UnsupportedManagedCliCommand("managed lark-cli config init requires the exact --new form")
    if lowered == ["config", "init", "--new"]:
        return ManagedCliMatch(
            adapter_id="lark-cli",
            action=ManagedCliAction.BROWSER_AUTH,
            route=ManagedCliRoute.BROWSER_AUTH,
            argv=argv,
            env=tuple(sorted(env.items())),
            provider="lark",
            requires_profile=True,
            requires_network=True,
            credential_state=_LARK_CREDENTIAL_STATE,
            authorization_phase="app_configuration",
        )
    if lowered[:2] == ["auth", "qrcode"]:
        if len(tokens) < 4:
            raise UnsupportedManagedCliCommand("managed lark-cli auth qrcode requires a URL")
        try:
            parsed_url = urlsplit(tokens[3])
        except ValueError as exc:
            raise UnsupportedManagedCliCommand("managed lark-cli auth qrcode URL is invalid") from exc
        if (
            parsed_url.scheme.lower() not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username
            or parsed_url.password
        ):
            raise UnsupportedManagedCliCommand("managed lark-cli auth qrcode requires an HTTP(S) URL")
        output_paths: list[str] = []
        index = 4
        while index < len(tokens):
            item = tokens[index]
            if item == "--ascii":
                index += 1
                continue
            if item == "--output" and index + 1 < len(tokens):
                output_paths.append(tokens[index + 1])
                index += 2
                continue
            if item.startswith("--output="):
                output_paths.append(item.partition("=")[2])
                index += 1
                continue
            raise UnsupportedManagedCliCommand("unsupported managed lark-cli auth qrcode option")
        for raw_path in output_paths:
            path = Path(raw_path)
            if path.is_absolute() or ".." in path.parts:
                raise UnsupportedManagedCliCommand("lark-cli QR output must stay inside the workspace")
        return ManagedCliMatch(
            adapter_id="lark-cli",
            action=ManagedCliAction.LOCAL_INSPECTION,
            route=ManagedCliRoute.PROVIDER,
            argv=argv,
            env=tuple(sorted(env.items())),
            workspace_writable=bool(output_paths),
        )
    if lowered[:2] == ["auth", "login"]:
        if any(item == "--device-code" or item.startswith("--device-code=") for item in lowered):
            raise UnsupportedManagedCliCommand(
                "device-code continuation is Backend-owned; use `lark-cli auth resume` after the user confirms"
            )
        if "--no-wait" not in lowered or "--json" not in lowered:
            raise UnsupportedManagedCliCommand(
                "managed lark-cli auth login initiation requires --no-wait --json so browser authorization can pause the Agent turn"
            )
        return ManagedCliMatch(
            adapter_id="lark-cli",
            action=ManagedCliAction.BROWSER_AUTH,
            route=ManagedCliRoute.BROWSER_AUTH,
            argv=argv,
            env=tuple(sorted(env.items())),
            provider="lark",
            requires_profile=True,
            requires_network=True,
            credential_state=_LARK_CREDENTIAL_STATE,
            authorization_phase="user_consent",
        )
    if lowered == ["auth", "resume"]:
        return ManagedCliMatch(
            adapter_id="lark-cli",
            action=ManagedCliAction.AUTHORIZATION_RESUME,
            route=ManagedCliRoute.PROVIDER,
            argv=argv,
            env=tuple(sorted(env.items())),
            provider="lark",
            requires_profile=True,
            requires_network=True,
            credential_state=_LARK_CREDENTIAL_STATE,
            authorization_phase="user_consent",
        )
    if lowered[:2] in (["config", "remove"], ["auth", "logout"]):
        action = ManagedCliAction.CREDENTIAL_WRITE
    elif lowered[:2] in (["config", "show"], ["auth", "status"]):
        action = ManagedCliAction.CREDENTIAL_READ
    elif lowered[:1] == ["update"]:
        raise UnsupportedManagedCliCommand("lark-cli update must be performed through the managed installer")
    else:
        action = ManagedCliAction.PROVIDER_OPERATION
    workspace_writable = any(
        item in {"download", "export", "save", "output"}
        or item.startswith(("--output=", "--out="))
        for item in control_lowered
    )
    return ManagedCliMatch(
        adapter_id="lark-cli",
        action=action,
        route=ManagedCliRoute.PROVIDER,
        argv=argv,
        env=tuple(sorted(env.items())),
        provider="lark",
        requires_profile=True,
        requires_network=True,
        workspace_writable=workspace_writable,
        destructive=is_lark_destructive_argv(argv),
        credential_state=_LARK_CREDENTIAL_STATE,
        requested_identity=requested_identity or "auto",
    )


@runtime_checkable
class ManagedCliAdapter(Protocol):
    """Trusted provider plug-in for CLI parsing and local state ownership.

    An Adapter is declarative from the runtime's point of view: it claims a
    narrow executable surface, converts it to immutable execution metadata,
    and publishes the package/state contracts that the Backend is allowed to
    install and persist.  It never receives a shell or filesystem handle.
    """

    adapter_id: str
    provider: str
    executables: frozenset[str]
    toolchain_package: ToolchainPackageSpec
    credential_state: CredentialStateSpec
    connector: ManagedConnectorSpec | None

    def claims(self, command: str) -> bool: ...

    def parse(
        self,
        tokens: tuple[str, ...],
        env: Mapping[str, str],
    ) -> ManagedCliMatch | None: ...


class LarkManagedCliAdapter:
    """Built-in Adapter for the trusted ``@larksuite/cli`` integration."""

    adapter_id = "lark-cli"
    provider = "lark"
    executables = frozenset({"lark-cli"})
    toolchain_package = _LARK_TOOLCHAIN_PACKAGE
    credential_state = _LARK_CREDENTIAL_STATE
    connector = ManagedConnectorSpec(
        connector_id="lark",
        display_name="飞书",
        description="消息、文档、云盘、日历、多维表格等飞书能力",
        capabilities=("消息", "文档", "云盘", "日历", "多维表格", "审批", "任务", "知识库"),
        skill_prefix="lark-",
    )
    _CLAIMED = re.compile(
        r"(?:^|\s|['\"])(?:[^\s'\"]*/)?lark-cli(?:\s|$|['\"])",
        re.IGNORECASE,
    )

    def claims(self, command: str) -> bool:
        return bool(self._CLAIMED.search(str(command or "")))

    def parse(
        self,
        tokens: tuple[str, ...],
        env: Mapping[str, str],
    ) -> ManagedCliMatch | None:
        install = _npm_lark_install(list(tokens), dict(env))
        if install is not None:
            return install
        return _lark_command(list(tokens), dict(env))


class ManagedCliRegistry:
    """Registry of Backend-owned adapters.

    Ordinary Skills and remote Markdown cannot mutate this registry.
    """

    def __init__(self, adapters: tuple[ManagedCliAdapter, ...] | None = None) -> None:
        configured = (LarkManagedCliAdapter(),) if adapters is None else adapters
        if not configured:
            raise ValueError("managed CLI registry requires at least one Adapter")
        adapter_ids = [item.adapter_id for item in configured]
        providers = [item.provider for item in configured]
        executables = [executable for item in configured for executable in item.executables]
        connector_ids = [
            connector.connector_id for item in configured if (connector := getattr(item, "connector", None)) is not None
        ]
        if len(set(adapter_ids)) != len(adapter_ids):
            raise ValueError("managed CLI Adapter ids must be unique")
        if len(set(providers)) != len(providers):
            raise ValueError("managed CLI providers must be unique")
        if len(set(executables)) != len(executables):
            raise ValueError("managed CLI executables must be owned by exactly one Adapter")
        if len(set(connector_ids)) != len(connector_ids):
            raise ValueError("managed Connector ids must be unique")
        for adapter in configured:
            if adapter.toolchain_package.executable not in adapter.executables:
                raise ValueError("managed CLI package executable must be claimed by its Adapter")
        self._adapters = configured
        self._by_id = {item.adapter_id: item for item in configured}
        self._by_provider = {item.provider: item for item in configured}

    def match(self, command: str) -> ManagedCliMatch | None:
        parsed = _standalone_argv(command)
        if parsed is None:
            if self.claims(command):
                surface = str(command or "").strip()
                normalized_surface = _DISPLAY_REDIRECT.sub(
                    "",
                    _DISPLAY_EXIT_DIAGNOSTIC.sub("", surface),
                )
                reason = _shell_surface_error(normalized_surface)
                raise UnsupportedManagedCliCommand(
                    reason
                    or "managed lark-cli commands must be standalone argv without pipes, redirects, or shell composition"
                )
            return None
        tokens, env = parsed
        matches: list[tuple[ManagedCliAdapter, ManagedCliMatch]] = []
        for adapter in self._adapters:
            match = adapter.parse(tuple(tokens), dict(env))
            if match is not None:
                self._validate_match(adapter, match)
                matches.append((adapter, match))
        if len(matches) > 1:
            raise UnsupportedManagedCliCommand("managed CLI command is claimed by multiple Adapters")
        if matches:
            return matches[0][1]
        if _claims_unregistered_toolchain_mutation(tokens):
            raise UnsupportedManagedCliCommand(
                "user-level CLI installation is Adapter-first; this distribution has no trusted ManagedCliAdapter"
            )
        if self.claims(command):
            raise UnsupportedManagedCliCommand(
                "managed CLI commands cannot be hidden behind wrappers or shell arguments"
            )
        return None

    def claims(self, command: str) -> bool:
        return any(adapter.claims(command) for adapter in self._adapters)

    @staticmethod
    def _validate_match(adapter: ManagedCliAdapter, match: ManagedCliMatch) -> None:
        """Fail closed when an Adapter returns metadata outside its contract."""

        if match.adapter_id != adapter.adapter_id:
            raise RuntimeError("managed CLI Adapter returned a foreign adapter id")
        if match.provider not in {None, adapter.provider}:
            raise RuntimeError("managed CLI Adapter returned a foreign provider")
        if match.credential_state not in {None, adapter.credential_state}:
            raise RuntimeError("managed CLI Adapter returned a foreign credential state")
        if match.route is ManagedCliRoute.INSTALLER:
            if match.action is not ManagedCliAction.INSTALL or not match.distribution:
                raise RuntimeError("managed CLI Adapter returned an invalid installer route")
            package = adapter.toolchain_package.package
            if match.distribution != package and not match.distribution.startswith(f"{package}@"):
                raise RuntimeError("managed CLI Adapter returned a foreign distribution")
            return
        if not match.argv or Path(match.argv[0]).name not in adapter.executables:
            raise RuntimeError("managed CLI Adapter returned a foreign executable")

    def adapter(self, adapter_id: str) -> ManagedCliAdapter:
        try:
            return self._by_id[adapter_id]
        except KeyError as exc:
            raise ValueError("managed CLI Adapter is not registered") from exc

    def adapters(self) -> tuple[ManagedCliAdapter, ...]:
        return self._adapters

    def validate_match(self, match: ManagedCliMatch) -> None:
        """Validate metadata received outside ``match()`` against the registry."""

        self._validate_match(self.adapter(match.adapter_id), match)

    def for_provider(self, provider: str) -> ManagedCliAdapter:
        try:
            return self._by_provider[provider]
        except KeyError as exc:
            raise ValueError("provider has no ManagedCliAdapter") from exc

    def state_for_provider(self, provider: str) -> CredentialStateSpec:
        return self.for_provider(provider).credential_state

    def state_specs(self) -> tuple[CredentialStateSpec, ...]:
        """Return every trusted state contract used to mask ordinary sandboxes."""

        return tuple(adapter.credential_state for adapter in self._adapters)

    def adapter_contract_fingerprint(self, adapter_id: str) -> str:
        adapter = self.adapter(adapter_id)
        payload = {
            "adapter_id": adapter.adapter_id,
            "provider": adapter.provider,
            "executables": sorted(adapter.executables),
            "toolchain_package": adapter.toolchain_package.fingerprint,
            "credential_state": adapter.credential_state.fingerprint,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    @staticmethod
    def credential_state_for_provider(provider: str) -> CredentialStateSpec:
        """Compatibility API for the process-wide built-in registry."""

        return ManagedCliRegistry().state_for_provider(provider)

    @staticmethod
    def credential_state_specs() -> tuple[CredentialStateSpec, ...]:
        """Compatibility API for the process-wide built-in registry."""

        return ManagedCliRegistry().state_specs()
