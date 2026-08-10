"""Model-facing browser tool backed by the built-in Kimi WebBridge connector."""

from __future__ import annotations

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from connectors.kimi_webbridge.lifecycle import KimiWebBridgeLifecycle
from connectors.kimi_webbridge.models import BrowserCommand
from connectors.kimi_webbridge.service import KimiWebBridgeService
from harness.execution_context import browser_action_digest, current_authorized_browser_action
from runtime_identity.paths import PuddingClawPaths


class BrowserToolInput(BrowserCommand):
    """Expose the same validated envelope that the service executes."""


class BrowserTool(BaseTool):
    name: str = "browser"
    description: str = (
        "Use the user's connected browser with existing login state. Supports navigation, "
        "snapshot, safe viewport scrolling, screenshot, PDF, click, fill, tab listing, and session cleanup. "
        "Pass snapshot @e element references in args.selector for click/fill. "
        "A screenshot result publishes an image attachment but does not expose pixels to the main Agent. "
        "When visual interpretation is required, delegate its puddingclaw_visual_route to image_analyzer; "
        "when the user only wants the image, return the published attachment without analysis. "
        "Use scroll instead of clicking unrelated page elements when content is outside the viewport. "
        "Do not use evaluate, upload, or network; final interactions require user approval."
    )
    args_schema: type[BaseModel] = BrowserToolInput
    risk_level: str = "moderate"
    session_id: str = ""
    run_id: str = ""
    service: KimiWebBridgeService | None = None

    def _service(self) -> KimiWebBridgeService:
        if self.service is not None:
            return self.service
        lifecycle = KimiWebBridgeLifecycle(PuddingClawPaths.from_environment())
        return KimiWebBridgeService(lifecycle)

    def _run(self, action: str, args: dict | None = None) -> str:
        if not self.session_id:
            return '{"status":"error","error":"missing_runtime_binding"}'
        try:
            command = BrowserCommand(action=action, args=args or {})
        except Exception:
            return '{"status":"error","error":"invalid_browser_command"}'
        authorized = current_authorized_browser_action()
        if (
            authorized is None
            or authorized.session_id != self.session_id
            or authorized.run_id != self.run_id
            or authorized.action != action
            or authorized.args_digest != browser_action_digest(action, command.args)
        ):
            return '{"status":"error","error":"browser_harness_authorization_required"}'
        return self._service().execute(command, session_id=self.session_id, run_id=self.run_id).model_dump_json()


def create_browser_tool() -> BrowserTool:
    return BrowserTool()
