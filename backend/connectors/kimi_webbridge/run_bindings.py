"""Persistent per-Run WebBridge session and tab ownership ledger."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from runtime_identity.paths import PuddingClawPaths

_BINDING_LOCK = threading.RLock()
_COMMAND_LOCK = threading.RLock()
_TEXTAREA_TARGET_EVIDENCE_TTL_SECONDS = 120.0
_UNRESOLVED_TAB_TRANSITION_TTL_SECONDS = 120.0

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


class WebBridgeRunBindingError(ValueError):
    pass


class WebBridgeRunBindingStore:
    def __init__(self, paths: PuddingClawPaths) -> None:
        self.root = paths.root / "connectors" / "kimi-webbridge-runs"
        self._lock = _BINDING_LOCK

    @staticmethod
    def _key(session_id: str, run_id: str) -> str:
        return hashlib.sha256(f"{session_id}\0{run_id}".encode()).hexdigest()[:32]

    @staticmethod
    def _session_key(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]

    def _path(self, session_id: str, run_id: str) -> Path:
        if not session_id or not run_id:
            raise WebBridgeRunBindingError("missing_run_binding")
        return self.root / f"{self._key(session_id, run_id)}.json"

    def _session_path(self, session_id: str) -> Path:
        if not session_id:
            raise WebBridgeRunBindingError("missing_session_binding")
        return self.root / f"session-{self._session_key(session_id)}.json"

    @contextmanager
    def _transaction(self):
        """Serialize binding read-modify-write across threads and workers."""

        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.root / ".bindings.lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @contextmanager
    def command_lease(self, webbridge_session: str):
        """Prevent product commands from changing tab/focus during a wire sequence."""

        if not webbridge_session:
            raise WebBridgeRunBindingError("missing_session_binding")
        lease_key = hashlib.sha256(webbridge_session.encode("utf-8")).hexdigest()[:32]
        with _COMMAND_LOCK:
            self.root.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self.root / f".command-{lease_key}.lock",
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            try:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return payload if isinstance(payload, dict) else None

    def _latest_legacy_binding(self, session_id: str) -> dict[str, Any] | None:
        """Reuse the most recent pre-session-scope binding during migration."""

        candidates: list[dict[str, Any]] = []
        try:
            paths = self.root.glob("*.json")
        except OSError:
            return None
        for path in paths:
            if path.name.startswith("session-"):
                continue
            payload = self._read(path)
            if payload is not None and payload.get("session_id") == session_id:
                candidates.append(payload)
        if not candidates:
            return None
        return max(candidates, key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0))

    def _get_or_create_session_state(self, session_id: str) -> dict[str, Any]:
        path = self._session_path(session_id)
        existing = self._read(path)
        if existing is not None:
            if existing.get("session_id") != session_id:
                raise WebBridgeRunBindingError("session_binding_identity_mismatch")
            return existing
        legacy = self._latest_legacy_binding(session_id)
        now = time.time()
        state = {
            "version": 1,
            "session_id": session_id,
            "webbridge_session": str((legacy or {}).get("webbridge_session") or f"puddingclaw-{secrets.token_urlsafe(18)}"),
            "current_tab_id": (legacy or {}).get("current_tab_id"),
            "current_url": (legacy or {}).get("current_url"),
            "pending_navigation": (legacy or {}).get("pending_navigation"),
            "pending_final_action": (legacy or {}).get("pending_final_action"),
            "unresolved_tab_transition": (legacy or {}).get("unresolved_tab_transition"),
            "last_clicked_element": (legacy or {}).get("last_clicked_element"),
            "known_tab_ids": sorted({
                str(item)
                for item in [
                    *((legacy or {}).get("owned_tab_ids") or []),
                    *((legacy or {}).get("borrowed_tab_ids") or []),
                    (legacy or {}).get("current_tab_id"),
                ]
                if item is not None
            }),
            "created_at": float((legacy or {}).get("created_at") or now),
            "updated_at": now,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            concurrent = self._read(path)
            if concurrent is None:
                raise WebBridgeRunBindingError("session_binding_create_race")
            state = concurrent
        return state

    def get_or_create(self, *, session_id: str, run_id: str) -> dict[str, Any]:
        with self._transaction():
            path = self._path(session_id, run_id)
            session_state = self._get_or_create_session_state(session_id)
            payload = self._read(path)
            if payload is None:
                inherited_tab = session_state.get("current_tab_id")
                inherited_known_tabs = {
                    str(item)
                    for item in session_state.get("known_tab_ids") or []
                    if item is not None
                }
                payload = {
                    "version": 1,
                    "session_id": session_id,
                    "run_id": run_id,
                    "webbridge_session": session_state["webbridge_session"],
                    "owned_tab_ids": [],
                    "borrowed_tab_ids": sorted(inherited_known_tabs),
                    "current_tab_id": inherited_tab,
                    "current_url": session_state.get("current_url"),
                    "pending_navigation": session_state.get("pending_navigation"),
                    "pending_final_action": session_state.get("pending_final_action"),
                    "unresolved_tab_transition": session_state.get("unresolved_tab_transition"),
                    "last_clicked_element": session_state.get("last_clicked_element"),
                    "created_at": time.time(),
                }
                self.root.mkdir(parents=True, exist_ok=True)
                try:
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                        json.dump(payload, stream, ensure_ascii=False)
                        stream.flush()
                        os.fsync(stream.fileno())
                except FileExistsError:
                    payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("session_id") != session_id or payload.get("run_id") != run_id:
                raise WebBridgeRunBindingError("run_binding_identity_mismatch")
            shared_fields = (
                "webbridge_session",
                "current_tab_id",
                "current_url",
                "pending_navigation",
                "pending_final_action",
                "unresolved_tab_transition",
                "last_clicked_element",
            )
            shared_changed = any(payload.get(key) != session_state.get(key) for key in shared_fields)
            for key in shared_fields:
                payload[key] = session_state.get(key)
            owned = {str(item) for item in payload.setdefault("owned_tab_ids", [])}
            borrowed = {str(item) for item in payload.setdefault("borrowed_tab_ids", [])}
            borrowed.update(
                str(item)
                for item in session_state.get("known_tab_ids") or []
                if item is not None and str(item) not in owned
            )
            refreshed_borrowed = sorted(borrowed)
            if shared_changed or refreshed_borrowed != payload.get("borrowed_tab_ids"):
                payload["borrowed_tab_ids"] = refreshed_borrowed
                self._write(payload)
            return dict(payload)

    def begin_navigation(self, binding: dict[str, Any], url: str) -> dict[str, Any]:
        """Persist an in-flight navigation before dispatching it to the daemon.

        A transport timeout does not prove that navigation failed: the browser
        may already have created a tab. Any unresolved navigation in the same
        browser session is therefore rejected instead of replayed or interleaved.
        """

        with self._transaction():
            state = self._get_or_create_session_state(str(binding.get("session_id") or ""))
            pending = state.get("pending_navigation")
            pending_final_action = state.get("pending_final_action")
            now = time.time()
            if isinstance(pending, dict) or isinstance(pending_final_action, dict):
                raise WebBridgeRunBindingError("browser_action_outcome_unknown")
            updated = dict(binding)
            updated["pending_navigation"] = {
                "url": url,
                "run_id": binding.get("run_id"),
                "started_at": now,
                "operation_id": secrets.token_urlsafe(12),
            }
            self._write(updated)
            self._write_session_state(updated)
            return updated

    def begin_final_action(self, binding: dict[str, Any], action: str, args_digest: str) -> dict[str, Any]:
        """Fence click/fill/close actions whose transport outcome may be unknown."""

        with self._transaction():
            state = self._get_or_create_session_state(str(binding.get("session_id") or ""))
            pending = state.get("pending_final_action")
            pending_navigation = state.get("pending_navigation")
            if isinstance(pending, dict) or isinstance(pending_navigation, dict):
                raise WebBridgeRunBindingError("browser_action_outcome_unknown")
            unresolved_transition = state.get("unresolved_tab_transition")
            if (
                action == "click"
                and isinstance(unresolved_transition, dict)
                and unresolved_transition.get("args_digest") == args_digest
                and 0
                <= time.time() - float(unresolved_transition.get("started_at") or 0)
                <= _UNRESOLVED_TAB_TRANSITION_TTL_SECONDS
            ):
                raise WebBridgeRunBindingError("browser_tab_transition_unresolved")
            updated = dict(binding)
            updated["pending_final_action"] = {
                "action": action,
                "args_digest": args_digest,
                "run_id": binding.get("run_id"),
                "started_at": time.time(),
                "operation_id": secrets.token_urlsafe(12),
                "tab_id": binding.get("current_tab_id"),
            }
            self._write(updated)
            self._write_session_state(updated)
            return updated

    def record_unresolved_tab_transition(
        self,
        binding: dict[str, Any],
        *,
        args_digest: str,
        reason: str,
    ) -> dict[str, Any]:
        """Fence an already-successful link click whose new tab was not adopted."""

        with self._transaction():
            updated = dict(binding)
            updated["unresolved_tab_transition"] = {
                "action": "click",
                "args_digest": args_digest,
                "run_id": binding.get("run_id"),
                "source_tab_id": binding.get("current_tab_id"),
                "source_url": binding.get("current_url"),
                "reason": reason,
                "started_at": time.time(),
            }
            self._write(updated)
            self._write_session_state(updated)
            return updated

    def record_command_failure(self, binding: dict[str, Any], action: str) -> dict[str, Any]:
        """Clear a fence only when the daemon explicitly reports failure."""

        with self._transaction():
            updated = dict(binding)
            if action == "navigate":
                updated["pending_navigation"] = None
            if action in {"click", "fill", "close_tab", "close_session"}:
                updated["pending_final_action"] = None
            self._write(updated)
            self._write_session_state(updated)
            return updated

    def record_command_result(
        self,
        binding: dict[str, Any],
        action: str,
        payload: dict[str, Any],
        *,
        action_args: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._transaction():
            return self._record_command_result(binding, action, payload, action_args=action_args)

    def _record_command_result(
        self,
        binding: dict[str, Any],
        action: str,
        payload: dict[str, Any],
        *,
        action_args: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        updated = dict(binding)
        previous_tab_id = updated.get("current_tab_id")
        removed_tab_ids: set[str] = set()
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        tab_id = None
        current_url = None
        if isinstance(data, dict):
            tab_id = data.get("tabId") or data.get("tab_id")
            current_url = data.get("url") if isinstance(data.get("url"), str) else None
            if action == "list_tabs" and isinstance(data.get("tabs"), list):
                current = next((item for item in data["tabs"] if isinstance(item, dict) and item.get("active")), None)
                tab_id = (current or {}).get("tabId") or (current or {}).get("tab_id")
                current_url = (current or {}).get("url") if isinstance((current or {}).get("url"), str) else None
                if current is None:
                    updated["current_tab_id"] = None
                owned = {str(item) for item in updated.get("owned_tab_ids") or []}
                borrowed = {str(item) for item in updated.setdefault("borrowed_tab_ids", [])}
                for item in data["tabs"]:
                    if not isinstance(item, dict):
                        continue
                    seen_id = item.get("tabId") or item.get("tab_id")
                    if seen_id is not None and str(seen_id) not in owned:
                        borrowed.add(str(seen_id))
                updated["borrowed_tab_ids"] = sorted(borrowed)
        if tab_id is not None:
            tab_id = str(tab_id)
            updated["current_tab_id"] = tab_id
            if action == "navigate" and tab_id not in updated.setdefault("owned_tab_ids", []):
                updated["owned_tab_ids"].append(tab_id)
            elif action in {"find_tab", "snapshot"} and tab_id not in updated.get("owned_tab_ids", []):
                if tab_id not in updated.setdefault("borrowed_tab_ids", []):
                    updated["borrowed_tab_ids"].append(tab_id)
        if current_url:
            updated["current_url"] = current_url
        if action == "navigate" or str(previous_tab_id or "") != str(updated.get("current_tab_id") or ""):
            updated["last_clicked_element"] = None
        if action == "click" and isinstance(data, dict):
            selector = (action_args or {}).get("selector")
            tag = data.get("tag")
            if isinstance(selector, str) and selector and isinstance(tag, str) and tag:
                pending_action = updated.get("pending_final_action")
                updated["last_clicked_element"] = {
                    "selector": selector,
                    "tag": tag.upper(),
                    "tab_id": updated.get("current_tab_id"),
                    "operation_id": (
                        pending_action.get("operation_id")
                        if isinstance(pending_action, dict)
                        else None
                    ),
                    "recorded_at": time.time(),
                }
        pending = updated.get("pending_navigation")
        if action == "navigate" or (
            isinstance(pending, dict)
            and current_url
            and pending.get("url") == current_url
        ):
            updated["pending_navigation"] = None
        unresolved_transition = updated.get("unresolved_tab_transition")
        if action == "navigate" and tab_id is not None:
            updated["unresolved_tab_transition"] = None
        elif action == "find_tab" and tab_id is not None:
            source_tab_id = (
                str(unresolved_transition.get("source_tab_id") or "")
                if isinstance(unresolved_transition, dict)
                else ""
            )
            # Selecting the old source tab is not recovery: the click-created
            # tab is still outside the session. Only a different tab can clear
            # the transition fence.
            if not source_tab_id or source_tab_id != str(tab_id):
                updated["unresolved_tab_transition"] = None
        unresolved_transition = updated.get("unresolved_tab_transition")
        if (
            action == "list_tabs"
            and tab_id is not None
            and isinstance(unresolved_transition, dict)
            and str(unresolved_transition.get("source_tab_id") or "") != str(tab_id)
        ):
            updated["unresolved_tab_transition"] = None
        if action in {"click", "fill", "close_tab", "close_session"}:
            updated["pending_final_action"] = None
        if action == "close_tab":
            current = str(updated.get("current_tab_id") or "")
            if current:
                removed_tab_ids.add(current)
            updated["owned_tab_ids"] = [item for item in updated.get("owned_tab_ids", []) if str(item) != current]
            updated["borrowed_tab_ids"] = [item for item in updated.get("borrowed_tab_ids", []) if str(item) != current]
            updated["current_tab_id"] = None
            updated["current_url"] = None
            updated["pending_navigation"] = None
            updated["pending_final_action"] = None
            updated["unresolved_tab_transition"] = None
            updated["last_clicked_element"] = None
        elif action == "close_session":
            removed_tab_ids.update(str(item) for item in updated.get("owned_tab_ids") or [])
            updated["owned_tab_ids"] = []
            updated["borrowed_tab_ids"] = []
            updated["current_tab_id"] = None
            updated["current_url"] = None
            updated["pending_navigation"] = None
            updated["pending_final_action"] = None
            updated["unresolved_tab_transition"] = None
            updated["last_clicked_element"] = None
        self._write(updated)
        self._write_session_state(updated, removed_tab_ids=removed_tab_ids, clear_all=action == "close_session")
        return updated

    def can_recover_textarea_fill(
        self,
        binding: dict[str, Any],
        *,
        selector: str,
        operation_id: str,
    ) -> bool:
        """Confirm fresh TEXTAREA evidence and the still-current fill fence."""

        with self._transaction():
            latest = self._read(
                self._path(str(binding.get("session_id") or ""), str(binding.get("run_id") or ""))
            )
            if latest is None:
                return False
            state = self._get_or_create_session_state(str(binding.get("session_id") or ""))
            pending = latest.get("pending_final_action")
            state_pending = state.get("pending_final_action")
            evidence = latest.get("last_clicked_element")
            if not all(isinstance(item, dict) for item in (pending, state_pending, evidence)):
                return False
            tab_id = str(latest.get("current_tab_id") or "")
            recorded_at = float(evidence.get("recorded_at") or 0)
            return bool(
                operation_id
                and pending.get("action") == "fill"
                and pending.get("operation_id") == operation_id
                and state_pending.get("operation_id") == operation_id
                and tab_id
                and tab_id == str(state.get("current_tab_id") or "")
                and tab_id == str(pending.get("tab_id") or "")
                and tab_id == str(evidence.get("tab_id") or "")
                and evidence.get("selector") == selector
                and evidence.get("tag") == "TEXTAREA"
                and 0 <= time.time() - recorded_at <= _TEXTAREA_TARGET_EVIDENCE_TTL_SECONDS
            )

    def can_reconcile_link_click(
        self,
        binding: dict[str, Any],
        *,
        operation_id: str,
        source_tab_id: str,
    ) -> bool:
        """Verify that an in-flight link click still owns the source tab."""

        with self._transaction():
            latest = self._read(
                self._path(str(binding.get("session_id") or ""), str(binding.get("run_id") or ""))
            )
            if latest is None:
                return False
            state = self._get_or_create_session_state(str(binding.get("session_id") or ""))
            pending = latest.get("pending_final_action")
            state_pending = state.get("pending_final_action")
            if not isinstance(pending, dict) or not isinstance(state_pending, dict):
                return False
            return bool(
                operation_id
                and source_tab_id
                and pending.get("action") == "click"
                and pending.get("operation_id") == operation_id
                and state_pending.get("operation_id") == operation_id
                and str(pending.get("tab_id") or "") == source_tab_id
                and str(latest.get("current_tab_id") or "") == source_tab_id
                and str(state.get("current_tab_id") or "") == source_tab_id
            )

    def has_fresh_unresolved_tab_transition(self, binding: dict[str, Any]) -> bool:
        """Return true while a successful link click still lacks a safe current tab."""

        with self._transaction():
            state = self._get_or_create_session_state(str(binding.get("session_id") or ""))
            transition = state.get("unresolved_tab_transition")
            if not isinstance(transition, dict):
                return False
            age = time.time() - float(transition.get("started_at") or 0)
            return 0 <= age <= _UNRESOLVED_TAB_TRANSITION_TTL_SECONDS

    def can_close_current_tab(self, binding: dict[str, Any]) -> bool:
        with self._transaction():
            state = self._get_or_create_session_state(str(binding.get("session_id") or ""))
            current = str(state.get("current_tab_id") or "")
            binding_current = str(binding.get("current_tab_id") or "")
            owned = {str(item) for item in binding.get("owned_tab_ids") or []}
            return bool(current and current == binding_current and current in owned)

    def can_close_session(self, binding: dict[str, Any]) -> bool:
        with self._transaction():
            state = self._get_or_create_session_state(str(binding.get("session_id") or ""))
            current = str(state.get("current_tab_id") or "")
            owned = {str(item) for item in binding.get("owned_tab_ids") or []}
            known = {str(item) for item in state.get("known_tab_ids") or []}
            return (
                not bool(binding.get("borrowed_tab_ids"))
                and known.issubset(owned)
                and not (current and current not in owned)
            )

    def _write(self, payload: dict[str, Any]) -> None:
        payload = {**payload, "updated_at": time.time()}
        path = self._path(str(payload.get("session_id") or ""), str(payload.get("run_id") or ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def _write_session_state(
        self,
        binding: dict[str, Any],
        *,
        removed_tab_ids: set[str] | None = None,
        clear_all: bool = False,
    ) -> None:
        session_id = str(binding.get("session_id") or "")
        state = self._get_or_create_session_state(session_id)
        known = {
            str(item)
            for item in [
                *(state.get("known_tab_ids") or []),
                *(binding.get("owned_tab_ids") or []),
                *(binding.get("borrowed_tab_ids") or []),
                binding.get("current_tab_id"),
            ]
            if item is not None
        }
        known.difference_update(removed_tab_ids or set())
        if clear_all:
            known.clear()
        payload = {
            **state,
            "webbridge_session": binding.get("webbridge_session") or state.get("webbridge_session"),
            "current_tab_id": binding.get("current_tab_id"),
            "current_url": binding.get("current_url"),
            "pending_navigation": binding.get("pending_navigation"),
            "pending_final_action": binding.get("pending_final_action"),
            "unresolved_tab_transition": binding.get("unresolved_tab_transition"),
            "last_clicked_element": binding.get("last_clicked_element"),
            "known_tab_ids": sorted(known),
            "updated_at": time.time(),
        }
        path = self._session_path(session_id)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
