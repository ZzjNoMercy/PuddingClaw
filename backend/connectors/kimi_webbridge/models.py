"""Typed contracts for the Kimi WebBridge browser-control surface."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BrowserAction = Literal[
    "list_tabs",
    "find_tab",
    "snapshot",
    "scroll",
    "navigate",
    "click",
    "fill",
    "screenshot",
    "save_as_pdf",
    "close_tab",
    "close_session",
]


def canonicalize_browser_args(action: str, args: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize harmless model-facing aliases before validation and authorization.

    Snapshot element handles are selectors in the WebBridge wire contract. Some
    models nevertheless call the field ``ref`` because the handles look like
    ``@e1``. Accept that spelling as an input alias, but keep one canonical
    representation so Harness and service authorization digests stay identical.
    """

    normalized = dict(args or {})
    if action in {"click", "fill", "screenshot"} and "ref" in normalized:
        ref = normalized.pop("ref")
        selector = normalized.get("selector")
        if selector is not None and selector != ref:
            raise ValueError(f"{action}.ref conflicts with {action}.selector")
        normalized["selector"] = ref
    return normalized


class BrowserCommand(BaseModel):
    """Model-facing command envelope.

    Session and run identity are deliberately not model-controlled fields;
    the service binds them from the trusted Agent runtime context.
    """

    model_config = ConfigDict(extra="forbid")

    action: BrowserAction
    args: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_action_args(cls, value):
        if not isinstance(value, Mapping):
            return value
        action = value.get("action")
        args = value.get("args")
        if not isinstance(action, str) or not isinstance(args, Mapping):
            return value
        normalized = dict(value)
        normalized["args"] = canonicalize_browser_args(action, args)
        return normalized

    @model_validator(mode="after")
    def validate_action_args(self):
        allowed = {
            "list_tabs": set(),
            "find_tab": {"url", "active"},
            "snapshot": set(),
            "scroll": {"direction", "amount", "scope"},
            "navigate": {"url", "newTab", "group_title"},
            "click": {"selector"},
            "fill": {"selector", "value"},
            "screenshot": {"format", "quality", "selector"},
            "save_as_pdf": {"paper_format", "landscape", "scale", "print_background"},
            "close_tab": set(),
            "close_session": set(),
        }[self.action]
        unknown = sorted(set(self.args) - allowed)
        if unknown:
            raise ValueError(f"browser action arguments not allowed: {', '.join(unknown)}")
        if self.action == "navigate":
            url = self.args.get("url")
            if not isinstance(url, str) or not url.strip():
                raise ValueError("browser URL is required")
        if self.action == "find_tab":
            url = self.args.get("url")
            active = self.args.get("active")
            if url is not None and (not isinstance(url, str) or not url.strip()):
                raise ValueError("find_tab.url is invalid")
            if "active" in self.args and not isinstance(active, bool):
                raise ValueError("find_tab.active must be boolean")
            if not isinstance(url, str) and active is not True:
                raise ValueError("find_tab requires a URL or active=true")
        if self.action == "navigate":
            if "newTab" in self.args and not isinstance(self.args["newTab"], bool):
                raise ValueError("navigate.newTab must be boolean")
            if "group_title" in self.args and (
                not isinstance(self.args["group_title"], str)
                or len(self.args["group_title"]) > 120
            ):
                raise ValueError("navigate.group_title is invalid")
        if self.action == "scroll":
            direction = self.args.get("direction", "down")
            amount = self.args.get("amount", 800)
            scope = self.args.get("scope", "largest_scrollable")
            if direction not in {"up", "down"}:
                raise ValueError("scroll.direction must be up or down")
            if isinstance(amount, bool) or not isinstance(amount, int) or not 100 <= amount <= 5000:
                raise ValueError("scroll.amount must be an integer between 100 and 5000")
            if scope not in {"page", "largest_scrollable"}:
                raise ValueError("scroll.scope must be page or largest_scrollable")
        if self.action in {"click", "fill"}:
            if not isinstance(self.args.get("selector"), str) or not self.args["selector"].strip():
                raise ValueError(f"{self.action}.selector is required")
        if self.action == "fill":
            if not isinstance(self.args.get("value"), str) or len(self.args["value"]) > 50_000:
                raise ValueError("fill.value is invalid")
        if self.action == "screenshot":
            if "selector" in self.args and (
                not isinstance(self.args["selector"], str) or not self.args["selector"].strip()
            ):
                raise ValueError("screenshot.selector is invalid")
            if "format" in self.args and self.args["format"] not in {"png", "jpeg", "webp"}:
                raise ValueError("screenshot.format is invalid")
            if "quality" in self.args and (
                isinstance(self.args["quality"], bool)
                or not isinstance(self.args["quality"], int)
                or not 0 <= self.args["quality"] <= 100
            ):
                raise ValueError("screenshot.quality is invalid")
        if self.action == "save_as_pdf":
            if "paper_format" in self.args and self.args["paper_format"] not in {
                "letter", "a4", "legal", "a3", "tabloid",
            }:
                raise ValueError("save_as_pdf.paper_format is invalid")
            if "landscape" in self.args and not isinstance(self.args["landscape"], bool):
                raise ValueError("save_as_pdf.landscape must be boolean")
            if "scale" in self.args and (
                isinstance(self.args["scale"], bool)
                or not isinstance(self.args["scale"], (int, float))
                or not 0.1 <= float(self.args["scale"]) <= 2.0
            ):
                raise ValueError("save_as_pdf.scale is invalid")
            if "print_background" in self.args and not isinstance(self.args["print_background"], bool):
                raise ValueError("save_as_pdf.print_background must be boolean")
        return self


class BrowserResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: Literal["ok", "needs_input", "error"]
    action: str
    data: Any = None
    error: str | None = None
    retryable: bool = False
