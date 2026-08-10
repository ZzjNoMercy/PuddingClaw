"""Strict localhost adapter for the Kimi WebBridge daemon."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from .models import BrowserAction

BASE_URL = "http://127.0.0.1:10086"
COMMAND_PATH = "/command"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 3.0
ACTION_RESPONSE_TIMEOUT_SECONDS: dict[BrowserAction, float] = {
    "list_tabs": 10.0,
    "find_tab": 10.0,
    "snapshot": 30.0,
    "scroll": 15.0,
    "navigate": 30.0,
    "click": 15.0,
    "fill": 15.0,
    "screenshot": 30.0,
    "save_as_pdf": 60.0,
    "close_tab": 15.0,
    "close_session": 15.0,
}
_TEXTAREA_REPLACEMENT_RESPONSE_TIMEOUT_SECONDS = 15.0


class WebBridgeError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class KimiWebBridgeAdapter:
    """Only component allowed to speak to the local WebBridge daemon."""

    def __init__(
        self,
        *,
        timeout_seconds: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        """Create an adapter with action-aware response timeouts.

        ``timeout_seconds`` remains as a test/compatibility override for every
        timeout. Production callers should use the defaults: localhost connect
        failures remain fast while browser work gets enough time to finish.
        """

        self.timeout_seconds = timeout_seconds
        self.connect_timeout_seconds = (
            float(timeout_seconds) if timeout_seconds is not None else CONNECT_TIMEOUT_SECONDS
        )
        if client is not None and str(client.base_url).rstrip("/") != BASE_URL:
            raise ValueError("WebBridge adapter client must target the fixed localhost daemon")
        self._client = client

    def _response_timeout(self, action: BrowserAction) -> float:
        if self.timeout_seconds is not None:
            return float(self.timeout_seconds)
        return ACTION_RESPONSE_TIMEOUT_SECONDS[action]

    def _request(
        self,
        method: str,
        path: str,
        *,
        response_timeout_seconds: float,
        **kwargs: Any,
    ) -> httpx.Response:
        client = self._client
        owned = client is None
        timeout = httpx.Timeout(
            response_timeout_seconds,
            connect=self.connect_timeout_seconds,
        )
        if owned:
            client = httpx.Client(
                base_url=BASE_URL,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            )
        try:
            response = client.request(method, path, timeout=timeout, **kwargs)
        except httpx.TimeoutException as exc:
            raise WebBridgeError("daemon_timeout", "WebBridge daemon 响应超时", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise WebBridgeError("daemon_unreachable", "WebBridge daemon 不可达", retryable=True) from exc
        finally:
            if owned:
                client.close()
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise WebBridgeError("response_too_large", "WebBridge 响应超过大小限制")
        return response

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 500:
            raise WebBridgeError("daemon_server_error", "WebBridge daemon 内部错误", retryable=True)
        if response.status_code >= 400:
            raise WebBridgeError("daemon_rejected", "WebBridge daemon 拒绝了请求")
        try:
            payload = response.json()
        except ValueError as exc:
            raise WebBridgeError("invalid_daemon_response", "WebBridge 返回了无效 JSON") from exc
        if not isinstance(payload, dict):
            raise WebBridgeError("invalid_daemon_response", "WebBridge 返回的 JSON 根节点不是对象")
        return payload

    def command(
        self,
        *,
        action: BrowserAction,
        args: Mapping[str, object],
        session_id: str,
    ) -> dict[str, Any]:
        if action not in ACTION_RESPONSE_TIMEOUT_SECONDS:
            raise WebBridgeError("unsupported_browser_action", "不支持的 WebBridge 浏览器动作")
        if action == "scroll":
            return self._scroll_page(args=args, session_id=session_id)
        return self._wire_command(
            action=action,
            args=args,
            session_id=session_id,
            response_timeout_seconds=self._response_timeout(action),
        )

    def _scroll_page(self, *, args: Mapping[str, object], session_id: str) -> dict[str, Any]:
        """Scroll through a fixed, non-programmable viewport operation.

        Arbitrary ``evaluate`` remains unavailable to the model. The adapter
        translates the narrow scroll contract into a constant script so pages
        with modal/comment scrollers do not force the Agent to click unrelated
        links merely to move the viewport.
        """

        direction = -1 if args.get("direction", "down") == "up" else 1
        amount = int(args.get("amount", 800))
        scope = str(args.get("scope", "largest_scrollable"))
        code = f"""(() => {{
          const direction = {direction};
          const amount = {amount};
          const page = document.scrollingElement || document.documentElement;
          const visibleArea = (el) => {{
            const rect = el.getBoundingClientRect();
            const width = Math.max(0, Math.min(innerWidth, rect.right) - Math.max(0, rect.left));
            const height = Math.max(0, Math.min(innerHeight, rect.bottom) - Math.max(0, rect.top));
            return width * height;
          }};
          const candidates = Array.from(document.querySelectorAll('*')).filter((el) => {{
            const style = getComputedStyle(el);
            return /(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 8 && visibleArea(el) > 0;
          }});
          candidates.sort((a, b) => visibleArea(b) - visibleArea(a));
          const target = {"page" if scope == "page" else "candidates[0] || page"};
          const before = target.scrollTop;
          target.scrollBy({{ top: direction * amount, behavior: 'instant' }});
          return {{ success: true, before, after: target.scrollTop, scope: target === page ? 'page' : 'largest_scrollable' }};
        }})()"""
        return self._wire_command(
            action="evaluate",
            args={"code": code},
            session_id=session_id,
            response_timeout_seconds=self._response_timeout("scroll"),
        )

    def replace_focused_textarea_value(self, *, value: str, session_id: str) -> dict[str, Any]:
        """Apply the narrow Kimi 1.11.5 textarea workaround.

        The model-facing command API cannot send arbitrary key primitives. This
        fixed sequence is intentionally the only internal entry point. Once the
        sequence starts, any non-success is outcome-unknown because selection or
        insertion may already have reached Chrome.
        """

        if not session_id:
            raise WebBridgeError("missing_session_binding", "缺少 Backend 绑定的 WebBridge session")
        for action, args in (
            ("send_keys", {"keys": "Mod+A"}),
            ("key_type", {"text": value}),
        ):
            try:
                payload = self._wire_command(
                    action=action,
                    args=args,
                    session_id=session_id,
                    response_timeout_seconds=(
                        float(self.timeout_seconds)
                        if self.timeout_seconds is not None
                        else _TEXTAREA_REPLACEMENT_RESPONSE_TIMEOUT_SECONDS
                    ),
                )
            except WebBridgeError as exc:
                # Once a wire request begins, no HTTP/JSON failure can prove
                # that Chrome did not receive some or all of the input.
                raise WebBridgeError(
                    "browser_action_outcome_unknown",
                    "WebBridge textarea compatibility action outcome is unknown",
                ) from exc
            markers = [
                item
                for container in (payload, payload.get("data"))
                if isinstance(container, dict)
                for key in ("ok", "success")
                if isinstance((item := container.get(key)), bool)
            ]
            if any(item is False for item in markers) or not any(item is True for item in markers):
                raise WebBridgeError(
                    "browser_action_outcome_unknown",
                    "WebBridge textarea compatibility action outcome is unknown",
                )
        return {
            "ok": True,
            "data": {
                "success": True,
                "mode": "cdp_insert_text",
                "compatibility_fallback": "kimi_webbridge_textarea_setter_1_11_5",
            },
        }

    def _wire_command(
        self,
        *,
        action: str,
        args: Mapping[str, object],
        session_id: str,
        response_timeout_seconds: float,
    ) -> dict[str, Any]:
        if not session_id:
            raise WebBridgeError("missing_session_binding", "缺少 Backend 绑定的 WebBridge session")
        response = self._request(
            "POST",
            COMMAND_PATH,
            response_timeout_seconds=response_timeout_seconds,
            # This is the Kimi WebBridge wire contract. ``session`` is a
            # top-level task group; it is not part of args and is not named
            # ``session_id``.
            json={"action": action, "args": dict(args), "session": session_id},
        )
        return self._json(response)
