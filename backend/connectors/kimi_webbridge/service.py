"""Backend service for the browser tool."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from graph.attachment_store import AttachmentStore, attachment_store
from harness.execution_context import browser_action_digest, current_authorized_browser_action

from .adapter import KimiWebBridgeAdapter, WebBridgeError
from .artifacts import WebBridgeArtifactError, WebBridgeArtifactStore
from .lifecycle import KimiWebBridgeLifecycle
from .models import BrowserCommand, BrowserResult
from .policy import WebBridgePolicyError, classify_browser_command, sanitize_action_args
from .run_bindings import WebBridgeRunBindingError, WebBridgeRunBindingStore

_SENSITIVE_RESULT_KEYS = frozenset(
    {"cookie", "cookies", "set-cookie", "authorization", "token", "access-token", "password", "secret"}
)
_OUTCOME_UNKNOWN_ACTIONS = frozenset({"navigate", "click", "fill", "close_tab", "close_session"})
_AMBIGUOUS_TRANSPORT_ERRORS = frozenset({"daemon_timeout", "daemon_unreachable", "daemon_server_error"})
_TEXTAREA_FILL_BUG_EXTENSION_VERSIONS = frozenset({"1.11.5"})
_TEXTAREA_FILL_ERROR_MESSAGES = frozenset(
    {
        "fill: uncaught",
        "fill: illegal invocation",
        "fill: uncaught typeerror: illegal invocation",
    }
)
_TAB_TRANSITION_RESULT_KEY = "puddingclaw_tab_transition"
_TAB_TRANSITION_RETRY_DELAYS_SECONDS = (0.25, 0.5, 1.0, 2.0, 4.0)
_TAB_TRANSITION_RECOVERY_ACTIONS = frozenset({"list_tabs", "find_tab"})


def _explicit_command_success(payload: dict[str, Any]) -> bool | None:
    """Return the daemon's explicit success marker without guessing."""

    markers: list[bool] = []
    containers = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        containers.append(data)
    for container in containers:
        for key in ("ok", "success"):
            value = container.get(key)
            if isinstance(value, bool):
                markers.append(value)
    if any(value is False for value in markers):
        return False
    if any(value is True for value in markers):
        return True
    return None


def _sanitize_result(value: Any, *, depth: int = 0) -> Any:
    """Keep browser context useful without returning transport secrets."""

    if depth > 12:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key): _sanitize_result(item, depth=depth + 1)
            for key, item in value.items()
            if str(key).lower().replace("_", "-") not in _SENSITIVE_RESULT_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_result(item, depth=depth + 1) for item in value[:200]]
    return value


def _normalized_version(value: str | None) -> str:
    return str(value or "").strip().lower().removeprefix("v")


def _daemon_error_text(payload: dict[str, Any]) -> str:
    """Collect only error-shaped daemon fields for internal compatibility routing."""

    parts: list[str] = []

    def visit(value: Any, *, depth: int = 0) -> None:
        if depth > 5 or len(parts) >= 20:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = str(key).lower().replace("_", "-")
                if normalized_key in {"error", "message", "detail", "reason", "exception"}:
                    if isinstance(item, str):
                        parts.append(item[:500])
                    else:
                        visit(item, depth=depth + 1)
                elif normalized_key in {"data", "result"}:
                    visit(item, depth=depth + 1)
        elif isinstance(value, list):
            for item in value[:20]:
                visit(item, depth=depth + 1)

    visit(payload)
    return "\n".join(parts)


def _is_known_textarea_fill_bug(payload: dict[str, Any], *, extension_version: str | None) -> bool:
    if _normalized_version(extension_version) not in _TEXTAREA_FILL_BUG_EXTENSION_VERSIONS:
        return False
    error_lines = {
        line.strip().lower()
        for line in _daemon_error_text(payload).splitlines()
        if line.strip()
    }
    return bool(error_lines & _TEXTAREA_FILL_ERROR_MESSAGES)


def _payload_data(payload: dict[str, Any]) -> dict[str, Any] | None:
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def _payload_tabs(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    data = _payload_data(payload)
    tabs = data.get("tabs") if data is not None else None
    if not isinstance(tabs, list) or not all(isinstance(item, dict) for item in tabs):
        return None
    return tabs


def _artifact_result_path(payload: dict[str, Any]) -> object:
    """Accept both flat and daemon-nested artifact receipts internally."""

    nested = _payload_data(payload)
    if isinstance(nested, dict) and nested.get("path"):
        return nested.get("path")
    return payload.get("path")


def _without_artifact_paths(value: Any) -> Any:
    """Remove Backend-owned filesystem paths from a published artifact receipt."""

    if isinstance(value, dict):
        return {
            key: _without_artifact_paths(item)
            for key, item in value.items()
            if str(key).lower() != "path"
        }
    if isinstance(value, list):
        return [_without_artifact_paths(item) for item in value]
    return value


def _tab_id(value: Any) -> str:
    return str(value) if value is not None else ""


def _url_host(value: Any) -> str:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"}:
        return ""
    return str(parsed.hostname or "").lower().rstrip(".")


def _urls_match_exactly(requested: Any, returned: Any) -> bool:
    """Compare a model-requested tab URL without accepting same-origin guesses."""

    try:
        left = urlsplit(str(requested or "").strip())
        right = urlsplit(str(returned or "").strip())
    except ValueError:
        return False
    if left.scheme.lower() not in {"http", "https"} or right.scheme.lower() not in {"http", "https"}:
        return False
    left_path = left.path.rstrip("/") or "/"
    right_path = right.path.rstrip("/") or "/"
    return (
        left.scheme.lower() == right.scheme.lower()
        and left.netloc.lower() == right.netloc.lower()
        and left_path == right_path
        and left.query == right.query
    )


def _click_returned_link(payload: dict[str, Any]) -> bool:
    data = _payload_data(payload)
    return isinstance(data, dict) and str(data.get("tag") or "").upper() == "A"


def _with_tab_transition(payload: dict[str, Any], transition: dict[str, Any]) -> dict[str, Any]:
    updated = dict(payload)
    data = _payload_data(payload)
    if data is None:
        updated[_TAB_TRANSITION_RESULT_KEY] = transition
    else:
        updated["data"] = {**data, _TAB_TRANSITION_RESULT_KEY: transition}
    return updated


class KimiWebBridgeService:
    def __init__(
        self,
        lifecycle: KimiWebBridgeLifecycle,
        *,
        adapter: KimiWebBridgeAdapter | None = None,
        run_validator: Callable[[str, str], bool] | None = None,
        attachment_store_instance: AttachmentStore | None = None,
        tab_transition_retry_delays: tuple[float, ...] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.lifecycle = lifecycle
        self.adapter = adapter or lifecycle.adapter
        self.artifacts = WebBridgeArtifactStore(lifecycle.paths)
        self.bindings = WebBridgeRunBindingStore(lifecycle.paths)
        self.run_validator = run_validator or self._default_run_validator
        self.attachment_store = attachment_store_instance or attachment_store
        self._tab_transition_retry_delays = tuple(
            _TAB_TRANSITION_RETRY_DELAYS_SECONDS
            if tab_transition_retry_delays is None
            else tab_transition_retry_delays
        )
        self._sleep = sleep or time.sleep

    @staticmethod
    def _default_run_validator(session_id: str, run_id: str) -> bool:
        try:
            from graph.session_manager import session_manager

            if not session_manager.session_exists(session_id):
                return False
            run = session_manager.get_run_state(session_id, run_id)
        except Exception:
            return False
        if not isinstance(run, dict) or str(run.get("session_id") or "") != session_id:
            return False
        return str(run.get("status") or "") in {"running", "waiting_hitl", "evaluating"}

    def _close_tab_preflight_error(
        self,
        *,
        binding: dict[str, Any],
        bound_session: str,
    ) -> str | None:
        """Fail closed unless the daemon and Backend identify one safe target.

        Kimi WebBridge 1.11.5 matches ``find_tab`` by host and does not accept a
        tab id for ``close_tab``. A successful find response therefore cannot
        prove which same-origin tab the following close will target. Inspect the
        live session immediately before closing and refuse every ambiguous case.
        """

        current_tab_id = _tab_id(binding.get("current_tab_id"))
        current_url = str(binding.get("current_url") or "")
        current_host = _url_host(current_url)
        if not current_tab_id or not current_host:
            return "browser_close_preflight_unavailable"
        try:
            observed = self.adapter.command(
                action="list_tabs",
                args={},
                session_id=bound_session,
            )
        except WebBridgeError:
            return "browser_close_preflight_unavailable"
        if _explicit_command_success(observed) is not True:
            return "browser_close_preflight_unavailable"
        tabs = _payload_tabs(observed)
        if tabs is None:
            return "browser_close_preflight_unavailable"

        same_host_tabs = [item for item in tabs if _url_host(item.get("url")) == current_host]
        if len(same_host_tabs) > 1:
            return "browser_close_ambiguous_same_origin_tabs"

        active_tabs = [item for item in tabs if item.get("active") is True]
        if len(active_tabs) != 1:
            return "browser_close_current_tab_mismatch"
        active = active_tabs[0]
        active_tab_id = _tab_id(active.get("tabId") or active.get("tab_id"))
        if (
            active_tab_id != current_tab_id
            or not _urls_match_exactly(current_url, active.get("url"))
        ):
            return "browser_close_current_tab_mismatch"
        return None

    def _reconcile_link_click_tab(
        self,
        *,
        binding: dict[str, Any],
        operation_id: str,
        source_tab_id: str,
        source_url: str,
        bound_session: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Adopt a link-opened tab only when browser/session evidence agrees.

        Kimi WebBridge 1.11.5 does not add tabs created by target=_blank or
        window.open to its session tab ids. The visible Chrome tab still
        inherits the source tab group. Reconcile that orphan immediately under
        the original click's operation and command lease.
        """

        if not self.bindings.can_reconcile_link_click(
            binding,
            operation_id=operation_id,
            source_tab_id=source_tab_id,
        ):
            return None, None
        source_host = _url_host(source_url)
        if not source_host:
            return None, None

        observed = self.adapter.command(action="list_tabs", args={}, session_id=bound_session)
        if _explicit_command_success(observed) is not True:
            raise WebBridgeError(
                "browser_action_outcome_unknown",
                "WebBridge could not reconcile the link click tab transition",
            )
        tabs = _payload_tabs(observed)
        if tabs is None:
            raise WebBridgeError(
                "browser_action_outcome_unknown",
                "WebBridge returned an invalid tab list during click reconciliation",
            )
        source = next((item for item in tabs if _tab_id(item.get("tabId") or item.get("tab_id")) == source_tab_id), None)
        if source is None:
            return {"status": "unresolved", "reason": "source_tab_missing"}, None
        source_group = str(source.get("groupTitle") or "")
        active_tabs = [item for item in tabs if item.get("active") is True]
        if active_tabs:
            active = active_tabs[0]
            active_tab_id = _tab_id(active.get("tabId") or active.get("tab_id"))
            if active_tab_id == source_tab_id:
                return None, None
            same_group = bool(source_group and str(active.get("groupTitle") or "") == source_group)
            same_host = bool(source_host and _url_host(active.get("url")) == source_host)
            if not (same_group if source_group else same_host):
                return {"status": "unresolved", "reason": "active_tab_not_click_related"}, None
            return (
                {
                    "status": "adopted",
                    "mode": "already_tracked",
                    "tabId": active_tab_id,
                    "url": str(active.get("url") or ""),
                },
                observed,
            )

        adopted: dict[str, Any] | None = None
        for attempt in range(len(self._tab_transition_retry_delays) + 1):
            if attempt:
                self._sleep(self._tab_transition_retry_delays[attempt - 1])
            candidate_payload = self.adapter.command(
                action="find_tab",
                args={"url": source_url, "active": True},
                session_id=bound_session,
            )
            adopted_success = _explicit_command_success(candidate_payload)
            if adopted_success is False:
                continue
            if adopted_success is not True:
                raise WebBridgeError(
                    "browser_action_outcome_unknown",
                    "WebBridge active tab adoption outcome is unknown",
                )
            adopted = candidate_payload
            break
        if adopted is None:
            # The source tab is known to be inactive. Return that observation so
            # the ledger stops page-level commands from silently falling back to
            # the old tab while a spawned tab remains outside the session.
            return {"status": "unresolved", "reason": "active_same_host_tab_not_found"}, observed
        adopted_data = _payload_data(adopted)
        adopted_tab_id = _tab_id((adopted_data or {}).get("tabId") or (adopted_data or {}).get("tab_id"))
        adopted_url = str((adopted_data or {}).get("url") or "")
        if (
            not adopted_tab_id
            or adopted_tab_id == source_tab_id
            or _url_host(adopted_url) != source_host
        ):
            return {"status": "unresolved", "reason": "adopted_tab_identity_mismatch"}, None

        reconciled = self.adapter.command(action="list_tabs", args={}, session_id=bound_session)
        if _explicit_command_success(reconciled) is not True:
            raise WebBridgeError(
                "browser_action_outcome_unknown",
                "WebBridge could not verify the adopted tab",
            )
        reconciled_tabs = _payload_tabs(reconciled)
        if reconciled_tabs is None:
            raise WebBridgeError(
                "browser_action_outcome_unknown",
                "WebBridge returned an invalid adopted tab list",
            )
        candidate = next(
            (
                item
                for item in reconciled_tabs
                if _tab_id(item.get("tabId") or item.get("tab_id")) == adopted_tab_id
                and item.get("active") is True
            ),
            None,
        )
        if candidate is None:
            raise WebBridgeError(
                "browser_action_outcome_unknown",
                "The adopted tab was not visible in the WebBridge session",
            )
        candidate_group = str(candidate.get("groupTitle") or "")
        if source_group and candidate_group != source_group:
            raise WebBridgeError(
                "browser_action_outcome_unknown",
                "The adopted tab did not inherit the source tab group",
            )
        if _url_host(candidate.get("url")) != source_host:
            raise WebBridgeError(
                "browser_action_outcome_unknown",
                "The adopted tab host did not match the clicked page",
            )
        return (
            {
                "status": "adopted",
                "mode": "active_same_group",
                "tabId": adopted_tab_id,
                "url": str(candidate.get("url") or adopted_url),
            },
            reconciled,
        )

    def execute(self, command: BrowserCommand, *, session_id: str, run_id: str = "") -> BrowserResult:
        if not run_id:
            return BrowserResult(status="error", action=command.action, error="missing_run_binding")
        if not self.run_validator(session_id, run_id):
            return BrowserResult(status="error", action=command.action, error="invalid_run_binding")
        binding: dict[str, Any] | None = None
        try:
            args = sanitize_action_args(command.action, command.args)
            browser_policy = classify_browser_command(command.action, args)
            if browser_policy.decision == "deny":
                return BrowserResult(status="error", action=command.action, error=browser_policy.reason)
            if browser_policy.decision == "ask":
                authorization = current_authorized_browser_action()
                if (
                    authorization is None
                    or authorization.session_id != session_id
                    or authorization.run_id != run_id
                    or authorization.action != command.action
                    or authorization.args_digest != browser_action_digest(command.action, args)
                ):
                    return BrowserResult(
                        status="error",
                        action=command.action,
                        error="browser_harness_authorization_required",
                    )
            state = self.lifecycle.ensure_ready()
            if not state.enabled:
                return BrowserResult(status="error", action=command.action, error="webbridge_disabled")
            if not state.installed:
                return BrowserResult(status="error", action=command.action, error="webbridge_not_installed")
            if not state.daemon_running:
                # ensure_ready already performed the one bounded automatic
                # recovery attempt. Marking this retryable would make the model
                # loop while the lifecycle circuit breaker is cooling down.
                return BrowserResult(status="error", action=command.action, error="webbridge_daemon_unavailable")
            if not state.extension_connected:
                return BrowserResult(status="needs_input", action=command.action, error="webbridge_extension_not_connected")
            if not state.version_compatible:
                return BrowserResult(status="error", action=command.action, error="webbridge_version_mismatch")
            binding = self.bindings.get_or_create(session_id=session_id, run_id=run_id)
            bound_session = str(binding["webbridge_session"])
            expected_artifact: Path | None = None
            if command.action == "screenshot":
                extension = str(args.get("format") or "png")
                expected_artifact = self.artifacts.allocate(
                    session_id=session_id, run_id=run_id, kind="screenshot", extension=extension,
                )
                args = {**args, "path": str(expected_artifact)}
            elif command.action == "save_as_pdf":
                expected_artifact = self.artifacts.allocate(
                    session_id=session_id, run_id=run_id, kind="pdf", extension="pdf",
                )
                args = {**args, "path": str(expected_artifact)}
            with self.bindings.command_lease(bound_session):
                # Refresh after acquiring the lease so the tab/fence checks are
                # based on the latest command in this product session.
                binding = self.bindings.get_or_create(session_id=session_id, run_id=run_id)
                if (
                    command.action not in _TAB_TRANSITION_RECOVERY_ACTIONS
                    and self.bindings.has_fresh_unresolved_tab_transition(binding)
                ):
                    return BrowserResult(
                        status="error",
                        action=command.action,
                        error="browser_tab_transition_unresolved",
                        retryable=False,
                    )
                if command.action == "close_tab" and not self.bindings.can_close_current_tab(binding):
                    return BrowserResult(status="error", action=command.action, error="tab_not_owned_by_run")
                if command.action == "close_tab":
                    close_preflight_error = self._close_tab_preflight_error(
                        binding=binding,
                        bound_session=bound_session,
                    )
                    if close_preflight_error is not None:
                        return BrowserResult(
                            status="error",
                            action=command.action,
                            error=close_preflight_error,
                            retryable=False,
                        )
                if command.action == "close_session" and not self.bindings.can_close_session(binding):
                    return BrowserResult(status="error", action=command.action, error="session_contains_borrowed_tabs")
                if command.action == "navigate":
                    binding = self.bindings.begin_navigation(binding, str(args["url"]))
                elif command.action in {"click", "fill", "close_tab", "close_session"}:
                    binding = self.bindings.begin_final_action(
                        binding,
                        command.action,
                        browser_action_digest(command.action, args),
                    )
                source_tab_id = str(binding.get("current_tab_id") or "")
                source_url = str(binding.get("current_url") or "")
                payload = self.adapter.command(action=command.action, args=args, session_id=bound_session)
                explicit_success = _explicit_command_success(payload)
                if explicit_success is False:
                    known_textarea_bug = command.action == "fill" and _is_known_textarea_fill_bug(
                        payload,
                        extension_version=state.extension_version,
                    )
                    pending_action = binding.get("pending_final_action")
                    operation_id = (
                        str(pending_action.get("operation_id") or "")
                        if isinstance(pending_action, dict)
                        else ""
                    )
                    can_recover = bool(
                        known_textarea_bug
                        and self.bindings.can_recover_textarea_fill(
                            binding,
                            selector=str(args.get("selector") or ""),
                            operation_id=operation_id,
                        )
                    )
                    if can_recover:
                        payload = self.adapter.replace_focused_textarea_value(
                            value=str(args.get("value") or ""),
                            session_id=bound_session,
                        )
                        explicit_success = True
                    else:
                        binding = self.bindings.record_command_failure(binding, command.action)
                        return BrowserResult(
                            status="error",
                            action=command.action,
                            error="webbridge_command_failed",
                            retryable=False,
                        )
                if explicit_success is None:
                    if command.action in _OUTCOME_UNKNOWN_ACTIONS:
                        return BrowserResult(
                            status="error",
                            action=command.action,
                            error="browser_action_outcome_unknown",
                            retryable=False,
                        )
                    return BrowserResult(
                        status="error",
                        action=command.action,
                        error="invalid_daemon_response",
                        retryable=False,
                    )
                if command.action == "find_tab" and isinstance(args.get("url"), str):
                    result_data = _payload_data(payload)
                    returned_url = (result_data or {}).get("url")
                    if not _urls_match_exactly(args["url"], returned_url):
                        return BrowserResult(
                            status="error",
                            action=command.action,
                            error="browser_tab_url_mismatch",
                            retryable=False,
                        )
                tab_transition = None
                transition_tabs_payload = None
                if command.action == "click" and _click_returned_link(payload):
                    pending_action = binding.get("pending_final_action")
                    operation_id = (
                        str(pending_action.get("operation_id") or "")
                        if isinstance(pending_action, dict)
                        else ""
                    )
                    tab_transition, transition_tabs_payload = self._reconcile_link_click_tab(
                        binding=binding,
                        operation_id=operation_id,
                        source_tab_id=source_tab_id,
                        source_url=source_url,
                        bound_session=bound_session,
                    )
                    if tab_transition is not None:
                        payload = _with_tab_transition(payload, tab_transition)
                binding = self.bindings.record_command_result(
                    binding,
                    command.action,
                    payload,
                    action_args=args,
                )
                if (
                    command.action == "click"
                    and isinstance(tab_transition, dict)
                    and tab_transition.get("status") == "unresolved"
                ):
                    binding = self.bindings.record_unresolved_tab_transition(
                        binding,
                        args_digest=browser_action_digest(command.action, args),
                        reason=str(tab_transition.get("reason") or "unknown"),
                    )
                if transition_tabs_payload is not None:
                    binding = self.bindings.record_command_result(
                        binding,
                        "list_tabs",
                        transition_tabs_payload,
                    )
            data = _sanitize_result(payload)
            if expected_artifact is not None:
                artifact_path = self.artifacts.validate_result(
                    expected=expected_artifact,
                    returned=_artifact_result_path(payload),
                )
                if isinstance(data, dict):
                    if self.attachment_store.root_dir is None:
                        raise WebBridgeError(
                            "attachment_store_unavailable",
                            "附件仓库尚未初始化",
                            retryable=True,
                        )
                    extension = "pdf" if command.action == "save_as_pdf" else str(args.get("format") or "png")
                    mime_type = "application/pdf" if extension == "pdf" else f"image/{extension}"
                    nofollow = getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(str(artifact_path), os.O_RDONLY | nofollow)
                    with os.fdopen(descriptor, "rb") as stream:
                        attachment = self.attachment_store.save(
                            session_id=session_id,
                            filename=f"webbridge-{command.action}-{artifact_path.stem}.{extension}",
                            mime_type=mime_type,
                            source="generated",
                            stream=stream,
                            created_by_run_id=run_id,
                        )
                    artifact_path.unlink(missing_ok=True)
                    data = _without_artifact_paths(data)
                    data["artifact"] = attachment
                    if command.action == "screenshot":
                        # The screenshot is published to the user immediately, but the
                        # main Agent is intentionally text-only.  Give it a trusted,
                        # session-scoped route to the image analyzer without pretending
                        # that artifact metadata is visual evidence.
                        data["puddingclaw_visual_route"] = {
                            "status": "not_analyzed",
                            "subagent_type": "image_analyzer",
                            "harness_attachment_session_id": session_id,
                            "attachment_ref": str(attachment.get("id") or ""),
                            "delegate_when": "visual_content_is_required",
                            "display_only_when": "the_user_only_requested_the_screenshot_artifact",
                        }
            return BrowserResult(status="ok", action=command.action, data=data)
        except WebBridgePolicyError as exc:
            return BrowserResult(status="error", action=command.action, error=str(exc))
        except WebBridgeArtifactError as exc:
            return BrowserResult(status="error", action=command.action, error=str(exc))
        except WebBridgeRunBindingError as exc:
            if str(exc) == "browser_action_outcome_unknown":
                return BrowserResult(
                    status="error",
                    action=command.action,
                    error="browser_action_outcome_unknown",
                    retryable=False,
                )
            return BrowserResult(status="error", action=command.action, error=str(exc))
        except WebBridgeError as exc:
            if exc.code == "browser_action_outcome_unknown":
                return BrowserResult(
                    status="error",
                    action=command.action,
                    error="browser_action_outcome_unknown",
                    retryable=False,
                )
            if exc.code in _AMBIGUOUS_TRANSPORT_ERRORS:
                self.lifecycle.note_transport_failure()
                if command.action in _OUTCOME_UNKNOWN_ACTIONS:
                    return BrowserResult(
                        status="error",
                        action=command.action,
                        error="browser_action_outcome_unknown",
                        retryable=False,
                    )
            elif binding is not None and command.action in _OUTCOME_UNKNOWN_ACTIONS:
                self.bindings.record_command_failure(binding, command.action)
            return BrowserResult(status="error", action=command.action, error=exc.code, retryable=exc.retryable)
        except Exception:
            # The browser tool must not leak daemon responses, paths, or
            # exception details into the model context on an unknown failure.
            return BrowserResult(status="error", action=command.action, error="webbridge_internal_error")
