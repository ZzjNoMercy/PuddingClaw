"""Direct model provider registry and local credential store.

The registry is deliberately a small local control plane: application code
resolves one explicit binding (provider + endpoint + model + credential ref)
before sending a request.  It never falls back to a gateway or another
provider at runtime.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from llm.thinking_mapping import thinking_profile

REGISTRY_VERSION = 1
DEFAULT_NATIVE_MM_PATH = "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"
DASHSCOPE_NATIVE_DISCOVERY_TIMEOUT_SECONDS = 3.0
DASHSCOPE_NATIVE_DISCOVERY_CACHE_TTL_SECONDS = 300.0
DASHSCOPE_NATIVE_MODEL_CATALOG = (
    "qwen3-vl-embedding",
    "qwen2.5-vl-embedding",
    "tongyi-embedding-vision-plus-2026-03-06",
    "tongyi-embedding-vision-flash-2026-03-06",
    "tongyi-embedding-vision-plus",
    "tongyi-embedding-vision-flash",
    "qwen3-vl-rerank",
    "qwen3-rerank",
    "gte-rerank-v2",
)
MODEL_CATEGORY_CAPABILITIES = {
    "llm": "llm",
    "multimodal_llm": "llm",
    "text_embedding": "text_embedding",
    "multimodal_embedding": "multimodal_embedding",
    "rerank": "rerank",
}
DEFAULT_MODEL_CATEGORY = {
    "llm": "llm",
    "text_embedding": "text_embedding",
    "multimodal_embedding": "multimodal_embedding",
    "rerank": "rerank",
}

logger = logging.getLogger(__name__)


def user_data_dir() -> Path:
    """Return the OS user-data directory, with Electron taking precedence."""
    configured = os.getenv("PUDDINGDATA_USER_DATA_DIR") or os.getenv("PUDDINGCLAW_USER_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        return Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming") / "PuddingData"
    if os.getenv("XDG_CONFIG_HOME"):
        return Path(os.environ["XDG_CONFIG_HOME"]) / "PuddingData"
    if os.sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "PuddingData"
    return Path.home() / ".config" / "PuddingData"


def _atomic_json_write(path: Path, payload: dict[str, Any], *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    fd, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if os.name != "nt":
                os.fchmod(handle.fileno(), mode)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        if os.name != "nt":
            os.chmod(path, mode)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else copy.deepcopy(default)
    except (OSError, ValueError, json.JSONDecodeError):
        return copy.deepcopy(default)


def _slug(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-") or "model"


def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "•" * len(secret)
    return f"{secret[:4]}{'•' * max(4, len(secret) - 8)}{secret[-4:]}"


class LocalCredentialStore:
    """Phase-one, file-backed credential store outside the repository.

    Values are deliberately not encrypted in phase one.  Directory and file
    permissions, atomic writes, and opaque references avoid accidental
    leakage into project files, API responses, and logs.  Phase two can keep
    these references and replace this implementation with OS keyring calls.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or user_data_dir()
        self.path = self.root / "credentials.json"

    def _payload(self) -> dict[str, Any]:
        return _read_json(self.path, {"version": 1, "credentials": {}})

    def put(self, ref: str, value: str) -> str:
        if not value:
            return ref
        payload = self._payload()
        credentials = payload.setdefault("credentials", {})
        credentials[ref] = {"value": value, "updated_at": int(time.time())}
        _atomic_json_write(self.path, payload, mode=0o600)
        return f"local-file://{ref}"

    def get(self, reference: str) -> str:
        if not reference:
            return ""
        if reference.startswith("env://"):
            return os.getenv(reference.removeprefix("env://"), "")
        if not reference.startswith("local-file://"):
            return ""
        item = self._payload().get("credentials", {}).get(reference.removeprefix("local-file://"), {})
        return str(item.get("value") or "") if isinstance(item, dict) else ""

    def delete(self, ref: str) -> None:
        key = ref.removeprefix("local-file://")
        payload = self._payload()
        credentials = payload.setdefault("credentials", {})
        if key in credentials:
            del credentials[key]
            _atomic_json_write(self.path, payload, mode=0o600)

    def display(self, reference: str) -> str:
        return _mask(self.get(reference))

    def updated_at(self, reference: str) -> int:
        if not reference.startswith("local-file://"):
            return 0
        item = self._payload().get("credentials", {}).get(reference.removeprefix("local-file://"), {})
        return int(item.get("updated_at") or 0) if isinstance(item, dict) else 0


def _provider_presets() -> list[dict[str, Any]]:
    return [
        {"id": "deepseek", "name": "DeepSeek", "enabled": True, "website": "https://platform.deepseek.com", "endpoints": [{"id": "deepseek-openai", "protocol": "deepseek", "base_url": "https://api.deepseek.com", "credential_ref": "", "capabilities": ["llm"]}], "models": []},
        {"id": "dashscope", "name": "阿里云百炼", "enabled": True, "website": "https://bailian.console.aliyun.com", "credential_scope": "provider", "endpoints": [{"id": "dashscope-compatible", "protocol": "openai_compatible", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "credential_ref": "", "capabilities": ["llm", "text_embedding"]}, {"id": "dashscope-native-mm", "protocol": "dashscope_multimodal_embedding", "base_url": "https://dashscope.aliyuncs.com", "route_path": DEFAULT_NATIVE_MM_PATH, "credential_ref": "", "capabilities": ["multimodal_embedding", "rerank"]}], "models": []},
        {"id": "kimi", "name": "Kimi", "enabled": False, "website": "https://platform.moonshot.cn", "endpoints": [{"id": "kimi-openai", "protocol": "openai_compatible", "base_url": "https://api.moonshot.cn/v1", "credential_ref": "", "capabilities": ["llm"]}], "models": []},
        {"id": "siliconflow", "name": "硅基流动", "enabled": False, "website": "https://siliconflow.cn", "endpoints": [{"id": "siliconflow-openai", "protocol": "openai_compatible", "base_url": "https://api.siliconflow.cn/v1", "credential_ref": "", "capabilities": ["llm", "text_embedding"]}], "models": []},
    ]


def _default_registry() -> dict[str, Any]:
    return {"version": REGISTRY_VERSION, "providers": _provider_presets(), "bindings": {}, "migration": {"state": "not_started"}}


class ProviderRegistry:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or user_data_dir()
        self.path = self.root / "providers.json"
        self.credentials = LocalCredentialStore(self.root)
        self._dashscope_native_discovery_cache: dict[
            tuple[str, str, int], tuple[float, list[dict[str, Any]]]
        ] = {}

    def _payload(self) -> dict[str, Any]:
        payload = _read_json(self.path, _default_registry())
        payload.setdefault("providers", _provider_presets())
        payload.setdefault("bindings", {})
        payload.setdefault("migration", {"state": "not_started"})
        return payload

    def _save(self, payload: dict[str, Any]) -> None:
        _atomic_json_write(self.path, payload, mode=0o600)
        # Endpoint URLs and credentials may have changed. Never serve discovery
        # results cached against registry state that has just been replaced.
        self._dashscope_native_discovery_cache.clear()

    @staticmethod
    def _backfill_bindings(payload: dict[str, Any]) -> bool:
        """Add newly introduced workload bindings without changing user choices."""
        bindings = payload.setdefault("bindings", {})
        agent_model_id = bindings.get("agent")
        if agent_model_id and not bindings.get("image_analyzer"):
            # Existing installations initially share the assistant model. The
            # user can select a vision-capable LLM independently afterwards.
            bindings["image_analyzer"] = agent_model_id
            return True
        return False

    @staticmethod
    def _backfill_model_categories(payload: dict[str, Any]) -> bool:
        """Give pre-category registry models a neutral capability-based category."""
        changed = False
        for provider in payload.get("providers", []):
            for model in provider.get("models", []):
                capability = str(model.get("capability") or "")
                raw_categories = model.get("categories")
                categories = [
                    str(category)
                    for category in raw_categories
                    if isinstance(category, str)
                    and MODEL_CATEGORY_CAPABILITIES.get(category) == capability
                ] if isinstance(raw_categories, list) else []
                if not categories and DEFAULT_MODEL_CATEGORY.get(capability):
                    categories = [DEFAULT_MODEL_CATEGORY[capability]]
                if categories != raw_categories:
                    model["categories"] = categories
                    changed = True
        return changed

    @staticmethod
    def _provider(payload: dict[str, Any], provider_id: str) -> dict[str, Any]:
        for provider in payload["providers"]:
            if provider.get("id") == provider_id:
                return provider
        raise ValueError(f"Unknown provider: {provider_id}")

    @staticmethod
    def _endpoint(provider: dict[str, Any], endpoint_id: str) -> dict[str, Any]:
        for endpoint in provider.get("endpoints", []):
            if endpoint.get("id") == endpoint_id:
                return endpoint
        raise ValueError(f"Unknown endpoint: {endpoint_id}")

    def _put_legacy_credential(self, name: str, value: str, *, env_name: str = "") -> str:
        if value:
            return self.credentials.put(name, value)
        return f"env://{env_name}" if env_name else ""

    def _set_endpoint_credential(self, provider: dict[str, Any], endpoint_id: str, reference: str) -> None:
        if not reference:
            return
        endpoint = self._endpoint(provider, endpoint_id)
        current = str(endpoint.get("credential_ref") or "")
        # Legacy re-import runs again whenever config.json regains a legacy
        # api_key (e.g. saved via the old settings UI).  Migration owns
        # legacy-* and env:// references, but a provider-scoped local-file
        # reference is an explicit user edit from the Provider page and must
        # never be overwritten by a re-import.
        if current.startswith("local-file://") and not current.startswith("local-file://legacy-"):
            return
        endpoint["credential_ref"] = reference

    def _normalize_shared_provider_credentials(self, payload: dict[str, Any]) -> bool:
        """Make provider-scoped credentials identical across all endpoints."""
        changed = False
        for provider in payload.get("providers", []):
            if provider.get("id") == "dashscope" and provider.get("credential_scope") != "provider":
                provider["credential_scope"] = "provider"
                changed = True
            if provider.get("credential_scope") != "provider":
                continue
            candidates = [
                (endpoint, str(endpoint.get("credential_ref") or ""))
                for endpoint in provider.get("endpoints", [])
                if endpoint.get("credential_ref")
            ]
            if not candidates:
                continue
            usable = [candidate for candidate in candidates if self.credentials.get(candidate[1])]
            pool = usable or candidates

            def priority(candidate: tuple[dict[str, Any], str]) -> tuple[int, int, int]:
                endpoint, reference = candidate
                explicit_local = reference.startswith("local-file://") and not reference.startswith("local-file://legacy-")
                compatible_endpoint = endpoint.get("id") == "dashscope-compatible"
                return int(explicit_local), int(compatible_endpoint), self.credentials.updated_at(reference)

            selected_reference = max(pool, key=priority)[1]
            for endpoint in provider.get("endpoints", []):
                if endpoint.get("credential_ref") != selected_reference:
                    endpoint["credential_ref"] = selected_reference
                    changed = True
        return changed

    def ensure_migrated(self, legacy_config: dict[str, Any]) -> None:
        """Import effective legacy direct/Higress values once without deleting sources."""
        payload = self._payload()
        bindings_backfilled = self._backfill_bindings(payload)
        categories_backfilled = self._backfill_model_categories(payload)
        shared_credentials_backfilled = self._normalize_shared_provider_credentials(payload)
        migration_complete = payload.get("migration", {}).get("state") == "complete"
        legacy_secret_present = any(
            isinstance(item, dict) and bool(item.get("api_key"))
            for item in (
                legacy_config.get("fallback_llm"),
                legacy_config.get("fallback_embedding"),
                legacy_config.get("multimodal_embedding"),
                (legacy_config.get("rag", {}) or {}).get("rerank"),
                (legacy_config.get("vanna", {}) or {}).get("llm"),
                (legacy_config.get("vanna", {}) or {}).get("embedding"),
            )
        )
        if migration_complete and not legacy_secret_present:
            if bindings_backfilled or categories_backfilled or shared_credentials_backfilled:
                self._save(payload)
            return
        deepseek = self._provider(payload, "deepseek")
        dashscope = self._provider(payload, "dashscope")
        llm = legacy_config.get("fallback_llm", {}) or {}
        embedding = legacy_config.get("fallback_embedding", {}) or {}
        multimodal = legacy_config.get("multimodal_embedding", {}) or {}
        rerank = (legacy_config.get("rag", {}) or {}).get("rerank", {}) or {}

        llm_value = str(llm.get("api_key") or "")
        llm_ref = self._put_legacy_credential("legacy-deepseek", llm_value, env_name="DEEPSEEK_API_KEY") if llm_value or not migration_complete else ""
        self._set_endpoint_credential(deepseek, "deepseek-openai", llm_ref)
        embedding_value = str(embedding.get("api_key") or "")
        embedding_ref = self._put_legacy_credential("legacy-dashscope-text", embedding_value, env_name="OPENAI_API_KEY") if embedding_value or not migration_complete else ""
        self._set_endpoint_credential(dashscope, "dashscope-compatible", embedding_ref)

        mm_value = str(multimodal.get("api_key") or "")
        if not mm_value and not migration_complete:
            # One-time legacy importer only. It is intentionally never used by
            # a request path after registry migration.
            try:
                from higress_config_reader import get_higress_dashscope_api_key
                mm_value = get_higress_dashscope_api_key()
            except Exception:
                mm_value = ""
        mm_ref = self._put_legacy_credential("legacy-dashscope-multimodal", mm_value, env_name="DASHSCOPE_API_KEY") if mm_value or not migration_complete else ""
        self._set_endpoint_credential(dashscope, "dashscope-native-mm", mm_ref)
        if not embedding_ref and mm_ref:
            self._set_endpoint_credential(dashscope, "dashscope-compatible", mm_ref)

        def add_model(provider: dict[str, Any], endpoint_id: str, name: str, capability: str, **extra: Any) -> str:
            model_id = f"{provider['id']}:{endpoint_id}:{_slug(name)}:{capability}"
            existing = next((item for item in provider["models"] if item.get("id") == model_id), None)
            item = {"id": model_id, "name": name, "endpoint_id": endpoint_id, "capability": capability, **extra}
            if existing is None:
                provider["models"].append(item)
            else:
                existing.update(item)
            return model_id

        llm_provider = str(llm.get("provider") or "deepseek").lower()
        active_llm_provider = deepseek if llm_provider == "deepseek" else dashscope
        active_llm_endpoint = "deepseek-openai" if active_llm_provider is deepseek else "dashscope-compatible"
        if active_llm_provider is not deepseek:
            self._endpoint(active_llm_provider, active_llm_endpoint)["base_url"] = str(llm.get("base_url") or self._endpoint(active_llm_provider, active_llm_endpoint)["base_url"])
            self._set_endpoint_credential(active_llm_provider, active_llm_endpoint, llm_ref)
        llm_id = add_model(active_llm_provider, active_llm_endpoint, str(llm.get("model") or "deepseek-chat"), "llm", categories=["llm"], temperature=llm.get("temperature", 0.7), max_tokens=llm.get("max_tokens", 4096), context_window=llm.get("context_window", 1000000), thinking=llm.get("thinking", {}))
        text_id = add_model(dashscope, "dashscope-compatible", str(embedding.get("model") or "text-embedding-v4"), "text_embedding", categories=["text_embedding"], dimension=int(embedding.get("dimension") or 1024), batch_size=int(embedding.get("batch_size") or 10))
        mm_id = add_model(dashscope, "dashscope-native-mm", str(multimodal.get("model") or "qwen3-vl-embedding"), "multimodal_embedding", categories=["multimodal_embedding"], dimension=int(multimodal.get("dimension") or 1024), batch_size=int(multimodal.get("batch_size") or 10), route_path=str(multimodal.get("route_path") or DEFAULT_NATIVE_MM_PATH))
        rerank_id = add_model(dashscope, "dashscope-native-mm", str(rerank.get("model") or "qwen3-vl-rerank"), "rerank", categories=["rerank"], top_n=int(rerank.get("top_n") or 5), candidate_top_k=int(rerank.get("candidate_top_k") or 20), route_path="/api/v1/services/rerank/text-rerank/text-rerank")
        bindings = payload["bindings"]
        bindings.setdefault("agent", llm_id)
        bindings.setdefault("image_analyzer", bindings["agent"])
        bindings.setdefault("text_embedding", text_id)
        bindings.setdefault("multimodal_embedding", mm_id)
        bindings.setdefault("rerank", rerank_id)
        bindings.setdefault("vanna_llm", llm_id)
        bindings.setdefault("vanna_embedding", text_id)
        payload["migration"] = {"state": "complete", "completed_at": int(time.time()), "sources": ["config.json", "environment references", "higress data (read-only import)"], "legacy_gateway": {"ai_gateway": legacy_config.get("ai_gateway", {}), "gateway_llm": legacy_config.get("gateway_llm", {})}}
        self._normalize_shared_provider_credentials(payload)
        self._save(payload)

    def resolve_binding(self, binding: str, *, legacy_config: dict[str, Any]) -> dict[str, Any]:
        self.ensure_migrated(legacy_config)
        payload = self._payload()
        model_id = payload["bindings"].get(binding)
        if not model_id:
            raise ValueError(f"No model bound for {binding}")
        for provider in payload["providers"]:
            for model in provider.get("models", []):
                if model.get("id") == model_id:
                    endpoint = self._endpoint(provider, str(model["endpoint_id"]))
                    if model.get("capability") not in endpoint.get("capabilities", []):
                        raise ValueError(f"Model {model_id} is incompatible with endpoint {endpoint['id']}")
                    return self._resolved_model(provider, endpoint, model, binding=binding)
        raise ValueError(f"Bound model not found: {model_id}")

    def resolve_model(
        self,
        model_id: str,
        *,
        legacy_config: dict[str, Any],
        expected_capability: str = "llm",
    ) -> dict[str, Any]:
        """Resolve an explicit registered model without switching providers."""

        self.ensure_migrated(legacy_config)
        payload = self._payload()
        for provider in payload["providers"]:
            for model in provider.get("models", []):
                if model.get("id") != model_id:
                    continue
                endpoint = self._endpoint(provider, str(model["endpoint_id"]))
                if model.get("capability") != expected_capability:
                    raise ValueError(
                        f"The selected model must have capability {expected_capability}"
                    )
                if model.get("capability") not in endpoint.get("capabilities", []):
                    raise ValueError(f"Model {model_id} is incompatible with endpoint {endpoint['id']}")
                if not self.credentials.get(str(endpoint.get("credential_ref") or "")):
                    raise ValueError(f"Provider {provider['name']} has no configured credential")
                return self._resolved_model(provider, endpoint, model)
        raise ValueError(f"Unknown model: {model_id}")

    def _resolved_model(
        self,
        provider: dict[str, Any],
        endpoint: dict[str, Any],
        model: dict[str, Any],
        *,
        binding: str = "",
    ) -> dict[str, Any]:
        return {
            "binding": binding,
            "provider_id": provider["id"],
            "provider_name": provider["name"],
            "endpoint_id": endpoint["id"],
            "protocol": endpoint["protocol"],
            "base_url": endpoint["base_url"],
            "route_path": model.get("route_path") or endpoint.get("route_path") or "",
            "credential_ref": endpoint.get("credential_ref", ""),
            "api_key": self.credentials.get(str(endpoint.get("credential_ref") or "")),
            "thinking_profile": thinking_profile(
                provider_id=str(provider.get("id") or ""),
                model_name=str(model.get("name") or ""),
                endpoint_id=str(endpoint.get("id") or ""),
            ),
            **copy.deepcopy(model),
        }

    def display(self, *, legacy_config: dict[str, Any]) -> dict[str, Any]:
        self.ensure_migrated(legacy_config)
        payload = self._payload()
        result = copy.deepcopy(payload)
        for provider in result["providers"]:
            for endpoint in provider.get("endpoints", []):
                reference = str(endpoint.pop("credential_ref", ""))
                endpoint["credential_configured"] = bool(self.credentials.get(reference))
                endpoint["api_key_masked"] = self.credentials.display(reference)
                endpoint["credential_source"] = "environment" if reference.startswith("env://") else "local_file" if reference else ""
            for model in provider.get("models", []):
                model["thinking_profile"] = thinking_profile(
                    provider_id=str(provider.get("id") or ""),
                    model_name=str(model.get("name") or ""),
                    endpoint_id=str(model.get("endpoint_id") or ""),
                )
        return result

    def update_provider(
        self,
        provider_id: str,
        update: dict[str, Any],
        *,
        legacy_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Migration may create the initial registry and environment references.
        # It must finish before applying an explicit user edit; otherwise the
        # display call below could migrate afterwards and overwrite a newly
        # saved local credential with a legacy environment reference.
        self.ensure_migrated(legacy_config or {})
        payload = self._payload()
        provider = self._provider(payload, provider_id)
        for key in ("name", "enabled"):
            if key in update:
                provider[key] = update[key]
        endpoint_updates = update.get("endpoints", [])
        if not isinstance(endpoint_updates, list):
            raise ValueError("endpoints must be a list")
        for update_endpoint in endpoint_updates:
            if not isinstance(update_endpoint, dict):
                continue
            endpoint = self._endpoint(provider, str(update_endpoint.get("id") or ""))
            for key in ("base_url", "route_path"):
                if key in update_endpoint:
                    endpoint[key] = str(update_endpoint[key]).strip()
            if update_endpoint.get("api_key"):
                if provider.get("credential_scope") == "provider":
                    reference = self.credentials.put(f"{provider_id}-shared", str(update_endpoint["api_key"]))
                    for provider_endpoint in provider.get("endpoints", []):
                        provider_endpoint["credential_ref"] = reference
                else:
                    endpoint["credential_ref"] = self.credentials.put(f"{provider_id}-{endpoint['id']}", str(update_endpoint["api_key"]))
        self._save(payload)
        return self.display(legacy_config={})

    def set_binding(self, binding: str, model_id: str) -> None:
        payload = self._payload()
        model = next((model for provider in payload["providers"] for model in provider.get("models", []) if model.get("id") == model_id), None)
        if not model:
            raise ValueError("Unknown model")
        expected = {"agent": "llm", "image_analyzer": "llm", "text_embedding": "text_embedding", "multimodal_embedding": "multimodal_embedding", "rerank": "rerank", "vanna_llm": "llm", "vanna_embedding": "text_embedding"}.get(binding)
        if expected and model.get("capability") != expected:
            raise ValueError(f"{binding} requires a {expected} model")
        payload["bindings"][binding] = model_id
        self._save(payload)

    def upsert_model(self, provider_id: str, model: dict[str, Any]) -> dict[str, Any]:
        payload = self._payload()
        provider = self._provider(payload, provider_id)
        endpoint_id = str(model.get("endpoint_id") or "").strip()
        capability = str(model.get("capability") or "").strip()
        name = str(model.get("name") or "").strip()
        if not endpoint_id or not capability or not name:
            raise ValueError("endpoint_id, capability and name are required")
        endpoint = self._endpoint(provider, endpoint_id)
        if capability not in endpoint.get("capabilities", []):
            raise ValueError(f"Endpoint {endpoint_id} does not support {capability}")
        model_id = str(model.get("id") or f"{provider_id}:{endpoint_id}:{_slug(name)}:{capability}")
        existing = next((entry for entry in provider["models"] if entry.get("id") == model_id), None)
        raw_categories = model.get("categories")
        if raw_categories is None and existing:
            raw_categories = existing.get("categories")
        if raw_categories is None:
            raw_categories = [DEFAULT_MODEL_CATEGORY.get(capability, capability)]
        if not isinstance(raw_categories, list):
            raise ValueError("categories must be a list")
        categories = list(dict.fromkeys(str(category).strip() for category in raw_categories if str(category).strip()))
        if not categories:
            raise ValueError("at least one model category is required")
        invalid = [
            category
            for category in categories
            if MODEL_CATEGORY_CAPABILITIES.get(category) != capability
        ]
        if invalid:
            raise ValueError(f"categories are incompatible with {capability}: {', '.join(invalid)}")
        item = {"id": model_id, "name": name, "endpoint_id": endpoint_id, "capability": capability, "categories": categories}
        for key in ("dimension", "batch_size", "concurrency", "temperature", "max_tokens", "context_window", "route_path"):
            if key in model:
                item[key] = model[key]
        if existing:
            existing.update(item)
        else:
            provider["models"].append(item)
        self._save(payload)
        return item

    def discover_models(self, provider_id: str, endpoint_id: str) -> list[dict[str, Any]]:
        payload = self._payload()
        provider = self._provider(payload, provider_id)
        endpoint = self._endpoint(provider, endpoint_id)
        protocol = str(endpoint.get("protocol") or "")
        api_key = self.credentials.get(str(endpoint.get("credential_ref") or ""))
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

        if protocol == "dashscope_multimodal_embedding":
            credential_ref = str(endpoint.get("credential_ref") or "")
            cache_key = (
                str(endpoint.get("base_url") or "").strip().rstrip("/"),
                credential_ref,
                self.credentials.updated_at(credential_ref),
            )
            now = time.monotonic()
            cached = self._dashscope_native_discovery_cache.get(cache_key)
            if cached and cached[0] > now:
                return copy.deepcopy(cached[1])

            models = self._discover_dashscope_native_models(endpoint, headers)
            self._dashscope_native_discovery_cache[cache_key] = (
                now + DASHSCOPE_NATIVE_DISCOVERY_CACHE_TTL_SECONDS,
                copy.deepcopy(models),
            )
            return models
        if protocol not in {"deepseek", "openai_compatible"}:
            raise ValueError("This endpoint has no supported model-list API.")

        with httpx.Client(timeout=15.0) as client:
            response = client.get(f"{str(endpoint['base_url']).rstrip('/')}/models", headers=headers)
            response.raise_for_status()
        raw_models = response.json().get("data", [])
        return [{"id": str(item.get("id")), "name": str(item.get("id")), "owned_by": item.get("owned_by", "")} for item in raw_models if isinstance(item, dict) and item.get("id")]

    @staticmethod
    def _discover_dashscope_native_models(endpoint: dict[str, Any], headers: dict[str, str]) -> list[dict[str, Any]]:
        """Discover models for DashScope-only embedding and rerank routes.

        DashScope does not expose an OpenAI-style inference ``/models`` route
        for these native APIs.  Its authenticated deployment catalog is the
        closest remote catalog, but it does not contain every serverless native
        model.  Merge compatible entries from that catalog with the small
        official native inference catalog so models such as
        ``qwen3-vl-embedding`` remain selectable.
        """
        base_url = str(endpoint.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            raise ValueError("Endpoint base URL is required")
        catalog_url = (
            f"{base_url}/deployments/models"
            if base_url.endswith("/api/v1")
            else f"{base_url}/api/v1/deployments/models"
        )
        discovered: dict[str, dict[str, Any]] = {
            name: {"id": name, "name": name, "owned_by": "dashscope"}
            for name in DASHSCOPE_NATIVE_MODEL_CATALOG
        }

        # Without a credential the deployment catalog cannot add anything to
        # the built-in native catalog. Returning immediately also keeps initial
        # provider setup responsive.
        if not headers:
            return list(discovered.values())

        page_no = 1
        page_size = 100
        started_at = time.monotonic()
        deadline = started_at + DASHSCOPE_NATIVE_DISCOVERY_TIMEOUT_SECONDS
        try:
            with httpx.Client(timeout=DASHSCOPE_NATIVE_DISCOVERY_TIMEOUT_SECONDS) as client:
                while page_no <= 20:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    separator = "&" if "?" in catalog_url else "?"
                    response = client.get(
                        f"{catalog_url}{separator}page_no={page_no}&page_size={page_size}&version=v1.0&model_source=base",
                        headers=headers,
                        timeout=remaining,
                    )
                    response.raise_for_status()
                    body = response.json()
                    output = body.get("output", body) if isinstance(body, dict) else {}
                    raw_models = output.get("models", []) if isinstance(output, dict) else []
                    if not isinstance(raw_models, list):
                        raw_models = []

                    for item in raw_models:
                        if not isinstance(item, dict):
                            continue
                        name = str(item.get("model_name") or item.get("name") or item.get("id") or "").strip()
                        lowered = name.lower()
                        is_native_embedding = "embedding" in lowered and any(
                            marker in lowered for marker in ("vl", "vision", "image", "video", "multimodal")
                        )
                        if name and (is_native_embedding or "rerank" in lowered):
                            discovered.setdefault(name, {"id": name, "name": name, "owned_by": "dashscope"})

                    total = int(output.get("total") or 0) if isinstance(output, dict) else 0
                    if not raw_models or len(raw_models) < page_size or (total and page_no * page_size >= total):
                        break
                    page_no += 1
        except httpx.TransportError as exc:
            # The remote deployment catalog is optional enrichment. Keep the
            # picker usable when DashScope is slow or temporarily unreachable.
            logger.warning(
                "DashScope native model discovery timed out or failed after %.0fms; using %d built-in/partial models: %s",
                (time.monotonic() - started_at) * 1000,
                len(discovered),
                exc,
            )

        return list(discovered.values())

    def test_endpoint(
        self,
        provider_id: str,
        endpoint_id: str,
        *,
        base_url: str = "",
        api_key: str = "",
    ) -> dict[str, Any]:
        """Validate a Provider endpoint without returning or registering models.

        OpenAI-compatible APIs have no portable health endpoint. ``GET /models``
        is the lowest-cost authenticated probe shared by these providers; its
        response body is intentionally discarded here. Model discovery remains
        a separate explicit user action.
        """
        payload = self._payload()
        provider = self._provider(payload, provider_id)
        endpoint = self._endpoint(provider, endpoint_id)
        if endpoint.get("protocol") not in {"deepseek", "openai_compatible"}:
            raise ValueError("This endpoint has no standard authenticated connectivity probe")

        target_base_url = (base_url or str(endpoint.get("base_url") or "")).strip().rstrip("/")
        if not target_base_url:
            raise ValueError("Endpoint base URL is required")
        resolved_key = api_key or self.credentials.get(str(endpoint.get("credential_ref") or ""))
        headers = {"Authorization": f"Bearer {resolved_key}"} if resolved_key else {}

        with httpx.Client(timeout=10.0, trust_env=False) as client:
            response = client.get(f"{target_base_url}/models", headers=headers)
            response.raise_for_status()
        return {"reachable": True, "status_code": response.status_code}


_default_registry_instance: ProviderRegistry | None = None


def get_provider_registry() -> ProviderRegistry:
    global _default_registry_instance
    if _default_registry_instance is None:
        _default_registry_instance = ProviderRegistry()
    return _default_registry_instance
