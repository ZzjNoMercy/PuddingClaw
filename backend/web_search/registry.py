"""User-local registry for web-search providers and routing preferences."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from provider_registry import (
    CredentialVaultDecryptionError,
    LocalCredentialStore,
    get_provider_registry,
    user_data_dir,
)

PROVIDER_IDS = ("tavily", "deepseek", "grok")
PROVIDER_MANIFEST: dict[str, dict[str, Any]] = {
    "tavily": {
        "name": "Tavily",
        "description": "面向 Agent 的通用公网搜索，覆盖国内与全球公开网页。",
        "website": "https://app.tavily.com",
        "docs": "https://docs.tavily.com/documentation/api-reference/endpoint/search",
        "model": None,
        "base_url": "https://api.tavily.com",
        "required_packages": [],
    },
    "deepseek": {
        "name": "DeepSeek Search",
        "description": "通过 DeepSeek v4 Flash 的服务端搜索检索国内公开互联网。",
        "website": "https://platform.deepseek.com",
        "docs": "https://api-docs.deepseek.com/zh-cn/guides/responses_api/",
        "model": "deepseek-v4-flash",
        "base_url": "https://api.deepseek.com",
        "required_packages": [],
    },
    "grok": {
        "name": "Grok Search",
        "description": "检索全球网页与 X 实时内容；每次搜索包含一次 Grok 模型调用。",
        "website": "https://console.x.ai",
        "docs": "https://docs.x.ai/developers/tools/web-search",
        "model": "grok-4.5",
        "base_url": "https://api.x.ai/v1",
        "required_packages": [],
    },
}


def _default_provider(provider_id: str) -> dict[str, Any]:
    options: dict[str, Any] = {"max_results": 5}
    if provider_id == "tavily":
        options.update({"search_depth": "basic"})
    if provider_id == "grok":
        options.update(
            {
                "web_search_enabled": True,
                "x_search_enabled": True,
                "x_image_understanding_enabled": False,
                "x_video_understanding_enabled": False,
            }
        )
    return {
        "enabled": False,
        "state": "disabled",
        "credential_ref": "",
        "options": options,
        "last_test": None,
        "last_error": "",
    }


def _default_payload() -> dict[str, Any]:
    return {
        "version": 1,
        "default_scope": "global",
        "routing": {
            "domestic": ["deepseek", "tavily", "grok"],
            "global": ["grok", "tavily", "deepseek"],
            "fallback_enabled": True,
            "max_provider_attempts": 2,
            "cross_check_enabled": False,
        },
        "providers": {provider_id: _default_provider(provider_id) for provider_id in PROVIDER_IDS},
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else _default_payload()
    except (OSError, ValueError, json.JSONDecodeError):
        return _default_payload()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    fd, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if os.name != "nt":
                os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "•" * len(secret)
    # A fixed-length mask is easier to scan and does not disclose secret length.
    return f"{secret[:4]}••••••••{secret[-4:]}"


class WebSearchRegistry:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or user_data_dir()
        self.path = self.root / "web-search.json"
        self.credentials = LocalCredentialStore(self.root)

    def _payload(self) -> dict[str, Any]:
        payload = _read_json(self.path)
        defaults = _default_payload()
        payload.setdefault("version", 1)
        # v1 briefly exposed a redundant global switch. Provider readiness is
        # now the single source of truth; discard the legacy field on read.
        payload.pop("enabled", None)
        payload.setdefault("default_scope", "global")
        routing = payload.setdefault("routing", {})
        for key, value in defaults["routing"].items():
            routing.setdefault(key, copy.deepcopy(value))
        providers = payload.setdefault("providers", {})
        for provider_id in PROVIDER_IDS:
            current = providers.setdefault(provider_id, _default_provider(provider_id))
            for key, value in _default_provider(provider_id).items():
                current.setdefault(key, copy.deepcopy(value))
        return payload

    def _save(self, payload: dict[str, Any]) -> None:
        _atomic_write(self.path, payload)

    def raw(self) -> dict[str, Any]:
        return copy.deepcopy(self._payload())

    def available(self) -> bool:
        """Return whether the managed tool has at least one executable route."""
        payload = self._payload()
        return any(
            provider.get("enabled") and provider.get("state") == "ready"
            for provider in payload["providers"].values()
            if isinstance(provider, dict)
        )

    def _own_credential(self, provider_id: str, payload: dict[str, Any]) -> str:
        provider = payload["providers"][provider_id]
        return self.credentials.get(str(provider.get("credential_ref") or ""))

    def credential(self, provider_id: str) -> tuple[str, str]:
        if provider_id not in PROVIDER_IDS:
            raise ValueError(f"Unknown web-search provider: {provider_id}")
        payload = self._payload()
        own = self._own_credential(provider_id, payload)
        if own:
            return own, "web_search"
        if provider_id == "tavily":
            value = os.getenv("TAVILY_API_KEY", "").strip()
            if value:
                return value, "environment"
        if provider_id == "deepseek":
            try:
                value = get_provider_registry().resolve_credential_for_runtime(
                    "deepseek", "default"
                )
            except CredentialVaultDecryptionError:
                raise
            except ValueError:
                value = ""
            if value:
                return value, "provider_registry"
        return "", ""

    def display(self) -> dict[str, Any]:
        payload = self._payload()
        result = copy.deepcopy(payload)
        rendered: list[dict[str, Any]] = []
        for provider_id in PROVIDER_IDS:
            provider = result["providers"][provider_id]
            credential_error = ""
            configured_but_unreadable = False
            try:
                secret, source = self.credential(provider_id)
            except CredentialVaultDecryptionError as exc:
                secret = ""
                configured_but_unreadable = True
                source = (
                    "web_search"
                    if provider.get("credential_ref")
                    else "provider_registry"
                    if provider_id == "deepseek"
                    else ""
                )
                credential_error = str(exc)
            provider.pop("credential_ref", None)
            provider.update(PROVIDER_MANIFEST[provider_id])
            provider["id"] = provider_id
            provider["credential_configured"] = bool(secret) or configured_but_unreadable
            provider["api_key_masked"] = _mask(secret) or (
                "••••••••" if configured_but_unreadable else ""
            )
            provider["credential_source"] = source
            provider["credential_readable"] = not credential_error
            provider["credential_error"] = credential_error
            provider["dependencies"] = {
                "status": "already_satisfied",
                "packages": PROVIDER_MANIFEST[provider_id]["required_packages"],
            }
            rendered.append(provider)
        result["providers"] = rendered
        result["ready_providers"] = [
            item["id"] for item in rendered if item.get("enabled") and item.get("state") == "ready"
        ]
        vault_errors = [
            str(item.get("credential_error") or "")
            for item in rendered
            if item.get("credential_error")
        ]
        result["credential_vault"] = {
            "readable": not vault_errors,
            "error": vault_errors[0] if vault_errors else "",
        }
        return result

    def save_credential(self, provider_id: str, api_key: str) -> dict[str, Any]:
        value = api_key.strip()
        if not value:
            raise ValueError("API Key 不能为空")
        payload = self._payload()
        if provider_id not in PROVIDER_IDS:
            raise ValueError(f"Unknown web-search provider: {provider_id}")
        reference = self.credentials.put(f"web-search-{provider_id}-default", value)
        provider = payload["providers"][provider_id]
        provider["credential_ref"] = reference
        provider["enabled"] = False
        provider["state"] = "needs_test"
        provider["last_error"] = ""
        self._save(payload)
        return self.display()

    def delete_credential(self, provider_id: str) -> dict[str, Any]:
        payload = self._payload()
        provider = payload["providers"].get(provider_id)
        if not isinstance(provider, dict):
            raise ValueError(f"Unknown web-search provider: {provider_id}")
        reference = str(provider.get("credential_ref") or "")
        if reference:
            self.credentials.delete(reference)
        provider.update({"credential_ref": "", "enabled": False, "state": "disabled", "last_error": ""})
        self._save(payload)
        return self.display()

    def prepare(self, provider_id: str) -> dict[str, Any]:
        if provider_id not in PROVIDER_IDS:
            raise ValueError(f"Unknown web-search provider: {provider_id}")
        return {
            "provider_id": provider_id,
            "status": "already_satisfied",
            "packages": PROVIDER_MANIFEST[provider_id]["required_packages"],
        }

    def mark_test(self, provider_id: str, *, success: bool, latency_ms: int, error: str = "") -> None:
        payload = self._payload()
        provider = payload["providers"][provider_id]
        provider["last_test"] = {"success": success, "latency_ms": latency_ms, "tested_at": int(time.time())}
        provider["last_error"] = error
        provider["state"] = "ready" if success else "error"
        if not success:
            provider["enabled"] = False
        self._save(payload)

    def mark_auth_failure(self, provider_id: str, error: str) -> None:
        """Disable a credential rejected at runtime without deleting it."""
        payload = self._payload()
        provider = payload["providers"][provider_id]
        provider["enabled"] = False
        provider["state"] = "needs_test"
        provider["last_error"] = error
        self._save(payload)

    def set_enabled(self, provider_id: str, enabled: bool) -> dict[str, Any]:
        payload = self._payload()
        provider = payload["providers"].get(provider_id)
        if not isinstance(provider, dict):
            raise ValueError(f"Unknown web-search provider: {provider_id}")
        if enabled and provider.get("state") != "ready":
            raise ValueError("请先单独测试连接，通过后再启用供应商")
        provider["enabled"] = enabled
        self._save(payload)
        return self.display()

    def update_routing(self, update: dict[str, Any]) -> dict[str, Any]:
        payload = self._payload()
        if "default_scope" in update:
            scope = str(update["default_scope"])
            if scope not in {"domestic", "global"}:
                raise ValueError("default_scope 必须是 domestic 或 global")
            payload["default_scope"] = scope
        routing = payload["routing"]
        for key in ("domestic", "global"):
            if key not in update:
                continue
            order = [str(item) for item in update[key]]
            if set(order) != set(PROVIDER_IDS) or len(order) != len(PROVIDER_IDS):
                raise ValueError(f"{key} 路由必须且只能包含 tavily、deepseek、grok")
            routing[key] = order
        for key in ("fallback_enabled", "cross_check_enabled"):
            if key in update:
                routing[key] = bool(update[key])
        if "max_provider_attempts" in update:
            routing["max_provider_attempts"] = max(1, min(int(update["max_provider_attempts"]), 3))
        self._save(payload)
        return self.display()

    def update_provider_options(self, provider_id: str, options: dict[str, Any]) -> dict[str, Any]:
        payload = self._payload()
        provider = payload["providers"].get(provider_id)
        if not isinstance(provider, dict):
            raise ValueError(f"Unknown web-search provider: {provider_id}")
        allowed = {"max_results"}
        if provider_id == "tavily":
            allowed.add("search_depth")
        if provider_id == "grok":
            allowed.update({"web_search_enabled", "x_search_enabled"})
        for key, value in options.items():
            if key not in allowed:
                continue
            if key == "max_results":
                value = max(1, min(int(value), 10))
            if key == "search_depth" and value not in {"basic", "advanced", "fast", "ultra-fast"}:
                raise ValueError("Unsupported Tavily search_depth")
            provider["options"][key] = value
        self._save(payload)
        return self.display()


_default_registry: WebSearchRegistry | None = None


def get_web_search_registry() -> WebSearchRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = WebSearchRegistry()
    return _default_registry
