"""Deterministic, fail-closed policy for the WebBridge action surface."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

from .models import BrowserAction


class WebBridgePolicyError(ValueError):
    """Raised when a command is outside the explicitly implemented contract."""


READ_ONLY_ACTIONS = frozenset({"snapshot", "list_tabs", "find_tab", "scroll"})
ARTIFACT_ACTIONS = frozenset({"screenshot", "save_as_pdf"})
FINAL_ACTIONS = frozenset({"click", "fill", "close_tab", "close_session"})
SUPPORTED_ACTIONS = READ_ONLY_ACTIONS | ARTIFACT_ACTIONS | FINAL_ACTIONS | {"navigate"}

_PRIVATE_HOST_SUFFIXES = (".local", ".internal", ".lan", ".home.arpa")
_FINAL_WORDS = (
    "submit", "send", "publish", "post", "delete", "remove", "buy", "purchase",
    "pay", "checkout", "confirm", "authorize", "invite", "apply", "book",
    "下单", "购买", "支付", "提交", "发送", "发布", "删除", "授权", "邀请", "确认",
)
@dataclass(frozen=True)
class BrowserPolicyResult:
    decision: str
    reason: str
    risk: str
    target_kind: str | None = None
    target: str | None = None


def _url_needs_confirmation(raw_url: str) -> bool:
    try:
        parsed = urlsplit(raw_url.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return True
    if parsed.scheme.lower() not in {"http", "https"} or not host or parsed.username or parsed.password:
        return True
    if host in {"localhost", "localhost.localdomain"} or host.endswith(_PRIVATE_HOST_SUFFIXES):
        return True
    try:
        address = ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return True
    expected_port = 80 if parsed.scheme.lower() == "http" else 443
    return port not in {None, expected_port}


def _is_final_label(value: object) -> bool:
    normalized = str(value or "").strip().lower()
    return bool(normalized) and any(word in normalized for word in _FINAL_WORDS)


def classify_browser_command(action: str, args: Mapping[str, object]) -> BrowserPolicyResult:
    """Return the server-side authorization class for a browser command.

    The model cannot opt into a lower-risk class. A selector or snapshot
    reference is only an address into untrusted page state, never proof that an
    interaction is reversible. Every click/fill therefore requires one-time
    approval.
    """

    if action not in SUPPORTED_ACTIONS:
        return BrowserPolicyResult("deny", "browser_action_not_supported", "critical")
    if action == "find_tab" and args.get("active") is True:
        return BrowserPolicyResult(
            "ask", "browser_active_tab_selection_confirmation", "high",
            target_kind="browser_tab", target="active",
        )
    if action in READ_ONLY_ACTIONS:
        return BrowserPolicyResult("allow", f"browser_read_only:{action}", "browser_read_only")
    if action in ARTIFACT_ACTIONS:
        return BrowserPolicyResult("allow", f"browser_managed_artifact:{action}", "browser_artifact")
    if action == "navigate":
        url = str(args.get("url") or "")
        if _url_needs_confirmation(url):
            return BrowserPolicyResult(
                "ask", "browser_navigation_requires_confirmation", "high",
                target_kind="network_origin", target=url[:512],
            )
        origin = urlsplit(url).scheme + "://" + (urlsplit(url).netloc or "")
        return BrowserPolicyResult(
            "allow", "browser_navigation_public_url", "browser_navigation",
            target_kind="network_origin", target=origin.lower(),
        )
    label = " ".join(str(args.get(key) or "") for key in ("selector", "role", "name", "text"))
    reason = "browser_final_action_confirmation" if _is_final_label(label) else "browser_interaction_confirmation"
    return BrowserPolicyResult(
        "ask", reason, "browser_final_action" if _is_final_label(label) else "browser_interaction",
    )


def sanitize_action_args(action: BrowserAction, args: Mapping[str, object]) -> dict[str, object]:
    if action not in SUPPORTED_ACTIONS:
        raise WebBridgePolicyError("该 browser 动作尚未实现")
    normalized = dict(args)
    if action == "navigate" and _url_needs_confirmation(str(normalized.get("url") or "")):
        # The action is still supported, but the Harness must obtain a one-time
        # approval before service execution. Keep validation and authorization
        # separate so a direct service caller cannot mistake this for a deny.
        pass
    # Caller-controlled filesystem paths are never accepted. The service owns
    # the artifact path for screenshot/PDF and injects it after authorization.
    return normalized


def redact_browser_args(args: Mapping[str, object]) -> dict[str, object]:
    """Create an approval/log preview without exposing fill values."""

    redacted = dict(args)
    if "value" in redacted:
        value = str(redacted["value"] or "")
        redacted["value"] = f"<redacted:{len(value)} chars>"
    return redacted
