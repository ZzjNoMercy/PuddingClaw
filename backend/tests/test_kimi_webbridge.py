from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
from langchain.agents.middleware.types import ToolCallRequest

import connectors.kimi_webbridge.lifecycle as webbridge_lifecycle
from connectors.kimi_webbridge.adapter import (
    ACTION_RESPONSE_TIMEOUT_SECONDS,
    BASE_URL,
    CONNECT_TIMEOUT_SECONDS,
    MAX_RESPONSE_BYTES,
    KimiWebBridgeAdapter,
    WebBridgeError,
)
from connectors.kimi_webbridge.lifecycle import KimiWebBridgeLifecycle
from connectors.kimi_webbridge.models import BrowserCommand
from connectors.kimi_webbridge.policy import classify_browser_command, sanitize_action_args
from connectors.kimi_webbridge.run_bindings import WebBridgeRunBindingStore
from connectors.kimi_webbridge.service import KimiWebBridgeService
from graph.attachment_store import AttachmentStore, attachment_store
from graph.citations import encode_tool_result
from graph.permission_policy import RunPermissionContext
from graph.permission_resume import PermissionResumeRegistry
from graph.tool_result_adapter import ToolResultAdapter
from harness.execution_context import (
    AuthorizedBrowserAction,
    bind_authorized_browser_action,
    browser_action_digest,
)
from harness.tool_execution import PolicyDecision, ToolExecutionPipeline
from harness.verification_activations import resolve_browser_generated_attachment
from runtime_identity.paths import PuddingClawPaths


class FakeAdapter:
    def __init__(self, payload=None):
        self.payload = payload or {"running": True, "extension_connected": True, "version": "test"}
        self.commands = []

    def status(self):
        return self.payload

    def command(self, **kwargs):
        self.commands.append(kwargs)
        if kwargs["action"] == "screenshot":
            Path(kwargs["args"]["path"]).write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            ))
        elif kwargs["action"] == "save_as_pdf":
            Path(kwargs["args"]["path"]).write_bytes(b"%PDF-1.7 test")
        return {"ok": True, "action": kwargs["action"], "cookies": "secret-cookie"}


def TEST_RUN_VALIDATOR(_session_id, _run_id):
    return True


def _installed_binary(path: Path) -> Path:
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(path, 0o700)
    return path


def test_webbridge_is_disabled_until_explicitly_enabled(tmp_path):
    adapter = FakeAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )

    state = lifecycle.probe()

    assert state.installed is True
    assert state.daemon_running is True
    assert state.extension_connected is True
    assert state.enabled is False
    assert state.ready is False


def test_enablement_state_is_atomic_and_owner_only(tmp_path):
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=FakeAdapter(),
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )

    lifecycle.set_enabled(True)

    assert lifecycle.is_enabled() is True
    assert lifecycle.state_path.stat().st_mode & 0o077 == 0


def test_upgrade_uses_vendor_version_syntax(tmp_path, monkeypatch):
    daemon = _installed_binary(tmp_path / "bin" / "kimi-webbridge")
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=FakeAdapter(),
        daemon_path=daemon,
    )
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    lifecycle.upgrade_to("1.9.17")

    assert calls[0][-2:] == ["upgrade", "v1.9.17"]


def test_ensure_ready_restarts_stuck_daemon_with_live_pid(tmp_path, monkeypatch):
    daemon = _installed_binary(tmp_path / "bin" / "kimi-webbridge")
    adapter = FakeAdapter({"running": False, "extension_connected": False, "pid": 1234})
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=daemon,
    )
    lifecycle.set_enabled(True)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        adapter.payload = {
            "running": True,
            "extension_connected": True,
            "version": "test",
            "pid": 5678,
        }
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(webbridge_lifecycle, "STUCK_GRACE_SECONDS", 0)
    monkeypatch.setattr(subprocess, "run", fake_run)

    state = lifecycle.ensure_ready()

    assert state.ready is True
    assert calls == [[str(daemon), "restart"]]


def test_ensure_ready_starts_daemon_when_no_pid_exists(tmp_path, monkeypatch):
    daemon = _installed_binary(tmp_path / "bin" / "kimi-webbridge")
    adapter = FakeAdapter({"running": False, "extension_connected": False})
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=daemon,
    )
    lifecycle.set_enabled(True)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        adapter.payload = {"running": True, "extension_connected": True, "version": "test"}
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    state = lifecycle.ensure_ready()

    assert state.ready is True
    assert calls == [[str(daemon), "start"]]


def test_transport_failure_forces_restart_even_when_cli_status_says_running(tmp_path, monkeypatch):
    daemon = _installed_binary(tmp_path / "bin" / "kimi-webbridge")
    adapter = FakeAdapter({"running": True, "extension_connected": True, "version": "test", "pid": 1234})
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=daemon,
    )
    lifecycle.set_enabled(True)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    lifecycle.note_transport_failure()

    state = lifecycle.ensure_ready()

    assert state.ready is True
    assert calls == [[str(daemon), "restart"]]


def test_restart_cli_failure_still_polls_detached_daemon_to_ready(tmp_path, monkeypatch):
    daemon = _installed_binary(tmp_path / "bin" / "kimi-webbridge")
    adapter = FakeAdapter({"running": False, "extension_connected": False, "pid": 1234})
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=daemon,
    )
    lifecycle.set_enabled(True)

    def fake_run(args, **kwargs):
        adapter.payload = {"running": True, "extension_connected": True, "version": "test", "pid": 5678}
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(webbridge_lifecycle, "STUCK_GRACE_SECONDS", 0)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert lifecycle.ensure_ready().ready is True


def test_service_does_not_call_daemon_when_disabled(tmp_path):
    adapter = FakeAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)

    result = service.execute(BrowserCommand(action="list_tabs"), session_id="run-session", run_id="run-1")

    assert result.status == "error"
    assert result.error == "webbridge_disabled"
    assert adapter.commands == []


def test_service_requires_extension_before_command(tmp_path):
    adapter = FakeAdapter({"running": True, "extension_connected": False})
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)

    result = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR).execute(
        BrowserCommand(action="snapshot"), session_id="run-session", run_id="run-1"
    )

    assert result.status == "needs_input"
    assert result.error == "webbridge_extension_not_connected"
    assert adapter.commands == []


def test_service_binds_session_and_never_accepts_agent_output_path(tmp_path):
    adapter = FakeAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)

    result = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR).execute(
        BrowserCommand(action="snapshot"), session_id="run-session", run_id="run-1"
    )
    assert result.status == "ok"
    assert adapter.commands[0]["session_id"].startswith("puddingclaw-")
    assert "cookies" not in result.data

    sanitize_action_args("navigate", {"url": "https://example.com"})
    assert classify_browser_command("click", {"selector": "@e1"}).decision == "ask"
    assert classify_browser_command("navigate", {"url": "http://127.0.0.1:3000"}).decision == "ask"


def test_browser_schema_does_not_include_high_capability_actions():
    from tools.browser_tool import BrowserToolInput

    schema = BrowserToolInput.model_json_schema()
    actions = schema["properties"]["action"]["enum"]
    assert "evaluate" not in actions
    assert "upload" not in actions
    assert "network" not in actions


def test_browser_action_arguments_are_narrow_and_run_binding_is_required():
    try:
        BrowserCommand(action="list_tabs", args={"url": "https://example.com"})
    except ValueError:
        pass
    else:
        raise AssertionError("unknown browser action argument accepted")

    assert BrowserCommand(action="find_tab", args={"active": True}).args == {"active": True}
    assert BrowserCommand(
        action="scroll",
        args={"direction": "down", "amount": 900, "scope": "largest_scrollable"},
    ).args["amount"] == 900

    for args in (
        {"direction": "sideways"},
        {"amount": 50},
        {"scope": "comment"},
    ):
        try:
            BrowserCommand(action="scroll", args=args)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid scroll arguments accepted: {args!r}")


def test_browser_snapshot_ref_alias_is_canonicalized_before_policy_and_authorization():
    command = BrowserCommand(action="fill", args={"ref": "@e1", "value": "codex重置"})

    assert command.args == {"selector": "@e1", "value": "codex重置"}

    context = RunPermissionContext.from_config_snapshot(
        {"permissions": {"approval_mode": "smart", "policy_epoch": 1}}
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"browser"},
        backend_mode="spawn",
        permission_context=context,
    )
    for call_id, action, args in (
        ("fill-ref-1", "fill", {"ref": "@e1", "value": "codex重置"}),
        ("click-ref-1", "click", {"ref": "@e88"}),
    ):
        request = ToolCallRequest(
            tool_call={
                "id": call_id,
                "name": "browser",
                "args": {"action": action, "args": args},
            },
            tool=None,
            state={},
            runtime=SimpleNamespace(context={"session_id": "s", "run_id": "r"}),
        )

        assert pipeline._preflight(request).decision == PolicyDecision.ASK


def test_browser_ref_alias_rejects_conflicting_selector():
    try:
        BrowserCommand(action="click", args={"selector": "@e1", "ref": "@e2"})
    except ValueError as exc:
        assert "conflicts" in str(exc)
    else:
        raise AssertionError("conflicting browser selector aliases accepted")


def test_browser_artifact_arguments_reject_non_string_selector_and_boolean_numbers():
    for args in (
        {"selector": 123},
        {"ref": {"nested": True}},
        {"quality": True},
    ):
        try:
            BrowserCommand(action="screenshot", args=args)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid screenshot arguments accepted: {args!r}")

    try:
        BrowserCommand(action="save_as_pdf", args={"scale": True})
    except ValueError:
        pass
    else:
        raise AssertionError("boolean PDF scale accepted")


def test_browser_tool_and_service_authorize_canonical_ref_alias(tmp_path):
    from tools.browser_tool import BrowserTool

    adapter = FakeAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)
    command = BrowserCommand(action="click", args={"ref": "@e88"})
    authorization = AuthorizedBrowserAction(
        session_id="session",
        run_id="run",
        tool_call_id="click-ref-1",
        action="click",
        args_digest=browser_action_digest("click", command.args),
    )

    with bind_authorized_browser_action(authorization):
        result = json.loads(
            BrowserTool(session_id="session", run_id="run", service=service)._run(
                "click", {"ref": "@e88"}
            )
        )

    assert result["status"] == "ok"
    assert adapter.commands[0]["args"] == {"selector": "@e88"}


def test_find_tab_rejects_daemon_same_origin_guess_for_exact_url(tmp_path):
    class MismatchedFindAdapter(FakeAdapter):
        def command(self, **kwargs):
            self.commands.append(kwargs)
            return {
                "ok": True,
                "data": {
                    "success": True,
                    "tabId": "tab-search",
                    "url": "https://www.xiaohongshu.com/search_result?keyword=test",
                },
            }

    adapter = MismatchedFindAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)
    command = BrowserCommand(
        action="find_tab",
        args={"url": "https://www.xiaohongshu.com/explore/note?id=1"},
    )

    result = service.execute(command, session_id="chat-session", run_id="run-1")

    assert result.status == "error"
    assert result.error == "browser_tab_url_mismatch"


def test_close_tab_is_blocked_before_dispatch_when_same_origin_tabs_are_ambiguous(tmp_path):
    class AmbiguousCloseAdapter(FakeAdapter):
        def command(self, **kwargs):
            self.commands.append(kwargs)
            if kwargs["action"] == "close_tab":
                raise AssertionError("ambiguous close_tab reached the daemon")
            return {
                "ok": True,
                "data": {
                    "success": True,
                    "tabs": [
                        {
                            "tabId": "tab-search",
                            "url": "https://www.xiaohongshu.com/search_result?keyword=test",
                            "active": True,
                        },
                        {
                            "tabId": "tab-note",
                            "url": "https://www.xiaohongshu.com/explore/note?id=1",
                            "active": False,
                        },
                    ],
                },
            }

    adapter = AmbiguousCloseAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)
    binding = service.bindings.get_or_create(session_id="chat-session", run_id="run-1")
    service.bindings.record_command_result(
        binding,
        "navigate",
        {
            "ok": True,
            "data": {
                "success": True,
                "tabId": "tab-search",
                "url": "https://www.xiaohongshu.com/search_result?keyword=test",
            },
        },
    )
    command = BrowserCommand(action="close_tab", args={})
    authorization = AuthorizedBrowserAction(
        session_id="chat-session",
        run_id="run-1",
        tool_call_id="close-ambiguous-tab",
        action="close_tab",
        args_digest=browser_action_digest("close_tab", {}),
    )

    with bind_authorized_browser_action(authorization):
        result = service.execute(command, session_id="chat-session", run_id="run-1")

    assert result.status == "error"
    assert result.error == "browser_close_ambiguous_same_origin_tabs"
    assert [item["action"] for item in adapter.commands] == ["list_tabs"]
    latest = service.bindings.get_or_create(session_id="chat-session", run_id="run-1")
    assert latest["current_tab_id"] == "tab-search"
    assert latest["pending_final_action"] is None


def test_close_tab_is_blocked_when_live_active_tab_disagrees_with_binding(tmp_path):
    class MismatchedActiveTabAdapter(FakeAdapter):
        def command(self, **kwargs):
            self.commands.append(kwargs)
            if kwargs["action"] == "close_tab":
                raise AssertionError("mismatched close_tab reached the daemon")
            return {
                "ok": True,
                "data": {
                    "success": True,
                    "tabs": [
                        {
                            "tabId": "tab-owned",
                            "url": "https://example.com/owned",
                            "active": False,
                        },
                        {
                            "tabId": "tab-other",
                            "url": "https://openai.com/",
                            "active": True,
                        },
                    ],
                },
            }

    adapter = MismatchedActiveTabAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)
    binding = service.bindings.get_or_create(session_id="chat-session", run_id="run-1")
    service.bindings.record_command_result(
        binding,
        "navigate",
        {
            "ok": True,
            "data": {
                "success": True,
                "tabId": "tab-owned",
                "url": "https://example.com/owned",
            },
        },
    )
    command = BrowserCommand(action="close_tab", args={})
    authorization = AuthorizedBrowserAction(
        session_id="chat-session",
        run_id="run-1",
        tool_call_id="close-mismatched-tab",
        action="close_tab",
        args_digest=browser_action_digest("close_tab", {}),
    )

    with bind_authorized_browser_action(authorization):
        result = service.execute(command, session_id="chat-session", run_id="run-1")

    assert result.status == "error"
    assert result.error == "browser_close_current_tab_mismatch"
    assert [item["action"] for item in adapter.commands] == ["list_tabs"]


def test_close_tab_dispatches_only_after_unambiguous_live_preflight(tmp_path):
    class SafeCloseAdapter(FakeAdapter):
        def command(self, **kwargs):
            self.commands.append(kwargs)
            if kwargs["action"] == "list_tabs":
                return {
                    "ok": True,
                    "data": {
                        "success": True,
                        "tabs": [
                            {
                                "tabId": "tab-owned",
                                "url": "https://example.com/owned",
                                "active": True,
                            }
                        ],
                    },
                }
            return {"ok": True, "data": {"success": True, "closed": True}}

    adapter = SafeCloseAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)
    binding = service.bindings.get_or_create(session_id="chat-session", run_id="run-1")
    service.bindings.record_command_result(
        binding,
        "navigate",
        {
            "ok": True,
            "data": {
                "success": True,
                "tabId": "tab-owned",
                "url": "https://example.com/owned",
            },
        },
    )
    command = BrowserCommand(action="close_tab", args={})
    authorization = AuthorizedBrowserAction(
        session_id="chat-session",
        run_id="run-1",
        tool_call_id="close-safe-tab",
        action="close_tab",
        args_digest=browser_action_digest("close_tab", {}),
    )

    with bind_authorized_browser_action(authorization):
        result = service.execute(command, session_id="chat-session", run_id="run-1")

    assert result.status == "ok"
    assert [item["action"] for item in adapter.commands] == ["list_tabs", "close_tab"]
    latest = service.bindings.get_or_create(session_id="chat-session", run_id="run-1")
    assert latest["current_tab_id"] is None


def test_harness_uses_browser_preflight_and_hard_denies_daemon_shell_bypass():
    context = RunPermissionContext.from_config_snapshot(
        {"permissions": {"approval_mode": "smart", "policy_epoch": 1}}
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"browser", "execute"},
        backend_mode="spawn",
        permission_context=context,
    )
    request = ToolCallRequest(
        tool_call={"id": "browser-1", "name": "browser", "args": {"action": "snapshot", "args": {}}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"session_id": "s", "run_id": "r"}),
    )
    assert pipeline._preflight(request).decision == PolicyDecision.ALLOW

    bypass = ToolCallRequest(
        tool_call={"id": "shell-1", "name": "execute", "args": {"command": "curl http://127.0.0.1:10086/command"}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"session_id": "s", "run_id": "r"}),
    )
    assert pipeline._preflight(bypass).decision == PolicyDecision.DENY


def test_adapter_uses_kimi_wire_session_field():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"ok": True, "data": {"success": True, "tabs": []}})

    import json

    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler), trust_env=False)
    try:
        result = KimiWebBridgeAdapter(client=client).command(
            action="list_tabs", args={}, session_id="puddingclaw:test-session",
        )
    finally:
        client.close()
    assert result["data"]["success"] is True
    assert seen["payload"]["session"] == "puddingclaw:test-session"
    assert "session_id" not in seen["payload"]


def test_adapter_uses_action_aware_response_timeouts():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        seen[payload["action"]] = request.extensions["timeout"]
        return httpx.Response(200, json={"ok": True, "data": {"success": True}})

    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler), trust_env=False)
    try:
        adapter = KimiWebBridgeAdapter(client=client)
        for action in ACTION_RESPONSE_TIMEOUT_SECONDS:
            adapter.command(action=action, args={}, session_id="puddingclaw:test-session")
    finally:
        client.close()

    expected_wire_actions = set(ACTION_RESPONSE_TIMEOUT_SECONDS) - {"scroll"} | {"evaluate"}
    assert set(seen) == expected_wire_actions
    for action, expected_read_timeout in ACTION_RESPONSE_TIMEOUT_SECONDS.items():
        wire_action = "evaluate" if action == "scroll" else action
        assert seen[wire_action]["connect"] == CONNECT_TIMEOUT_SECONDS
        assert seen[wire_action]["read"] == expected_read_timeout


def test_adapter_scroll_uses_fixed_evaluate_contract_without_model_code():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"ok": True, "data": {"success": True}})

    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler), trust_env=False)
    try:
        KimiWebBridgeAdapter(client=client).command(
            action="scroll",
            args={"direction": "up", "amount": 700, "scope": "largest_scrollable"},
            session_id="puddingclaw:test-session",
        )
    finally:
        client.close()

    assert seen["action"] == "evaluate"
    assert set(seen["args"]) == {"code"}
    assert "direction = -1" in seen["args"]["code"]
    assert "amount = 700" in seen["args"]["code"]
    assert "document.querySelectorAll('*')" in seen["args"]["code"]


def test_adapter_timeout_override_applies_to_connect_and_response():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.extensions["timeout"])
        return httpx.Response(200, json={"ok": True, "data": {"success": True}})

    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler), trust_env=False)
    try:
        KimiWebBridgeAdapter(timeout_seconds=0.25, client=client).command(
            action="navigate",
            args={"url": "https://example.com"},
            session_id="puddingclaw:test-session",
        )
    finally:
        client.close()

    assert seen["connect"] == 0.25
    assert seen["read"] == 0.25


def test_adapter_textarea_workaround_is_fixed_semantics_not_a_public_action():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"ok": True, "data": {"success": True}})

    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler), trust_env=False)
    try:
        adapter = KimiWebBridgeAdapter(client=client)
        result = adapter.replace_focused_textarea_value(
            value="卡兹克",
            session_id="puddingclaw:test-session",
        )
        try:
            adapter.command(  # type: ignore[arg-type]
                action="send_keys",
                args={"keys": "Mod+A"},
                session_id="puddingclaw:test-session",
            )
        except WebBridgeError as exc:
            assert exc.code == "unsupported_browser_action"
        else:
            raise AssertionError("internal key primitives must not be public browser actions")
    finally:
        client.close()

    assert result["data"]["mode"] == "cdp_insert_text"
    assert [item["action"] for item in seen] == ["send_keys", "key_type"]
    assert seen[0]["args"] == {"keys": "Mod+A"}
    assert seen[1]["args"] == {"text": "卡兹克"}


def test_adapter_textarea_workaround_explicit_failure_is_outcome_unknown():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": False, "error": "send_keys failed"})

    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler), trust_env=False)
    try:
        try:
            KimiWebBridgeAdapter(client=client).replace_focused_textarea_value(
                value="value",
                session_id="puddingclaw:test-session",
            )
        except WebBridgeError as exc:
            assert exc.code == "browser_action_outcome_unknown"
        else:
            raise AssertionError("partially dispatched compatibility sequences must be fenced")
    finally:
        client.close()

    assert calls == 1


def test_adapter_textarea_workaround_protocol_failures_are_outcome_unknown():
    responses = (
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)),
    )

    for response in responses:
        client = httpx.Client(
            base_url=BASE_URL,
            transport=httpx.MockTransport(lambda _request, response=response: response),
            trust_env=False,
        )
        try:
            try:
                KimiWebBridgeAdapter(client=client).replace_focused_textarea_value(
                    value="value",
                    session_id="puddingclaw:test-session",
                )
            except WebBridgeError as exc:
                assert exc.code == "browser_action_outcome_unknown"
            else:
                raise AssertionError("post-dispatch protocol failures must preserve the fill fence")
        finally:
            client.close()


def test_browser_policy_requires_human_confirmation_for_interactions_and_active_tab():
    context = RunPermissionContext.from_config_snapshot(
        {"permissions": {"approval_mode": "smart", "policy_epoch": 1}}
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"browser"},
        backend_mode="spawn",
        permission_context=context,
    )
    for call_id, action, args in (
        ("click-1", "click", {"selector": "@e1"}),
        ("fill-1", "fill", {"selector": "@e2", "value": "private text"}),
        ("find-1", "find_tab", {"url": "https://example.com", "active": True}),
    ):
        request = ToolCallRequest(
            tool_call={"id": call_id, "name": "browser", "args": {"action": action, "args": args}},
            tool=None,
            state={},
            runtime=SimpleNamespace(context={"session_id": "s", "run_id": "r"}),
        )
        assert pipeline._preflight(request).decision == PolicyDecision.ASK


def test_smart_mode_auto_authorizes_click_and_fill_but_not_other_browser_gates():
    context = RunPermissionContext.from_config_snapshot(
        {"permissions": {"approval_mode": "smart", "policy_epoch": 1}}
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"browser"},
        backend_mode="spawn",
        permission_context=context,
    )
    for call_id, action, args in (
        ("click-ref", "click", {"ref": "@e88"}),
        ("fill-ref", "fill", {"ref": "@e1", "value": "codex重置"}),
        ("click-submit", "click", {"selector": "#submit-order"}),
        ("fill-css", "fill", {"selector": "input:nth-of-type(2):not(.search)", "value": "AI"}),
    ):
        request = ToolCallRequest(
            tool_call={"id": call_id, "name": "browser", "args": {"action": action, "args": args}},
            tool=None,
            state={},
            runtime=SimpleNamespace(context={"session_id": "s", "run_id": "r"}),
        )
        result = pipeline._preflight(request)

        assert result.decision == PolicyDecision.ALLOW
        assert result.reason == "smart_browser_interaction"

    for call_id, action, args in (
        ("local-nav", "navigate", {"url": "http://127.0.0.1:3000"}),
        ("close-tab", "close_tab", {}),
        ("close-session", "close_session", {}),
    ):
        request = ToolCallRequest(
            tool_call={"id": call_id, "name": "browser", "args": {"action": action, "args": args}},
            tool=None,
            state={},
            runtime=SimpleNamespace(context={"session_id": "s", "run_id": "r"}),
        )

        assert pipeline._preflight(request).decision == PolicyDecision.ASK


def test_browser_policy_never_trusts_selector_text_to_bypass_hitl():
    assert classify_browser_command("click", {"selector": "#search"}).decision == "ask"
    assert classify_browser_command("fill", {"selector": "input[name=query]", "value": "AI"}).decision == "ask"
    assert classify_browser_command("click", {"selector": "button:nth-of-type(3):not(.search)"}).decision == "ask"
    assert classify_browser_command(
        "fill", {"selector": "input:nth-of-type(2):not(.search)", "value": "AI"}
    ).decision == "ask"
    assert classify_browser_command("click", {"selector": "#submit-order"}).decision == "ask"
    assert classify_browser_command("fill", {"selector": "#password", "value": "secret"}).decision == "ask"
    assert classify_browser_command("click", {"selector": "@e1"}).decision == "ask"


def test_run_binding_treats_observed_user_tabs_as_borrowed(tmp_path):
    store = WebBridgeRunBindingStore(PuddingClawPaths(tmp_path / "puddingclaw"))
    binding = store.get_or_create(session_id="session", run_id="run")

    binding = store.record_command_result(
        binding,
        "list_tabs",
        {"data": {"tabs": [{"tabId": "user-tab", "active": True}]}},
    )

    assert "user-tab" in binding["borrowed_tab_ids"]
    assert store.can_close_session(binding) is False


def test_runs_in_same_chat_session_share_webbridge_session_and_current_tab(tmp_path):
    store = WebBridgeRunBindingStore(PuddingClawPaths(tmp_path / "puddingclaw"))
    first = store.get_or_create(session_id="chat-session", run_id="run-1")
    first = store.record_command_result(
        first,
        "navigate",
        {"data": {"success": True, "tabId": "tab-1", "url": "https://example.com"}},
    )

    second = store.get_or_create(session_id="chat-session", run_id="run-2")
    other_chat = store.get_or_create(session_id="other-session", run_id="run-1")

    assert second["webbridge_session"] == first["webbridge_session"]
    assert second["current_tab_id"] == "tab-1"
    assert second["current_url"] == "https://example.com"
    assert second["borrowed_tab_ids"] == ["tab-1"]
    assert store.can_close_current_tab(second) is False
    assert other_chat["webbridge_session"] != first["webbridge_session"]


def test_command_lease_serializes_commands_in_same_webbridge_session(tmp_path):
    store = WebBridgeRunBindingStore(PuddingClawPaths(tmp_path / "puddingclaw"))
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first_worker():
        with store.command_lease("shared-webbridge-session"):
            first_entered.set()
            assert release_first.wait(1)

    def second_worker():
        assert first_entered.wait(1)
        with store.command_lease("shared-webbridge-session"):
            second_entered.set()

    first = threading.Thread(target=first_worker)
    second = threading.Thread(target=second_worker)
    first.start()
    second.start()
    assert first_entered.wait(1)
    assert second_entered.wait(0.05) is False
    release_first.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert first.is_alive() is False
    assert second.is_alive() is False
    assert second_entered.is_set() is True


def test_unresolved_link_transition_blocks_cross_run_replay_until_find_tab_resolves_it(tmp_path):
    store = WebBridgeRunBindingStore(PuddingClawPaths(tmp_path / "puddingclaw"))
    first = store.get_or_create(session_id="chat-session", run_id="run-1")
    first = store.record_command_result(
        first,
        "navigate",
        {"ok": True, "data": {"success": True, "tabId": "tab-old", "url": "https://example.com/a"}},
    )
    first = store.begin_final_action(first, "click", "same-link")
    first = store.record_command_result(
        first,
        "click",
        {"ok": True, "data": {"success": True, "tag": "A"}},
        action_args={"selector": "@e1"},
    )
    store.record_unresolved_tab_transition(
        first,
        args_digest="same-link",
        reason="active_same_host_tab_not_found",
    )
    second = store.get_or_create(session_id="chat-session", run_id="run-2")

    try:
        store.begin_final_action(second, "click", "same-link")
    except ValueError as exc:
        assert str(exc) == "browser_tab_transition_unresolved"
    else:
        raise AssertionError("a new Run must not repeat an unresolved link click")

    second = store.record_command_result(
        second,
        "find_tab",
        {"ok": True, "data": {"success": True, "tabId": "tab-old", "url": "https://example.com/a"}},
    )
    assert store.has_fresh_unresolved_tab_transition(second) is True
    try:
        store.begin_final_action(second, "click", "same-link")
    except ValueError as exc:
        assert str(exc) == "browser_tab_transition_unresolved"
    else:
        raise AssertionError("reselecting the source tab must not clear the transition")

    second = store.record_command_result(
        second,
        "find_tab",
        {"ok": True, "data": {"success": True, "tabId": "tab-new", "url": "https://example.com/b"}},
    )
    assert store.begin_final_action(second, "click", "same-link")["pending_final_action"]["action"] == "click"


def test_empty_tab_list_clears_stale_tab_but_keeps_recovery_url(tmp_path):
    store = WebBridgeRunBindingStore(PuddingClawPaths(tmp_path / "puddingclaw"))
    first = store.get_or_create(session_id="chat-session", run_id="run-1")
    first = store.record_command_result(
        first,
        "navigate",
        {"data": {"success": True, "tabId": "tab-1", "url": "https://example.com"}},
    )
    store.record_command_result(first, "list_tabs", {"data": {"success": True, "tabs": []}})

    second = store.get_or_create(session_id="chat-session", run_id="run-2")

    assert second["current_tab_id"] is None
    assert second["current_url"] == "https://example.com"
    assert second["webbridge_session"] == first["webbridge_session"]


def test_pending_navigation_blocks_immediate_replay_and_clears_when_tab_is_found(tmp_path):
    store = WebBridgeRunBindingStore(PuddingClawPaths(tmp_path / "puddingclaw"))
    binding = store.get_or_create(session_id="chat-session", run_id="run-1")
    binding = store.begin_navigation(binding, "https://example.com")

    try:
        store.begin_navigation(binding, "https://example.com")
    except ValueError as exc:
        assert str(exc) == "browser_action_outcome_unknown"
    else:
        raise AssertionError("unresolved navigation was replayed")

    binding = store.record_command_result(
        binding,
        "find_tab",
        {"data": {"success": True, "tabId": "tab-1", "url": "https://example.com"}},
    )
    assert binding["pending_navigation"] is None
    assert store.begin_navigation(binding, "https://example.com")["pending_navigation"]["url"] == "https://example.com"


def test_unresolved_navigation_blocks_different_url_in_same_run(tmp_path):
    store = WebBridgeRunBindingStore(PuddingClawPaths(tmp_path / "puddingclaw"))
    binding = store.get_or_create(session_id="chat-session", run_id="run-1")
    binding = store.begin_navigation(binding, "https://example.com/a")

    try:
        store.begin_navigation(binding, "https://example.com/b")
    except ValueError as exc:
        assert str(exc) == "browser_action_outcome_unknown"
    else:
        raise AssertionError("interleaved navigation bypassed the outcome-unknown fence")


def test_unresolved_write_fences_block_cross_action_interleaving(tmp_path):
    store = WebBridgeRunBindingStore(PuddingClawPaths(tmp_path / "puddingclaw"))
    binding = store.get_or_create(session_id="chat-session", run_id="run-1")
    pending_navigation = store.begin_navigation(binding, "https://example.com")

    try:
        store.begin_final_action(pending_navigation, "fill", "digest")
    except ValueError as exc:
        assert str(exc) == "browser_action_outcome_unknown"
    else:
        raise AssertionError("a fill must not interleave with an unresolved navigation")

    store.record_command_failure(pending_navigation, "navigate")
    refreshed = store.get_or_create(session_id="chat-session", run_id="run-1")
    pending_fill = store.begin_final_action(refreshed, "fill", "digest")

    try:
        store.begin_navigation(pending_fill, "https://example.com/other")
    except ValueError as exc:
        assert str(exc) == "browser_action_outcome_unknown"
    else:
        raise AssertionError("a navigation must not interleave with an unresolved fill")


def test_new_run_borrows_all_known_tabs_even_when_current_tab_is_empty(tmp_path):
    store = WebBridgeRunBindingStore(PuddingClawPaths(tmp_path / "puddingclaw"))
    first = store.get_or_create(session_id="chat-session", run_id="run-1")
    first = store.record_command_result(
        first,
        "navigate",
        {"ok": True, "data": {"tabId": "tab-a", "url": "https://example.com/a"}},
    )
    first = store.record_command_result(
        first,
        "navigate",
        {"ok": True, "data": {"tabId": "tab-b", "url": "https://example.com/b"}},
    )
    store.record_command_result(first, "close_tab", {"ok": True, "data": {"closed": True}})

    second = store.get_or_create(session_id="chat-session", run_id="run-2")

    assert second["current_tab_id"] is None
    assert second["borrowed_tab_ids"] == ["tab-a"]
    assert store.can_close_session(second) is False


def test_old_run_cannot_close_tabs_added_by_a_later_run(tmp_path):
    store = WebBridgeRunBindingStore(PuddingClawPaths(tmp_path / "puddingclaw"))
    first = store.get_or_create(session_id="chat-session", run_id="run-1")
    first = store.record_command_result(
        first,
        "navigate",
        {"ok": True, "data": {"tabId": "tab-a", "url": "https://example.com/a"}},
    )
    second = store.get_or_create(session_id="chat-session", run_id="run-2")
    store.record_command_result(
        second,
        "navigate",
        {"ok": True, "data": {"tabId": "tab-b", "url": "https://example.com/b"}},
    )

    assert store.can_close_session(first) is False


def test_navigate_timeout_is_outcome_unknown_and_cannot_be_replayed(tmp_path):
    class TimeoutNavigateAdapter(FakeAdapter):
        def command(self, **kwargs):
            self.commands.append(kwargs)
            raise WebBridgeError("daemon_timeout", "timed out", retryable=True)

    adapter = TimeoutNavigateAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)
    command = BrowserCommand(action="navigate", args={"url": "https://example.com"})

    first = service.execute(command, session_id="chat-session", run_id="run-1")
    second = service.execute(command, session_id="chat-session", run_id="run-1")

    assert first.error == "browser_action_outcome_unknown"
    assert first.retryable is False
    assert second.error == "browser_action_outcome_unknown"
    assert second.retryable is False
    assert len(adapter.commands) == 1


def test_explicit_navigation_failure_clears_fence_for_a_later_attempt(tmp_path):
    adapter = FakeAdapter({"running": True, "extension_connected": True, "version": "test"})

    def failed_command(**kwargs):
        adapter.commands.append(kwargs)
        return {"ok": False, "error": "invalid URL"}

    adapter.command = failed_command
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)
    command = BrowserCommand(action="navigate", args={"url": "https://example.com"})

    first = service.execute(command, session_id="chat-session", run_id="run-1")
    second = service.execute(command, session_id="chat-session", run_id="run-1")

    assert first.error == "webbridge_command_failed"
    assert second.error == "webbridge_command_failed"
    assert len(adapter.commands) == 2


def test_click_timeout_is_durably_fenced_within_same_run(tmp_path):
    class TimeoutClickAdapter(FakeAdapter):
        def command(self, **kwargs):
            self.commands.append(kwargs)
            if len(self.commands) == 1:
                raise WebBridgeError("daemon_timeout", "timed out", retryable=True)
            return {"ok": True, "data": {"success": True}}

    adapter = TimeoutClickAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)
    command = BrowserCommand(action="click", args={"selector": "@e1"})
    authorization = AuthorizedBrowserAction(
        session_id="chat-session",
        run_id="run-1",
        tool_call_id="click-1",
        action="click",
        args_digest=browser_action_digest("click", command.args),
    )

    with bind_authorized_browser_action(authorization):
        first = service.execute(command, session_id="chat-session", run_id="run-1")
    with bind_authorized_browser_action(authorization):
        second = service.execute(command, session_id="chat-session", run_id="run-1")

    assert first.error == "browser_action_outcome_unknown"
    assert second.error == "browser_action_outcome_unknown"
    assert len(adapter.commands) == 1


def _seed_click_source_tab(service, *, url="https://www.xiaohongshu.com/search", tab_id="tab-old"):
    binding = service.bindings.get_or_create(session_id="chat-session", run_id="run-1")
    return service.bindings.record_command_result(
        binding,
        "navigate",
        {"ok": True, "data": {"success": True, "tabId": tab_id, "url": url}},
    )


def test_link_click_adopts_same_group_active_orphan_tab(tmp_path):
    class OrphanTabAdapter(FakeAdapter):
        def command(self, **kwargs):
            self.commands.append(kwargs)
            action = kwargs["action"]
            list_count = sum(item["action"] == "list_tabs" for item in self.commands)
            if action == "click":
                return {"ok": True, "data": {"success": True, "tag": "A", "text": "数字生命卡兹克"}}
            if action == "find_tab":
                return {
                    "ok": True,
                    "data": {
                        "success": True,
                        "tabId": "tab-new",
                        "url": "https://www.xiaohongshu.com/user/profile/abc",
                        "borrowed": True,
                    },
                }
            if action == "list_tabs" and list_count == 1:
                return {
                    "ok": True,
                    "data": {
                        "success": True,
                        "tabs": [
                            {
                                "tabId": "tab-old",
                                "url": "https://www.xiaohongshu.com/search",
                                "active": False,
                                "groupTitle": "小红书-卡兹克",
                            }
                        ],
                    },
                }
            if action == "list_tabs":
                return {
                    "ok": True,
                    "data": {
                        "success": True,
                        "tabs": [
                            {
                                "tabId": "tab-old",
                                "url": "https://www.xiaohongshu.com/search",
                                "active": False,
                                "groupTitle": "小红书-卡兹克",
                            },
                            {
                                "tabId": "tab-new",
                                "url": "https://www.xiaohongshu.com/user/profile/abc",
                                "active": True,
                                "groupTitle": "小红书-卡兹克",
                            },
                        ],
                    },
                }
            raise AssertionError(f"unexpected action: {action}")

    adapter = OrphanTabAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)
    _seed_click_source_tab(service)
    command = BrowserCommand(action="click", args={"selector": "@e33"})
    authorization = AuthorizedBrowserAction(
        session_id="chat-session",
        run_id="run-1",
        tool_call_id="click-profile",
        action="click",
        args_digest=browser_action_digest("click", command.args),
    )

    with bind_authorized_browser_action(authorization):
        result = service.execute(command, session_id="chat-session", run_id="run-1")

    assert result.status == "ok"
    assert result.data["data"]["puddingclaw_tab_transition"] == {
        "status": "adopted",
        "mode": "active_same_group",
        "tabId": "tab-new",
        "url": "https://www.xiaohongshu.com/user/profile/abc",
    }
    assert [item["action"] for item in adapter.commands] == [
        "click",
        "list_tabs",
        "find_tab",
        "list_tabs",
    ]
    assert adapter.commands[2]["args"] == {
        "url": "https://www.xiaohongshu.com/search",
        "active": True,
    }
    latest = service.bindings.get_or_create(session_id="chat-session", run_id="run-1")
    assert latest["current_tab_id"] == "tab-new"
    assert "tab-new" in latest["borrowed_tab_ids"]
    assert "tab-old" in latest["owned_tab_ids"]


def test_link_click_waits_for_a_slow_spawned_tab_before_adopting_it(tmp_path):
    class SlowOrphanTabAdapter(FakeAdapter):
        def command(self, **kwargs):
            self.commands.append(kwargs)
            action = kwargs["action"]
            find_count = sum(item["action"] == "find_tab" for item in self.commands)
            list_count = sum(item["action"] == "list_tabs" for item in self.commands)
            if action == "click":
                return {"ok": True, "data": {"success": True, "tag": "A", "text": "profile"}}
            if action == "find_tab" and find_count < 3:
                return {"ok": False, "error": "no open tab found"}
            if action == "find_tab":
                return {
                    "ok": True,
                    "data": {
                        "success": True,
                        "tabId": "tab-new",
                        "url": "https://www.xiaohongshu.com/user/profile/abc",
                    },
                }
            tabs = [
                {
                    "tabId": "tab-old",
                    "url": "https://www.xiaohongshu.com/search",
                    "active": False,
                    "groupTitle": "小红书-卡兹克",
                }
            ]
            if list_count > 1:
                tabs.append(
                    {
                        "tabId": "tab-new",
                        "url": "https://www.xiaohongshu.com/user/profile/abc",
                        "active": True,
                        "groupTitle": "小红书-卡兹克",
                    }
                )
            return {"ok": True, "data": {"success": True, "tabs": tabs}}

    adapter = SlowOrphanTabAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(
        lifecycle,
        adapter=adapter,
        run_validator=TEST_RUN_VALIDATOR,
        tab_transition_retry_delays=(0.0, 0.0),
        sleep=lambda _seconds: None,
    )
    _seed_click_source_tab(service)
    command = BrowserCommand(action="click", args={"selector": "@e51"})
    authorization = AuthorizedBrowserAction(
        session_id="chat-session",
        run_id="run-1",
        tool_call_id="click-slow-profile",
        action="click",
        args_digest=browser_action_digest("click", command.args),
    )

    with bind_authorized_browser_action(authorization):
        result = service.execute(command, session_id="chat-session", run_id="run-1")

    assert result.data["data"]["puddingclaw_tab_transition"]["status"] == "adopted"
    assert [item["action"] for item in adapter.commands] == [
        "click",
        "list_tabs",
        "find_tab",
        "find_tab",
        "find_tab",
        "list_tabs",
    ]
    latest = service.bindings.get_or_create(session_id="chat-session", run_id="run-1")
    assert latest["current_tab_id"] == "tab-new"


def test_link_click_preserves_the_run_owned_source_tab_after_adoption(tmp_path):
    class NewTabAdapter(FakeAdapter):
        def command(self, **kwargs):
            self.commands.append(kwargs)
            action = kwargs["action"]
            list_count = sum(item["action"] == "list_tabs" for item in self.commands)
            if action == "click":
                return {"ok": True, "data": {"success": True, "tag": "A", "text": "profile"}}
            if action == "find_tab":
                return {
                    "ok": True,
                    "data": {
                        "success": True,
                        "tabId": "tab-new",
                        "url": "https://www.xiaohongshu.com/user/profile/abc",
                    },
                }
            if action == "list_tabs" and list_count == 1:
                tabs = [
                    {
                        "tabId": "tab-old",
                        "url": "https://www.xiaohongshu.com/search",
                        "active": False,
                        "groupTitle": "小红书-卡兹克",
                    }
                ]
            elif action == "list_tabs" and list_count == 2:
                tabs = [
                    {
                        "tabId": "tab-old",
                        "url": "https://www.xiaohongshu.com/search",
                        "active": False,
                        "groupTitle": "小红书-卡兹克",
                    },
                    {
                        "tabId": "tab-new",
                        "url": "https://www.xiaohongshu.com/user/profile/abc",
                        "active": True,
                        "groupTitle": "小红书-卡兹克",
                    },
                ]
            else:
                tabs = [
                    {
                        "tabId": "tab-new",
                        "url": "https://www.xiaohongshu.com/user/profile/abc",
                        "active": True,
                        "groupTitle": "小红书-卡兹克",
                    }
                ]
            return {"ok": True, "data": {"success": True, "tabs": tabs}}

    adapter = NewTabAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(
        lifecycle,
        adapter=adapter,
        run_validator=TEST_RUN_VALIDATOR,
    )
    _seed_click_source_tab(service)
    command = BrowserCommand(action="click", args={"selector": "@e51"})
    authorization = AuthorizedBrowserAction(
        session_id="chat-session",
        run_id="run-1",
        tool_call_id="click-open-profile",
        action="click",
        args_digest=browser_action_digest("click", command.args),
    )

    with bind_authorized_browser_action(authorization):
        result = service.execute(command, session_id="chat-session", run_id="run-1")

    assert result.data["data"]["puddingclaw_tab_transition"] == {
        "status": "adopted",
        "mode": "active_same_group",
        "tabId": "tab-new",
        "url": "https://www.xiaohongshu.com/user/profile/abc",
    }
    assert [item["action"] for item in adapter.commands] == [
        "click",
        "list_tabs",
        "find_tab",
        "list_tabs",
    ]
    latest = service.bindings.get_or_create(session_id="chat-session", run_id="run-1")
    assert latest["current_tab_id"] == "tab-new"
    assert latest["owned_tab_ids"] == ["tab-old"]
    assert latest["borrowed_tab_ids"] == ["tab-new"]


def test_link_click_preserves_a_borrowed_source_tab(tmp_path):
    class BorrowedSourceAdapter(FakeAdapter):
        def command(self, **kwargs):
            self.commands.append(kwargs)
            action = kwargs["action"]
            list_count = sum(item["action"] == "list_tabs" for item in self.commands)
            if action == "click":
                return {"ok": True, "data": {"success": True, "tag": "A", "text": "profile"}}
            if action == "find_tab":
                return {
                    "ok": True,
                    "data": {
                        "success": True,
                        "tabId": "tab-new",
                        "url": "https://www.xiaohongshu.com/user/profile/abc",
                    },
                }
            tabs = [
                {
                    "tabId": "tab-user",
                    "url": "https://www.xiaohongshu.com/search",
                    "active": list_count == 0,
                    "groupTitle": "用户页面",
                }
            ]
            if list_count > 1:
                tabs[0]["active"] = False
                tabs.append(
                    {
                        "tabId": "tab-new",
                        "url": "https://www.xiaohongshu.com/user/profile/abc",
                        "active": True,
                        "groupTitle": "用户页面",
                    }
                )
            return {"ok": True, "data": {"success": True, "tabs": tabs}}

    adapter = BorrowedSourceAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(
        lifecycle,
        adapter=adapter,
        run_validator=TEST_RUN_VALIDATOR,
    )
    binding = service.bindings.get_or_create(session_id="chat-session", run_id="run-1")
    service.bindings.record_command_result(
        binding,
        "find_tab",
        {
            "ok": True,
            "data": {
                "success": True,
                "tabId": "tab-user",
                "url": "https://www.xiaohongshu.com/search",
            },
        },
    )
    command = BrowserCommand(action="click", args={"selector": "@e51"})
    authorization = AuthorizedBrowserAction(
        session_id="chat-session",
        run_id="run-1",
        tool_call_id="click-borrowed-profile",
        action="click",
        args_digest=browser_action_digest("click", command.args),
    )

    with bind_authorized_browser_action(authorization):
        result = service.execute(command, session_id="chat-session", run_id="run-1")

    assert result.data["data"]["puddingclaw_tab_transition"]["mode"] == "active_same_group"
    assert "close_tab" not in [item["action"] for item in adapter.commands]


def test_link_click_preserves_an_explicit_multi_tab_run(tmp_path):
    class MultiTabAdapter(FakeAdapter):
        def command(self, **kwargs):
            self.commands.append(kwargs)
            action = kwargs["action"]
            list_count = sum(item["action"] == "list_tabs" for item in self.commands)
            if action == "click":
                return {"ok": True, "data": {"success": True, "tag": "A", "text": "profile"}}
            if action == "find_tab":
                return {
                    "ok": True,
                    "data": {
                        "success": True,
                        "tabId": "tab-new",
                        "url": "https://www.xiaohongshu.com/user/profile/abc",
                    },
                }
            tabs = [
                {
                    "tabId": "tab-extra",
                    "url": "https://example.com/reference",
                    "active": False,
                    "groupTitle": "对比任务",
                },
                {
                    "tabId": "tab-old",
                    "url": "https://www.xiaohongshu.com/search",
                    "active": False,
                    "groupTitle": "对比任务",
                },
            ]
            if list_count > 1:
                tabs.append(
                    {
                        "tabId": "tab-new",
                        "url": "https://www.xiaohongshu.com/user/profile/abc",
                        "active": True,
                        "groupTitle": "对比任务",
                    }
                )
            return {"ok": True, "data": {"success": True, "tabs": tabs}}

    adapter = MultiTabAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(
        lifecycle,
        adapter=adapter,
        run_validator=TEST_RUN_VALIDATOR,
    )
    binding = service.bindings.get_or_create(session_id="chat-session", run_id="run-1")
    binding = service.bindings.record_command_result(
        binding,
        "navigate",
        {"ok": True, "data": {"success": True, "tabId": "tab-extra", "url": "https://example.com"}},
    )
    service.bindings.record_command_result(
        binding,
        "navigate",
        {
            "ok": True,
            "data": {
                "success": True,
                "tabId": "tab-old",
                "url": "https://www.xiaohongshu.com/search",
            },
        },
    )
    command = BrowserCommand(action="click", args={"selector": "@e51"})
    authorization = AuthorizedBrowserAction(
        session_id="chat-session",
        run_id="run-1",
        tool_call_id="click-multi-tab-profile",
        action="click",
        args_digest=browser_action_digest("click", command.args),
    )

    with bind_authorized_browser_action(authorization):
        result = service.execute(command, session_id="chat-session", run_id="run-1")

    assert result.data["data"]["puddingclaw_tab_transition"]["mode"] == "active_same_group"
    assert "close_tab" not in [item["action"] for item in adapter.commands]


def test_unresolved_spawned_tab_pauses_old_tab_actions(tmp_path):
    class MissingOrphanTabAdapter(FakeAdapter):
        def command(self, **kwargs):
            self.commands.append(kwargs)
            if kwargs["action"] == "click":
                return {"ok": True, "data": {"success": True, "tag": "A", "text": "profile"}}
            if kwargs["action"] == "list_tabs":
                return {
                    "ok": True,
                    "data": {
                        "success": True,
                        "tabs": [
                            {
                                "tabId": "tab-old",
                                "url": "https://www.xiaohongshu.com/search",
                                "active": False,
                                "groupTitle": "小红书-卡兹克",
                            }
                        ],
                    },
                }
            return {"ok": False, "error": "no open tab found"}

    adapter = MissingOrphanTabAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(
        lifecycle,
        adapter=adapter,
        run_validator=TEST_RUN_VALIDATOR,
        tab_transition_retry_delays=(0.0, 0.0),
        sleep=lambda _seconds: None,
    )
    _seed_click_source_tab(service)
    command = BrowserCommand(action="click", args={"selector": "@e51"})
    authorization = AuthorizedBrowserAction(
        session_id="chat-session",
        run_id="run-1",
        tool_call_id="click-missing-profile",
        action="click",
        args_digest=browser_action_digest("click", command.args),
    )

    with bind_authorized_browser_action(authorization):
        click_result = service.execute(command, session_id="chat-session", run_id="run-1")
    snapshot_result = service.execute(
        BrowserCommand(action="snapshot"),
        session_id="chat-session",
        run_id="run-1",
    )
    navigate_result = service.execute(
        BrowserCommand(action="navigate", args={"url": "https://www.xiaohongshu.com/explore/note"}),
        session_id="chat-session",
        run_id="run-1",
    )

    assert click_result.data["data"]["puddingclaw_tab_transition"] == {
        "status": "unresolved",
        "reason": "active_same_host_tab_not_found",
    }
    assert snapshot_result.error == "browser_tab_transition_unresolved"
    assert navigate_result.error == "browser_tab_transition_unresolved"
    assert [item["action"] for item in adapter.commands] == [
        "click",
        "list_tabs",
        "find_tab",
        "find_tab",
        "find_tab",
    ]
    latest = service.bindings.get_or_create(session_id="chat-session", run_id="run-1")
    assert latest["current_tab_id"] is None


def test_link_click_reports_unresolved_for_an_unrelated_active_tab(tmp_path):
    class UnrelatedTabAdapter(FakeAdapter):
        def command(self, **kwargs):
            self.commands.append(kwargs)
            if kwargs["action"] == "click":
                return {"ok": True, "data": {"success": True, "tag": "A", "text": "profile"}}
            if kwargs["action"] == "list_tabs":
                return {
                    "ok": True,
                    "data": {
                        "success": True,
                        "tabs": [
                            {
                                "tabId": "tab-old",
                                "url": "https://www.xiaohongshu.com/search",
                                "active": False,
                                "groupTitle": "小红书-卡兹克",
                            },
                            {
                                "tabId": "tab-user",
                                "url": "https://example.com/private",
                                "active": True,
                                "groupTitle": "用户页面",
                            },
                        ],
                    },
                }
            raise AssertionError("an unrelated active tab must not be adopted")

    adapter = UnrelatedTabAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)
    _seed_click_source_tab(service)
    command = BrowserCommand(action="click", args={"selector": "@e33"})
    authorization = AuthorizedBrowserAction(
        session_id="chat-session",
        run_id="run-1",
        tool_call_id="click-unrelated",
        action="click",
        args_digest=browser_action_digest("click", command.args),
    )

    with bind_authorized_browser_action(authorization):
        result = service.execute(command, session_id="chat-session", run_id="run-1")
    with bind_authorized_browser_action(authorization):
        repeated = service.execute(command, session_id="chat-session", run_id="run-1")

    assert result.status == "ok"
    assert result.data["data"]["puddingclaw_tab_transition"] == {
        "status": "unresolved",
        "reason": "active_tab_not_click_related",
    }
    assert [item["action"] for item in adapter.commands] == ["click", "list_tabs"]
    assert repeated.error == "browser_tab_transition_unresolved"
    latest = service.bindings.get_or_create(session_id="chat-session", run_id="run-1")
    assert latest["current_tab_id"] == "tab-old"


def test_non_link_click_does_not_probe_or_adopt_tabs(tmp_path):
    class ButtonAdapter(FakeAdapter):
        def command(self, **kwargs):
            self.commands.append(kwargs)
            return {"ok": True, "data": {"success": True, "tag": "BUTTON", "text": "搜索"}}

    adapter = ButtonAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)
    _seed_click_source_tab(service)
    command = BrowserCommand(action="click", args={"selector": "@e2"})
    authorization = AuthorizedBrowserAction(
        session_id="chat-session",
        run_id="run-1",
        tool_call_id="click-button",
        action="click",
        args_digest=browser_action_digest("click", command.args),
    )

    with bind_authorized_browser_action(authorization):
        result = service.execute(command, session_id="chat-session", run_id="run-1")

    assert result.status == "ok"
    assert "puddingclaw_tab_transition" not in result.data["data"]
    assert [item["action"] for item in adapter.commands] == ["click"]


def test_link_click_tab_probe_failure_is_outcome_unknown_and_fenced(tmp_path):
    class ProbeFailureAdapter(FakeAdapter):
        def command(self, **kwargs):
            self.commands.append(kwargs)
            if kwargs["action"] == "click":
                return {"ok": True, "data": {"success": True, "tag": "A", "text": "profile"}}
            return {"error": "invalid tab response"}

    adapter = ProbeFailureAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)
    _seed_click_source_tab(service)
    command = BrowserCommand(action="click", args={"selector": "@e33"})
    authorization = AuthorizedBrowserAction(
        session_id="chat-session",
        run_id="run-1",
        tool_call_id="click-probe-failure",
        action="click",
        args_digest=browser_action_digest("click", command.args),
    )

    with bind_authorized_browser_action(authorization):
        first = service.execute(command, session_id="chat-session", run_id="run-1")
    with bind_authorized_browser_action(authorization):
        second = service.execute(command, session_id="chat-session", run_id="run-1")

    assert first.error == "browser_action_outcome_unknown"
    assert second.error == "browser_action_outcome_unknown"
    assert [item["action"] for item in adapter.commands] == ["click", "list_tabs"]


def test_missing_daemon_success_marker_is_not_treated_as_success(tmp_path):
    adapter = FakeAdapter()

    def malformed_command(**kwargs):
        adapter.commands.append(kwargs)
        return {"error": "element not found"}

    adapter.command = malformed_command
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)

    result = service.execute(
        BrowserCommand(action="navigate", args={"url": "https://example.com"}),
        session_id="chat-session",
        run_id="run-1",
    )

    assert result.status == "error"
    assert result.error == "browser_action_outcome_unknown"


def test_browser_approval_fingerprint_binds_full_fill_value_without_logging_it():
    first = ToolCallRequest(
        tool_call={"name": "browser", "args": {"action": "fill", "args": {"selector": "#q", "value": "ab"}}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={}),
    )
    second = ToolCallRequest(
        tool_call={"name": "browser", "args": {"action": "fill", "args": {"selector": "#q", "value": "cd"}}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={}),
    )
    pipeline = ToolExecutionPipeline.__new__(ToolExecutionPipeline)
    first_preview = pipeline._action_preview(first)
    second_preview = pipeline._action_preview(second)
    first_command = pipeline._permission_fingerprint_command(first, first_preview)
    second_command = pipeline._permission_fingerprint_command(second, second_preview)

    assert first_command != second_command
    assert "ab" not in first_preview
    assert PermissionResumeRegistry.tool_action_fingerprint(
        tool_name="browser", command=first_command, reason="browser_interaction_confirmation"
    ) != PermissionResumeRegistry.tool_action_fingerprint(
        tool_name="browser", command=second_command, reason="browser_interaction_confirmation"
    )


def _seed_confirmed_textarea_target(service, *, selector="@e32", tab_id="tab-1"):
    binding = service.bindings.get_or_create(session_id="chat-session", run_id="run-1")
    binding = service.bindings.record_command_result(
        binding,
        "navigate",
        {"ok": True, "data": {"success": True, "tabId": tab_id, "url": "https://example.com"}},
    )
    binding = service.bindings.begin_final_action(binding, "click", browser_action_digest("click", {"selector": selector}))
    return service.bindings.record_command_result(
        binding,
        "click",
        {"ok": True, "data": {"success": True, "tag": "TEXTAREA"}},
        action_args={"selector": selector},
    )


def test_affected_extension_recovers_textarea_fill_inside_original_authorization(tmp_path):
    class TextareaBugAdapter(FakeAdapter):
        def __init__(self):
            super().__init__(
                {
                    "running": True,
                    "extension_connected": True,
                    "version": "1.11.5",
                    "extension_version": "1.11.5",
                }
            )

        def command(self, **kwargs):
            self.commands.append(kwargs)
            if kwargs["action"] == "fill":
                return {"ok": False, "error": "fill: Uncaught"}
            return {"ok": True, "data": {"success": True}}

        def replace_focused_textarea_value(self, **kwargs):
            self.commands.append({"action": "replace_focused_textarea_value", **kwargs})
            return {
                "ok": True,
                "data": {
                    "success": True,
                    "mode": "cdp_insert_text",
                    "compatibility_fallback": "kimi_webbridge_textarea_setter_1_11_5",
                },
            }

    adapter = TextareaBugAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)
    _seed_confirmed_textarea_target(service)
    command = BrowserCommand(action="fill", args={"selector": "@e32", "value": "卡兹克"})
    authorization = AuthorizedBrowserAction(
        session_id="chat-session",
        run_id="run-1",
        tool_call_id="fill-textarea",
        action="fill",
        args_digest=browser_action_digest("fill", command.args),
    )

    with bind_authorized_browser_action(authorization):
        result = service.execute(command, session_id="chat-session", run_id="run-1")

    assert result.status == "ok"
    assert result.data["data"]["mode"] == "cdp_insert_text"
    assert result.data["data"]["compatibility_fallback"] == "kimi_webbridge_textarea_setter_1_11_5"
    assert "卡兹克" not in result.model_dump_json()
    assert [call["action"] for call in adapter.commands] == ["fill", "replace_focused_textarea_value"]
    assert adapter.commands[1]["value"] == "卡兹克"
    assert {call["session_id"] for call in adapter.commands} == {
        adapter.commands[0]["session_id"]
    }


def test_textarea_fill_compatibility_requires_verified_textarea_target(tmp_path):
    class UnverifiedTargetAdapter(FakeAdapter):
        def __init__(self):
            super().__init__(
                {
                    "running": True,
                    "extension_connected": True,
                    "version": "1.11.5",
                    "extension_version": "1.11.5",
                }
            )

        def command(self, **kwargs):
            self.commands.append(kwargs)
            return {"ok": False, "error": "fill: Uncaught"}

        def replace_focused_textarea_value(self, **kwargs):
            raise AssertionError("unverified target must not use compatibility input")

    adapter = UnverifiedTargetAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)
    command = BrowserCommand(action="fill", args={"selector": "@e32", "value": "value"})
    authorization = AuthorizedBrowserAction(
        session_id="chat-session",
        run_id="run-1",
        tool_call_id="fill-unverified",
        action="fill",
        args_digest=browser_action_digest("fill", command.args),
    )

    with bind_authorized_browser_action(authorization):
        result = service.execute(command, session_id="chat-session", run_id="run-1")

    assert result.error == "webbridge_command_failed"
    assert [call["action"] for call in adapter.commands] == ["fill"]


def test_textarea_fill_compatibility_does_not_activate_for_newer_extension(tmp_path):
    adapter = FakeAdapter(
        {
            "running": True,
            "extension_connected": True,
            "version": "1.11.6",
            "extension_version": "1.11.6",
        }
    )

    def failed_command(**kwargs):
        adapter.commands.append(kwargs)
        return {"ok": False, "error": "fill: Uncaught"}

    adapter.command = failed_command
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)
    command = BrowserCommand(action="fill", args={"selector": "@e1", "value": "value"})
    authorization = AuthorizedBrowserAction(
        session_id="chat-session",
        run_id="run-1",
        tool_call_id="fill-newer",
        action="fill",
        args_digest=browser_action_digest("fill", command.args),
    )

    with bind_authorized_browser_action(authorization):
        result = service.execute(command, session_id="chat-session", run_id="run-1")

    assert result.error == "webbridge_command_failed"
    assert [call["action"] for call in adapter.commands] == ["fill"]


def test_textarea_fill_compatibility_requires_known_error_signature(tmp_path):
    adapter = FakeAdapter(
        {
            "running": True,
            "extension_connected": True,
            "version": "1.11.5",
            "extension_version": "1.11.5",
        }
    )

    def failed_command(**kwargs):
        adapter.commands.append(kwargs)
        return {"ok": False, "error": "element is disabled"}

    adapter.command = failed_command
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)
    command = BrowserCommand(action="fill", args={"selector": "@e1", "value": "value"})
    authorization = AuthorizedBrowserAction(
        session_id="chat-session",
        run_id="run-1",
        tool_call_id="fill-other-error",
        action="fill",
        args_digest=browser_action_digest("fill", command.args),
    )

    with bind_authorized_browser_action(authorization):
        result = service.execute(command, session_id="chat-session", run_id="run-1")

    assert result.error == "webbridge_command_failed"
    assert [call["action"] for call in adapter.commands] == ["fill"]


def test_textarea_fill_compatibility_internal_failure_is_outcome_unknown_and_fenced(tmp_path):
    class SelectFailureAdapter(FakeAdapter):
        def __init__(self):
            super().__init__(
                {
                    "running": True,
                    "extension_connected": True,
                    "version": "1.11.5",
                    "extension_version": "1.11.5",
                }
            )

        def command(self, **kwargs):
            self.commands.append(kwargs)
            return {"ok": False, "error": "fill: Illegal invocation"}

        def replace_focused_textarea_value(self, **kwargs):
            self.commands.append({"action": "replace_focused_textarea_value", **kwargs})
            raise WebBridgeError("browser_action_outcome_unknown", "internal action failed")

    adapter = SelectFailureAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)
    _seed_confirmed_textarea_target(service, selector="@e1")
    command = BrowserCommand(action="fill", args={"selector": "@e1", "value": "value"})
    authorization = AuthorizedBrowserAction(
        session_id="chat-session",
        run_id="run-1",
        tool_call_id="fill-fallback-failure",
        action="fill",
        args_digest=browser_action_digest("fill", command.args),
    )

    with bind_authorized_browser_action(authorization):
        first = service.execute(command, session_id="chat-session", run_id="run-1")
    with bind_authorized_browser_action(authorization):
        second = service.execute(command, session_id="chat-session", run_id="run-1")

    assert first.error == "browser_action_outcome_unknown"
    assert second.error == "browser_action_outcome_unknown"
    assert first.retryable is False
    assert second.retryable is False
    assert [call["action"] for call in adapter.commands] == ["fill", "replace_focused_textarea_value"]


def test_textarea_fill_compatibility_timeout_is_outcome_unknown_and_fenced(tmp_path):
    class InsertTimeoutAdapter(FakeAdapter):
        def __init__(self):
            super().__init__(
                {
                    "running": True,
                    "extension_connected": True,
                    "version": "1.11.5",
                    "extension_version": "1.11.5",
                }
            )

        def command(self, **kwargs):
            self.commands.append(kwargs)
            return {"ok": False, "error": "fill: Uncaught"}

        def replace_focused_textarea_value(self, **kwargs):
            self.commands.append({"action": "replace_focused_textarea_value", **kwargs})
            raise WebBridgeError("daemon_timeout", "timed out", retryable=True)

    adapter = InsertTimeoutAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    service = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR)
    _seed_confirmed_textarea_target(service, selector="@e1")
    command = BrowserCommand(action="fill", args={"selector": "@e1", "value": "value"})
    authorization = AuthorizedBrowserAction(
        session_id="chat-session",
        run_id="run-1",
        tool_call_id="fill-fallback-timeout",
        action="fill",
        args_digest=browser_action_digest("fill", command.args),
    )

    with bind_authorized_browser_action(authorization):
        first = service.execute(command, session_id="chat-session", run_id="run-1")
    with bind_authorized_browser_action(authorization):
        second = service.execute(command, session_id="chat-session", run_id="run-1")

    assert first.error == "browser_action_outcome_unknown"
    assert second.error == "browser_action_outcome_unknown"
    assert first.retryable is False
    assert second.retryable is False
    assert [call["action"] for call in adapter.commands] == ["fill", "replace_focused_textarea_value"]


def test_screenshot_and_pdf_are_imported_as_session_attachments(tmp_path):
    adapter = FakeAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    canonical_root = tmp_path / "backend"
    store = AttachmentStore()
    store.initialize(canonical_root)
    service = KimiWebBridgeService(
        lifecycle,
        adapter=adapter,
        run_validator=TEST_RUN_VALIDATOR,
        attachment_store_instance=store,
    )

    for action in ("screenshot", "save_as_pdf"):
        result = service.execute(
            BrowserCommand(action=action), session_id="run-session", run_id="run-1"
        )
        assert result.status == "ok"
        assert "path" not in result.data
        assert result.data["artifact"]["download_url"].startswith("/api/attachments/")
        assert store.root_dir == canonical_root / "data" / "attachments"
        if action == "screenshot":
            route = result.data["puddingclaw_visual_route"]
            assert route == {
                "status": "not_analyzed",
                "subagent_type": "image_analyzer",
                "harness_attachment_session_id": "run-session",
                "attachment_ref": result.data["artifact"]["id"],
                "delegate_when": "visual_content_is_required",
                "display_only_when": "the_user_only_requested_the_screenshot_artifact",
            }
        else:
            assert "puddingclaw_visual_route" not in result.data


def test_nested_daemon_artifact_path_is_validated_but_never_returned_to_agent(tmp_path):
    class NestedArtifactAdapter(FakeAdapter):
        def command(self, **kwargs):
            self.commands.append(kwargs)
            path = Path(kwargs["args"]["path"])
            path.write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            ))
            return {
                "ok": True,
                "data": {
                    "success": True,
                    "path": str(path),
                    "mimeType": "image/png",
                },
            }

    adapter = NestedArtifactAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    store = AttachmentStore()
    store.initialize(tmp_path / "backend")
    result = KimiWebBridgeService(
        lifecycle,
        adapter=adapter,
        run_validator=TEST_RUN_VALIDATOR,
        attachment_store_instance=store,
    ).execute(BrowserCommand(action="screenshot"), session_id="run-session", run_id="run-1")

    assert result.status == "ok"
    assert "path" not in result.data
    assert "path" not in result.data["data"]
    assert result.data["artifact"]["id"].startswith("att_")


def test_browser_artifact_is_promoted_to_chat_attachment(tmp_path):
    adapter = FakeAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    attachment_store.initialize(tmp_path / "backend")
    service = KimiWebBridgeService(
        lifecycle,
        adapter=adapter,
        run_validator=TEST_RUN_VALIDATOR,
    )
    result = service.execute(BrowserCommand(action="screenshot"), session_id="run-session", run_id="run-1")

    resolved = resolve_browser_generated_attachment(
        result.model_dump_json(), session_id="run-session", run_id="run-1"
    )

    assert resolved is not None
    assert resolved["download_url"].startswith("/api/attachments/")


def test_service_rejects_side_effect_without_harness_authorization(tmp_path):
    adapter = FakeAdapter()
    lifecycle = KimiWebBridgeLifecycle(
        PuddingClawPaths(tmp_path / "puddingclaw"),
        adapter=adapter,
        daemon_path=_installed_binary(tmp_path / "bin" / "kimi-webbridge"),
    )
    lifecycle.set_enabled(True)
    result = KimiWebBridgeService(lifecycle, adapter=adapter, run_validator=TEST_RUN_VALIDATOR).execute(
        BrowserCommand(action="click", args={"selector": "@e1"}),
        session_id="run-session",
        run_id="run-1",
    )
    assert result.status == "error"
    assert result.error == "browser_harness_authorization_required"
    assert adapter.commands == []


def test_shell_bypass_variants_are_denied():
    context = RunPermissionContext.from_config_snapshot(
        {"permissions": {"approval_mode": "smart", "policy_epoch": 1}}
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode="spawn",
        permission_context=context,
    )
    commands = (
        "curl http://2130706433:10086/command",
        "curl http://0x7f000001:10086/command",
        "curl -L https://example.com",
        "curl --config ./request.conf",
        "kimi-webbridge status",
        "python3 -c \"import base64; exec(base64.b64decode('obfuscated'))\"",
    )
    for index, command in enumerate(commands):
        request = ToolCallRequest(
            tool_call={"id": f"shell-{index}", "name": "execute", "args": {"command": command}},
            tool=None,
            state={},
            runtime=SimpleNamespace(context={"session_id": "s", "run_id": "r"}),
        )
        assert pipeline._preflight(request).decision == PolicyDecision.DENY


def test_browser_output_never_becomes_public_citation_source():
    adapted = ToolResultAdapter().adapt(
        encode_tool_result(
            "private page text",
            [{"url": "https://private.example"}],
        ),
        tool_name="browser",
        tool_call_id="browser-1",
    )

    assert adapted.adapter == "browser_private"
    assert adapted.answer_context == "private page text"
    assert adapted.sources == []
