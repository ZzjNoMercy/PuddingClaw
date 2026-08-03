"""Evaluation data-loss prevention and provider-neutral redaction profiles."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

DEFAULT_REDACTION_PROFILE = "default-v1"
SUPPORTED_REDACTION_PROFILES = frozenset({DEFAULT_REDACTION_PROFILE})

_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?key|private[_-]?key|token|secret|password|passwd|authorization|cookie)",
    re.I,
)
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.S,
)
_CREDENTIAL_URI = re.compile(
    r"\b([a-z][a-z0-9+.-]*://)([^\s/:@]+):([^\s/@]+)@",
    re.I,
)
_AUTHORIZATION = re.compile(
    r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+"
)
_TOKEN = re.compile(
    r"(?i)\b(?:sk-|lsv2_|gh[opusr]_|xox[baprs]-)[A-Za-z0-9._~-]{8,}"
)
_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?key|token|secret|password|passwd)\s*[=:]\s*[^\s,;]+"
)
_EMAIL = re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.I)
_POSIX_USER_PATH = re.compile(r"(?<!\w)/(?:Users|home)/[^\s,;]+")
_WINDOWS_USER_PATH = re.compile(r"(?i)\b[A-Z]:\\Users\\[^\s,;]+")

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", _PEM_PRIVATE_KEY),
    ("credential_uri", _CREDENTIAL_URI),
    ("authorization", _AUTHORIZATION),
    ("provider_token", _TOKEN),
    ("aws_access_key", _AWS_ACCESS_KEY),
    ("secret_assignment", _SECRET_ASSIGNMENT),
)


@dataclass(frozen=True)
class SensitiveFinding:
    kind: str
    path: str


def _redact_string(value: str, *, max_string: int) -> str:
    value = _PEM_PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", value)
    value = _CREDENTIAL_URI.sub(r"\1[REDACTED_CREDENTIALS]@", value)
    value = _AUTHORIZATION.sub(r"\1[REDACTED]", value)
    value = _TOKEN.sub("[REDACTED_TOKEN]", value)
    value = _AWS_ACCESS_KEY.sub("[REDACTED_AWS_ACCESS_KEY]", value)
    value = _SECRET_ASSIGNMENT.sub("[REDACTED_SECRET]", value)
    value = _EMAIL.sub("[REDACTED_EMAIL]", value)
    value = _POSIX_USER_PATH.sub("[REDACTED_USER_PATH]", value)
    value = _WINDOWS_USER_PATH.sub("[REDACTED_USER_PATH]", value)
    if len(value) > max_string:
        return value[:max_string] + "…[TRUNCATED]"
    return value


def redact(value: Any, *, profile: str = DEFAULT_REDACTION_PROFILE, max_string: int = 8_000) -> Any:
    """Apply a named, fail-closed redaction profile to a nested payload."""

    if profile not in SUPPORTED_REDACTION_PROFILES:
        raise ValueError(f"Unsupported redaction profile: {profile}")
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if _SENSITIVE_KEY.search(str(key))
            else redact(item, profile=profile, max_string=max_string)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, profile=profile, max_string=max_string) for item in value]
    if isinstance(value, str):
        return _redact_string(value, max_string=max_string)
    return value


def find_plaintext_secrets(value: Any, *, path: str = "$") -> list[SensitiveFinding]:
    """Find credentials that must block publication without returning their values."""

    findings: list[SensitiveFinding] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}"
            empty_or_redacted = item is None or item == "" or item == "[REDACTED]"
            if _SENSITIVE_KEY.search(str(key)) and not empty_or_redacted:
                findings.append(SensitiveFinding(kind="sensitive_field", path=child_path))
                continue
            findings.extend(find_plaintext_secrets(item, path=child_path))
        return findings
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            findings.extend(find_plaintext_secrets(item, path=f"{path}[{index}]"))
        return findings
    if isinstance(value, str):
        for kind, pattern in _SECRET_PATTERNS:
            if pattern.search(value):
                findings.append(SensitiveFinding(kind=kind, path=path))
    return findings
