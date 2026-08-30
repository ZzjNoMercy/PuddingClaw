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
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from llm.thinking_mapping import thinking_profile
from runtime_identity.paths import PuddingClawPaths, trusted_owner_user_id
from runtime_identity.profiles import (
    CredentialAuthorityLockedError,
    CredentialEnvelopeDecryptionError,
    CredentialVault,
    MasterKeyProvider,
)

REGISTRY_VERSION = 2
DEFAULT_CREDENTIAL_NAME = "default"
CREDENTIAL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEFAULT_NATIVE_MM_PATH = "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"
DEFAULT_AGENT_MODEL = {
    "provider": "deepseek",
    "model": "deepseek-v4-flash",
    "base_url": "https://api.deepseek.com",
    "temperature": 0.7,
    "max_tokens": 4096,
    "context_window": 1000000,
    "thinking": {
        "model": "deepseek-v4-flash",
        "reasoning_effort": "high",
        "extra_body": {"thinking": {"type": "enabled"}},
    },
}
DEFAULT_TEXT_EMBEDDING_MODEL = {
    "provider": "qwen",
    "model": "text-embedding-v4",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "dimension": 1024,
    "batch_size": 10,
}
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


class CredentialVaultDecryptionError(ValueError):
    """The encrypted Provider registry cannot be opened by local keys."""


def user_data_dir() -> Path:
    """Return the canonical PuddingClaw configuration directory."""
    return PuddingClawPaths.from_environment().config()


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


def _read_json_bytes(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else {"version": 1, "credentials": {}}
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return {"version": 1, "credentials": {}}


def _atomic_bytes_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            if os.name != "nt":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(raw_path, path)
    finally:
        if os.path.exists(raw_path):
            os.unlink(raw_path)


def _slug(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-") or "model"


def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "•" * len(secret)
    return f"{secret[:4]}{'•' * max(4, len(secret) - 8)}{secret[-4:]}"


class LocalCredentialStore:
    """Owner-scoped encrypted Credential Vault compatibility adapter."""

    def __init__(self, root: Path | None = None, *, owner_user_id: str | None = None) -> None:
        configured_root = (root or user_data_dir()).expanduser().resolve()
        self.root = configured_root
        self.owner_user_id = owner_user_id or trusted_owner_user_id()
        home_root = configured_root.parent if configured_root.name == "config" else configured_root
        self.paths = PuddingClawPaths(home_root)
        self.path = self.paths.credentials_root(self.owner_user_id) / "provider-registry.enc"
        self.legacy_paths = (configured_root / "credentials.json", home_root / "credentials.json")
        self.key_provider = MasterKeyProvider(self.paths, self.owner_user_id)
        self._vault: CredentialVault | None = None
        self._unreadable_envelope: tuple[int, int, str] | None = None

    @property
    def vault(self) -> CredentialVault:
        if self._vault is None:
            self._vault = CredentialVault(self.key_provider.get_or_create())
        return self._vault

    @vault.setter
    def vault(self, value: CredentialVault) -> None:
        self._vault = value

    def _open_payload(self) -> dict[str, Any]:
        """Open the registry with the installation's sole credential authority."""

        stat = self.path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        if self._unreadable_envelope and self._unreadable_envelope[:2] == signature:
            raise CredentialVaultDecryptionError(self._unreadable_envelope[2]) from None
        envelope = self.path.read_bytes()
        try:
            raw = self.vault.open(
                envelope,
                owner_user_id=self.owner_user_id,
                provider="provider-registry",
                profile_id="default",
            )
        except CredentialAuthorityLockedError:
            raise
        except (CredentialEnvelopeDecryptionError, ValueError, KeyError, TypeError, UnicodeDecodeError):
            pass
        else:
            self._unreadable_envelope = None
            return _read_json_bytes(raw)
        error = CredentialVaultDecryptionError(
            "Credential Vault 无法解密已保存的 Provider API Key；主密钥与密文不匹配。"
            "请在 Settings > 模型服务重新保存所有 Provider API Key。"
        )
        self._unreadable_envelope = (*signature, str(error))
        raise error from None

    def _payload(self) -> dict[str, Any]:
        if self.path.is_file():
            return self._open_payload()
        for legacy in self.legacy_paths:
            if not legacy.is_file():
                continue
            payload = _read_json(legacy, {"version": 1, "credentials": {}})
            self._write_payload(payload)
            # A successful authenticated read-back is the safety boundary for
            # removing the old plaintext source.
            if self._payload_from_disk().get("credentials") == payload.get("credentials"):
                legacy.unlink(missing_ok=True)
            return payload
        return {"version": 1, "credentials": {}}

    def _payload_from_disk(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        return self._open_payload()

    def _write_payload(self, payload: dict[str, Any]) -> None:
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        envelope = self.vault.seal(
            encoded,
            owner_user_id=self.owner_user_id,
            provider="provider-registry",
            profile_id="default",
        )
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_bytes_write(self.path, envelope)
        self._unreadable_envelope = None

    def _quarantine_unreadable_payload(self) -> Path | None:
        """Preserve unreadable ciphertext before an explicit credential save repairs the vault."""

        if not self.path.is_file():
            return None
        backup = self.path.with_name(f"{self.path.name}.unreadable-{time.time_ns()}")
        self.path.replace(backup)
        logger.warning(
            "Credential Vault key mismatch; preserved unreadable Provider registry at %s before re-keying",
            backup,
        )
        return backup

    def put(self, ref: str, value: str) -> str:
        if not value:
            return ref
        try:
            payload = self._payload()
        except CredentialVaultDecryptionError:
            # A credential save is an explicit repair action.  Keep the old
            # ciphertext for forensic/manual recovery, then create a fresh
            # registry with the installation's stable master key.  No read
            # path is allowed to reset credentials implicitly.
            self._quarantine_unreadable_payload()
            payload = {"version": 1, "credentials": {}}
        credentials = payload.setdefault("credentials", {})
        credentials[ref] = {"value": value, "updated_at": int(time.time())}
        self._write_payload(payload)
        return f"vault://users/{self.owner_user_id}/credentials/{ref}"

    def get(self, reference: str) -> str:
        if not reference:
            return ""
        if reference.startswith("env://"):
            return os.getenv(reference.removeprefix("env://"), "")
        if not reference.startswith("vault://"):
            return ""
        item = self._payload().get("credentials", {}).get(reference.rsplit("/", 1)[-1], {})
        return str(item.get("value") or "") if isinstance(item, dict) else ""

    def delete(self, ref: str) -> None:
        key = ref.rsplit("/", 1)[-1]
        try:
            payload = self._payload()
        except CredentialVaultDecryptionError:
            # Deleting/clearing a credential is also an explicit repair
            # action.  Preserve the unreadable ciphertext and let the caller
            # remove its non-secret reference instead of trapping the user on
            # a settings page that cannot clear the broken configuration.
            self._quarantine_unreadable_payload()
            return
        credentials = payload.setdefault("credentials", {})
        if key in credentials:
            del credentials[key]
            self._write_payload(payload)

    def display(self, reference: str) -> str:
        return str(self.inspect(reference)["api_key_masked"])

    def inspect(self, reference: str) -> dict[str, Any]:
        """Return control-plane credential status without propagating vault failures.

        Runtime callers must continue to use :meth:`get`.  Settings pages use
        this method so an unreadable vault can be repaired from the UI instead
        of making the repair screen itself unavailable.
        """

        try:
            value = self.get(reference)
        except (CredentialVaultDecryptionError, CredentialAuthorityLockedError) as exc:
            configured = bool(reference.startswith("vault://") and self.path.is_file())
            return {
                "credential_configured": configured,
                "credential_readable": False,
                "api_key_masked": "••••••••" if configured else "",
                "credential_error": str(exc),
            }
        return {
            "credential_configured": bool(value),
            "credential_readable": True,
            "api_key_masked": _mask(value),
            "credential_error": "",
        }

    def updated_at(self, reference: str) -> int:
        if not reference.startswith("vault://"):
            return 0
        item = self._payload().get("credentials", {}).get(reference.rsplit("/", 1)[-1], {})
        return int(item.get("updated_at") or 0) if isinstance(item, dict) else 0


def _provider_presets() -> list[dict[str, Any]]:
    return [
        {
            "id": "deepseek",
            "name": "DeepSeek",
            "enabled": True,
            "website": "https://platform.deepseek.com",
            "credentials": {DEFAULT_CREDENTIAL_NAME: "env://DEEPSEEK_API_KEY"},
            "endpoints": [{"id": "deepseek-openai", "protocol": "deepseek", "base_url": "https://api.deepseek.com", "credential_ref": "env://DEEPSEEK_API_KEY", "capabilities": ["llm"]}],
            "models": [
                {
                    "id": "deepseek:deepseek-openai:deepseek-v4-flash:llm",
                    "name": "deepseek-v4-flash",
                    "endpoint_id": "deepseek-openai",
                    "capability": "llm",
                    "categories": ["llm"],
                    "temperature": 0.7,
                    "max_tokens": 4096,
                    "context_window": 1000000,
                    "thinking": copy.deepcopy(DEFAULT_AGENT_MODEL["thinking"]),
                },
                {
                    "id": "deepseek:deepseek-openai:deepseek-v4-pro:llm",
                    "name": "deepseek-v4-pro",
                    "endpoint_id": "deepseek-openai",
                    "capability": "llm",
                    "categories": ["llm"],
                    "temperature": 0.7,
                    "max_tokens": 8192,
                    "context_window": 1000000,
                    "thinking": {
                        "model": "deepseek-v4-pro",
                        "reasoning_effort": "high",
                        "extra_body": {"thinking": {"type": "enabled"}},
                    },
                },
            ],
        },
        {
            "id": "dashscope",
            "name": "阿里云百炼",
            "enabled": True,
            "website": "https://bailian.console.aliyun.com",
            "credential_scope": "provider",
            "credentials": {DEFAULT_CREDENTIAL_NAME: "env://DASHSCOPE_API_KEY"},
            "endpoints": [
                {"id": "dashscope-compatible", "protocol": "openai_compatible", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "credential_ref": "env://DASHSCOPE_API_KEY", "capabilities": ["llm", "text_embedding"]},
                {"id": "dashscope-native-mm", "protocol": "dashscope_multimodal_embedding", "base_url": "https://dashscope.aliyuncs.com", "route_path": DEFAULT_NATIVE_MM_PATH, "credential_ref": "env://DASHSCOPE_API_KEY", "capabilities": ["multimodal_embedding", "rerank"]},
            ],
            "models": [
                {"id": "dashscope:dashscope-compatible:text-embedding-v4:text_embedding", "name": "text-embedding-v4", "endpoint_id": "dashscope-compatible", "capability": "text_embedding", "categories": ["text_embedding"], "dimension": 1024, "batch_size": 10},
                {"id": "dashscope:dashscope-native-mm:qwen3-vl-embedding:multimodal_embedding", "name": "qwen3-vl-embedding", "endpoint_id": "dashscope-native-mm", "capability": "multimodal_embedding", "categories": ["multimodal_embedding"], "dimension": 1024, "batch_size": 10, "route_path": DEFAULT_NATIVE_MM_PATH},
                {"id": "dashscope:dashscope-native-mm:qwen3-vl-rerank:rerank", "name": "qwen3-vl-rerank", "endpoint_id": "dashscope-native-mm", "capability": "rerank", "categories": ["rerank"], "top_n": 10, "candidate_top_k": 50, "route_path": "/api/v1/services/rerank/text-rerank/text-rerank"},
                {"id": "dashscope:dashscope-compatible:qwen3-7-plus:llm", "name": "qwen3.7-plus", "endpoint_id": "dashscope-compatible", "capability": "llm", "categories": ["llm", "multimodal_llm"]},
            ],
        },
        {
            "id": "zhipu",
            "name": "智谱 AI",
            "enabled": False,
            "website": "https://open.bigmodel.cn",
            "credential_scope": "provider",
            "credentials": {DEFAULT_CREDENTIAL_NAME: "env://ZAI_API_KEY"},
            "endpoints": [
                {
                    "id": "zhipu-openai",
                    "protocol": "openai_compatible",
                    "base_url": "https://open.bigmodel.cn/api/paas/v4",
                    "credential_ref": "env://ZAI_API_KEY",
                    "capabilities": ["llm", "text_embedding"],
                }
            ],
            "models": [
                {
                    "id": "zhipu:zhipu-openai:glm-5-3:llm",
                    "name": "glm-5.3",
                    "endpoint_id": "zhipu-openai",
                    "capability": "llm",
                    "categories": ["llm"],
                    "temperature": 1.0,
                    "max_tokens": 131072,
                    "context_window": 1000000,
                },
                {
                    "id": "zhipu:zhipu-openai:glm-5-3-flash:llm",
                    "name": "glm-5.3-flash",
                    "endpoint_id": "zhipu-openai",
                    "capability": "llm",
                    "categories": ["llm", "multimodal_llm"],
                    "temperature": 1.0,
                    "max_tokens": 131072,
                    "context_window": 1000000,
                },
                {
                    "id": "zhipu:zhipu-openai:embedding-3:text_embedding",
                    "name": "embedding-3",
                    "endpoint_id": "zhipu-openai",
                    "capability": "text_embedding",
                    "categories": ["text_embedding"],
                    "dimension": 2048,
                    "batch_size": 64,
                    "context_window": 8192,
                },
            ],
        },
        {"id": "kimi", "name": "Kimi", "enabled": False, "website": "https://platform.moonshot.cn", "credentials": {}, "endpoints": [{"id": "kimi-openai", "protocol": "openai_compatible", "base_url": "https://api.moonshot.cn/v1", "credential_ref": "", "capabilities": ["llm"]}], "models": []},
        {"id": "siliconflow", "name": "硅基流动", "enabled": False, "website": "https://siliconflow.cn", "credentials": {}, "endpoints": [{"id": "siliconflow-openai", "protocol": "openai_compatible", "base_url": "https://api.siliconflow.cn/v1", "credential_ref": "", "capabilities": ["llm", "text_embedding"]}], "models": []},
    ]


def _backfill_missing_provider_presets(payload: dict[str, Any]) -> bool:
    """Add newly shipped Provider presets without disturbing user edits."""

    providers = payload.get("providers", [])
    if not isinstance(providers, list):
        return False
    existing_ids = {
        str(provider.get("id") or "")
        for provider in providers
        if isinstance(provider, dict)
    }
    missing = [
        copy.deepcopy(provider)
        for provider in _provider_presets()
        if str(provider.get("id") or "") not in existing_ids
    ]
    if not missing:
        return False
    providers.extend(missing)
    return True


def _bootstrap_provider_binding(
    payload: dict[str, Any],
    initial: dict[str, Any] | None,
    *,
    binding: str,
    credential_ref: str,
    multimodal: bool = False,
) -> None:
    if not isinstance(initial, dict) or initial.get("status") not in {"configured", "needs_action"}:
        return
    provider_id = _slug(str(initial.get("id") or initial.get("name") or "provider"))
    provider_name = str(initial.get("name") or provider_id)
    base_url = str(initial.get("base_url") or "").rstrip("/")
    model_name = str(initial.get("model") or "").strip()
    if not base_url or not model_name:
        return
    configured_provider = next(
        (provider for provider in payload["providers"] if provider.get("id") == provider_id),
        None,
    )
    if configured_provider is None:
        endpoint_id = f"{provider_id}-openai"
        configured_provider = {
            "id": provider_id,
            "name": provider_name,
            "enabled": True,
            "website": base_url,
            "credentials": {DEFAULT_CREDENTIAL_NAME: credential_ref},
            "endpoints": [{
                "id": endpoint_id,
                "protocol": str(initial.get("protocol") or "openai_compatible"),
                "base_url": base_url,
                "credential_ref": credential_ref,
                "capabilities": ["llm"],
            }],
            "models": [],
        }
        payload["providers"].insert(0, configured_provider)
    else:
        configured_provider["enabled"] = True
        configured_provider["credentials"] = {
            **configured_provider.get("credentials", {}),
            DEFAULT_CREDENTIAL_NAME: credential_ref,
        }
        llm_endpoints = [
            endpoint
            for endpoint in configured_provider.get("endpoints", [])
            if "llm" in endpoint.get("capabilities", [])
        ]
        if not llm_endpoints:
            endpoint_id = f"{provider_id}-openai"
            llm_endpoints = [{
                "id": endpoint_id,
                "protocol": str(initial.get("protocol") or "openai_compatible"),
                "base_url": base_url,
                "credential_ref": credential_ref,
                "capabilities": ["llm"],
            }]
            configured_provider.setdefault("endpoints", []).extend(llm_endpoints)
        endpoint = llm_endpoints[0]
        endpoint_id = str(endpoint["id"])
        endpoint["protocol"] = str(initial.get("protocol") or endpoint.get("protocol") or "openai_compatible")
        endpoint["base_url"] = base_url
        endpoint["credential_ref"] = credential_ref
        if configured_provider.get("credential_scope") == "provider":
            for item in configured_provider.get("endpoints", []):
                item["credential_ref"] = credential_ref

    models = configured_provider.setdefault("models", [])
    configured_model = next(
        (
            model
            for model in models
            if model.get("endpoint_id") == endpoint_id
            and model.get("capability") == "llm"
            and model.get("name") == model_name
        ),
        None,
    )
    if configured_model is None:
        model_id = f"{provider_id}:{endpoint_id}:{_slug(model_name)}:llm"
        configured_model = {
            "id": model_id,
            "name": model_name,
            "endpoint_id": endpoint_id,
            "capability": "llm",
            "categories": ["llm", "multimodal_llm"] if multimodal else ["llm"],
            "temperature": 0.7,
            "max_tokens": 4096,
        }
        models.append(configured_model)
    elif multimodal:
        categories = list(configured_model.get("categories") or ["llm"])
        if "multimodal_llm" not in categories:
            categories.append("multimodal_llm")
        configured_model["categories"] = categories
    payload["bindings"][binding] = str(configured_model["id"])


def _environment_provider(name: str) -> dict[str, Any] | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _bootstrap_multimodal_binding(payload: dict[str, Any]) -> dict[str, Any]:
    initial = _environment_provider("PUDDINGCLAW_INITIAL_MULTIMODAL_PROVIDER")
    credential_ref = (
        "env://PUDDINGCLAW_INITIAL_PROVIDER_API_KEY"
        if initial and initial.get("reuse_primary_credential") is True
        else "env://PUDDINGCLAW_INITIAL_MULTIMODAL_PROVIDER_API_KEY"
    )
    _bootstrap_provider_binding(
        payload,
        initial,
        binding="image_analyzer",
        credential_ref=credential_ref,
        multimodal=True,
    )
    return payload


def _default_registry() -> dict[str, Any]:
    payload = {
        "version": REGISTRY_VERSION,
        "providers": _provider_presets(),
        "bindings": {
            "agent": "deepseek:deepseek-openai:deepseek-v4-flash:llm",
            "image_analyzer": "dashscope:dashscope-compatible:qwen3-7-plus:llm",
            "text_embedding": "dashscope:dashscope-compatible:text-embedding-v4:text_embedding",
            "multimodal_embedding": "dashscope:dashscope-native-mm:qwen3-vl-embedding:multimodal_embedding",
            "rerank": "dashscope:dashscope-native-mm:qwen3-vl-rerank:rerank",
            "vanna_llm": "deepseek:deepseek-openai:deepseek-v4-pro:llm",
            "vanna_embedding": "dashscope:dashscope-compatible:text-embedding-v4:text_embedding",
        },
    }
    raw = os.getenv("PUDDINGCLAW_INITIAL_PROVIDER", "").strip()
    if not raw:
        return _bootstrap_multimodal_binding(payload)
    try:
        initial = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _bootstrap_multimodal_binding(payload)
    if not isinstance(initial, dict) or initial.get("status") not in {"configured", "needs_action"}:
        return _bootstrap_multimodal_binding(payload)
    provider_id = _slug(str(initial.get("id") or initial.get("name") or "provider"))
    provider_name = str(initial.get("name") or provider_id)
    base_url = str(initial.get("base_url") or "").rstrip("/")
    model_name = str(initial.get("model") or "").strip()
    if not base_url or not model_name:
        return _bootstrap_multimodal_binding(payload)
    credential_ref = "env://PUDDINGCLAW_INITIAL_PROVIDER_API_KEY"
    configured_provider = next(
        (provider for provider in payload["providers"] if provider.get("id") == provider_id),
        None,
    )
    if configured_provider is None:
        endpoint_id = f"{provider_id}-openai"
        configured_provider = {
            "id": provider_id,
            "name": provider_name,
            "enabled": True,
            "website": base_url,
            "credentials": {DEFAULT_CREDENTIAL_NAME: credential_ref},
            "endpoints": [{
                "id": endpoint_id,
                "protocol": str(initial.get("protocol") or "openai_compatible"),
                "base_url": base_url,
                "credential_ref": credential_ref,
                "capabilities": ["llm"],
            }],
            "models": [],
        }
        payload["providers"].insert(0, configured_provider)
    else:
        configured_provider["enabled"] = True
        configured_provider["credentials"] = {
            **configured_provider.get("credentials", {}),
            DEFAULT_CREDENTIAL_NAME: credential_ref,
        }
        llm_endpoints = [
            endpoint
            for endpoint in configured_provider.get("endpoints", [])
            if "llm" in endpoint.get("capabilities", [])
        ]
        if not llm_endpoints:
            endpoint_id = f"{provider_id}-openai"
            llm_endpoints = [{
                "id": endpoint_id,
                "protocol": str(initial.get("protocol") or "openai_compatible"),
                "base_url": base_url,
                "credential_ref": credential_ref,
                "capabilities": ["llm"],
            }]
            configured_provider.setdefault("endpoints", []).extend(llm_endpoints)
        endpoint = llm_endpoints[0]
        endpoint_id = str(endpoint["id"])
        endpoint["protocol"] = str(initial.get("protocol") or endpoint.get("protocol") or "openai_compatible")
        endpoint["base_url"] = base_url
        endpoint["credential_ref"] = credential_ref
        if configured_provider.get("credential_scope") == "provider":
            for item in configured_provider.get("endpoints", []):
                item["credential_ref"] = credential_ref

    models = configured_provider.setdefault("models", [])
    configured_model = next(
        (
            model
            for model in models
            if model.get("endpoint_id") == endpoint_id
            and model.get("capability") == "llm"
            and model.get("name") == model_name
        ),
        None,
    )
    if configured_model is None:
        model_id = f"{provider_id}:{endpoint_id}:{_slug(model_name)}:llm"
        configured_model = {
            "id": model_id,
            "name": model_name,
            "endpoint_id": endpoint_id,
            "capability": "llm",
            "categories": ["llm"],
            "temperature": 0.7,
            "max_tokens": 4096,
        }
        models.append(configured_model)
    model_id = str(configured_model["id"])
    payload["bindings"]["agent"] = model_id
    return _bootstrap_multimodal_binding(payload)


def _migrate_legacy_initial_provider(payload: dict[str, Any]) -> bool:
    """Fold the short-lived initial-* bootstrap shape into its built-in preset."""
    providers = payload.get("providers", [])
    bindings = payload.get("bindings", {})
    agent_model_id = str(bindings.get("agent") or "")
    for legacy in list(providers):
        legacy_id = str(legacy.get("id") or "") if isinstance(legacy, dict) else ""
        if not legacy_id.startswith("initial-") or not agent_model_id.startswith(f"{legacy_id}:"):
            continue
        provider_id = legacy_id.removeprefix("initial-")
        target = next(
            (
                provider
                for provider in providers
                if isinstance(provider, dict) and provider.get("id") == provider_id
            ),
            None,
        )
        if target is None:
            continue
        selected_model = next(
            (
                model
                for model in legacy.get("models", [])
                if isinstance(model, dict) and model.get("id") == agent_model_id
            ),
            None,
        )
        if selected_model is None:
            continue
        source_endpoint = next(
            (
                endpoint
                for endpoint in legacy.get("endpoints", [])
                if isinstance(endpoint, dict)
                and endpoint.get("id") == selected_model.get("endpoint_id")
            ),
            None,
        )
        target_endpoint = next(
            (
                endpoint
                for endpoint in target.get("endpoints", [])
                if isinstance(endpoint, dict) and "llm" in endpoint.get("capabilities", [])
            ),
            None,
        )
        if source_endpoint is None or target_endpoint is None:
            continue
        credential_ref = str(
            legacy.get("credentials", {}).get(DEFAULT_CREDENTIAL_NAME)
            or source_endpoint.get("credential_ref")
            or ""
        )
        target["enabled"] = True
        if credential_ref:
            target["credentials"] = {
                **target.get("credentials", {}),
                DEFAULT_CREDENTIAL_NAME: credential_ref,
            }
        for field in ("protocol", "base_url"):
            if source_endpoint.get(field):
                target_endpoint[field] = source_endpoint[field]
        if credential_ref:
            target_endpoint["credential_ref"] = credential_ref
            if target.get("credential_scope") == "provider":
                for endpoint in target.get("endpoints", []):
                    endpoint["credential_ref"] = credential_ref
        model_name = str(selected_model.get("name") or "").strip()
        target_model = next(
            (
                model
                for model in target.get("models", [])
                if isinstance(model, dict)
                and model.get("endpoint_id") == target_endpoint.get("id")
                and model.get("capability") == "llm"
                and model.get("name") == model_name
            ),
            None,
        )
        if target_model is None:
            target_model = {
                **selected_model,
                "id": f"{provider_id}:{target_endpoint['id']}:{_slug(model_name)}:llm",
                "endpoint_id": target_endpoint["id"],
            }
            target.setdefault("models", []).append(target_model)
        bindings["agent"] = target_model["id"]
        providers.remove(legacy)
        return True
    return False


class ProviderRegistry:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or user_data_dir()
        self.path = self.root / "providers.json"
        self.bootstrap_marker_path = self.root / ".cli-provider-bootstrap.json"
        self.credentials = LocalCredentialStore(self.root)
        self._dashscope_native_discovery_cache: dict[
            tuple[str, str, int], tuple[float, list[dict[str, Any]]]
        ] = {}

    def _payload(self) -> dict[str, Any]:
        if not self.path.is_file():
            payload = _default_registry()
            self._mark_environment_bootstrap()
            return payload
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid Provider Registry JSON: {self.path}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Provider Registry must be a JSON object")
        unknown = sorted(set(payload) - {"version", "providers", "bindings"})
        if unknown:
            raise ValueError(f"Unsupported Provider Registry fields: {', '.join(unknown)}")
        if payload.get("version") != REGISTRY_VERSION:
            raise ValueError(f"Unsupported Provider Registry version: {payload.get('version')}")
        if not isinstance(payload.get("providers"), list) or not isinstance(payload.get("bindings"), dict):
            raise ValueError("Provider Registry requires providers and bindings")
        migrated = _migrate_legacy_initial_provider(payload)
        presets_backfilled = _backfill_missing_provider_presets(payload)
        bootstrapped = self._apply_environment_bootstrap_once(payload)
        model_ids = {
            str(model.get("id") or "")
            for provider in payload["providers"]
            if isinstance(provider, dict)
            for model in provider.get("models", [])
            if isinstance(model, dict)
        }
        required_bindings = set(_default_registry()["bindings"])
        missing_bindings = sorted(required_bindings - set(payload["bindings"]))
        if missing_bindings:
            raise ValueError(f"Provider Registry is missing bindings: {', '.join(missing_bindings)}")
        dangling_bindings = sorted(
            binding
            for binding, model_id in payload["bindings"].items()
            if str(model_id) not in model_ids
        )
        if dangling_bindings:
            raise ValueError(f"Provider Registry has unknown bound models: {', '.join(dangling_bindings)}")
        if migrated or presets_backfilled or bootstrapped:
            self._save(payload)
        if bootstrapped:
            self._mark_environment_bootstrap()
        return payload

    def _bootstrap_id(self) -> str:
        return os.getenv("PUDDINGCLAW_INITIAL_PROVIDER_BOOTSTRAP_ID", "").strip()

    def _mark_environment_bootstrap(self) -> None:
        bootstrap_id = self._bootstrap_id()
        if bootstrap_id:
            _atomic_json_write(
                self.bootstrap_marker_path,
                {"version": 1, "bootstrap_id": bootstrap_id},
                mode=0o600,
            )

    def _apply_environment_bootstrap_once(self, payload: dict[str, Any]) -> bool:
        bootstrap_id = self._bootstrap_id()
        if not bootstrap_id:
            return False
        marker = _read_json(self.bootstrap_marker_path, {})
        if marker.get("bootstrap_id") == bootstrap_id:
            return False
        _bootstrap_provider_binding(
            payload,
            _environment_provider("PUDDINGCLAW_INITIAL_PROVIDER"),
            binding="agent",
            credential_ref="env://PUDDINGCLAW_INITIAL_PROVIDER_API_KEY",
        )
        _bootstrap_multimodal_binding(payload)
        return True

    def _save(self, payload: dict[str, Any]) -> None:
        payload["version"] = REGISTRY_VERSION
        _atomic_json_write(self.path, payload, mode=0o600)
        # Endpoint URLs and credentials may have changed. Never serve discovery
        # results cached against registry state that has just been replaced.
        self._dashscope_native_discovery_cache.clear()

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

    @staticmethod
    def _normalize_credential_name(value: Any) -> str:
        name = str(value or "").strip()
        if not CREDENTIAL_NAME_PATTERN.fullmatch(name):
            raise ValueError(
                "Credential name must be 1-64 characters using letters, numbers, '.', '_' or '-'"
            )
        return name

    def _credential_reference(
        self,
        provider: dict[str, Any],
        endpoint: dict[str, Any],
        credential_name: str | None = None,
    ) -> tuple[str, str]:
        name = self._normalize_credential_name(credential_name or DEFAULT_CREDENTIAL_NAME)
        credentials = provider.get("credentials")
        reference = str(credentials.get(name) or "") if isinstance(credentials, dict) else ""
        if not reference and name == DEFAULT_CREDENTIAL_NAME:
            reference = str(endpoint.get("credential_ref") or "")
        if credential_name and not reference:
            raise ValueError(f"本地未保存 {provider['name']} 的 API Key：{name}")
        return name, reference

    def resolve_binding(
        self,
        binding: str,
        *,
        credential_name: str | None = None,
    ) -> dict[str, Any]:
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
                    if credential_name:
                        selected_name, reference = self._credential_reference(
                            provider, endpoint, credential_name
                        )
                        if not self.credentials.get(reference):
                            raise ValueError(
                                f"本地未保存 {provider['name']} 的 API Key：{selected_name}"
                            )
                    return self._resolved_model(
                        provider,
                        endpoint,
                        model,
                        binding=binding,
                        credential_name=credential_name,
                    )
        raise ValueError(f"Bound model not found: {model_id}")

    def resolve_model(
        self,
        model_id: str,
        *,
        expected_capability: str = "llm",
        credential_name: str | None = None,
    ) -> dict[str, Any]:
        """Resolve an explicit registered model without switching providers."""

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
                selected_name, reference = self._credential_reference(provider, endpoint, credential_name)
                if not self.credentials.get(reference):
                    raise ValueError(f"本地未保存 {provider['name']} 的 API Key：{selected_name}")
                return self._resolved_model(
                    provider,
                    endpoint,
                    model,
                    credential_name=selected_name,
                )
        raise ValueError(f"Unknown model: {model_id}")

    def _resolved_model(
        self,
        provider: dict[str, Any],
        endpoint: dict[str, Any],
        model: dict[str, Any],
        *,
        binding: str = "",
        credential_name: str | None = None,
    ) -> dict[str, Any]:
        selected_name, reference = self._credential_reference(provider, endpoint, credential_name)
        return {
            "binding": binding,
            "provider_id": provider["id"],
            "provider_name": provider["name"],
            "endpoint_id": endpoint["id"],
            "protocol": endpoint["protocol"],
            "base_url": endpoint["base_url"],
            "route_path": model.get("route_path") or endpoint.get("route_path") or "",
            "credential_ref": reference,
            "credential_name": selected_name,
            "api_key": self.credentials.get(reference),
            "thinking_profile": thinking_profile(
                provider_id=str(provider.get("id") or ""),
                model_name=str(model.get("name") or ""),
                endpoint_id=str(endpoint.get("id") or ""),
            ),
            **copy.deepcopy(model),
        }

    def display(self) -> dict[str, Any]:
        payload = self._payload()
        result = copy.deepcopy(payload)
        vault_error = ""
        inspections: dict[str, dict[str, Any]] = {}

        def inspect(reference: str) -> dict[str, Any]:
            nonlocal vault_error
            if reference not in inspections:
                inspections[reference] = self.credentials.inspect(reference)
            status = inspections[reference]
            if not status["credential_readable"] and not vault_error:
                vault_error = str(status["credential_error"])
            return status

        for provider in result["providers"]:
            raw_credentials = provider.pop("credentials", {})
            provider["default_credential_name"] = DEFAULT_CREDENTIAL_NAME
            provider["api_keys"] = []
            names = set(raw_credentials) if isinstance(raw_credentials, dict) else set()
            names.add(DEFAULT_CREDENTIAL_NAME)
            for name in sorted(names, key=lambda item: (item != DEFAULT_CREDENTIAL_NAME, item.lower())):
                reference = str(raw_credentials.get(name) or "") if isinstance(raw_credentials, dict) else ""
                status = inspect(reference)
                provider["api_keys"].append({
                    "name": name,
                    "is_default": name == DEFAULT_CREDENTIAL_NAME,
                    **status,
                    "credential_source": (
                        "environment" if reference.startswith("env://")
                        else "local_file" if reference.startswith("vault://")
                        else "" if not reference else "legacy"
                    ),
                })
            for endpoint in provider.get("endpoints", []):
                reference = str(raw_credentials.get(DEFAULT_CREDENTIAL_NAME) or endpoint.pop("credential_ref", ""))
                endpoint.pop("credential_ref", None)
                endpoint.update(inspect(reference))
                endpoint["credential_source"] = (
                    "environment" if reference.startswith("env://")
                    else "local_file" if reference.startswith("vault://")
                    else "" if not reference else "legacy"
                )
            for model in provider.get("models", []):
                model["thinking_profile"] = thinking_profile(
                    provider_id=str(provider.get("id") or ""),
                    model_name=str(model.get("name") or ""),
                    endpoint_id=str(model.get("endpoint_id") or ""),
                )
        result["credential_vault"] = {
            "readable": not vault_error,
            "error": vault_error,
        }
        return result

    def resolve_credential_for_runtime(
        self,
        provider_id: str,
        credential_name: str,
    ) -> str:
        """Resolve one credential for an internal provider request only.

        This method is intentionally named as a runtime resolver rather than a
        reveal operation; callers must not expose its return value to HTTP/UI
        responses, logs, prompts, or tool results.
        """
        payload = self._payload()
        provider = self._provider(payload, provider_id)
        endpoints = provider.get("endpoints", [])
        if not endpoints:
            raise ValueError(f"Provider {provider['name']} has no endpoint")
        name, reference = self._credential_reference(
            provider,
            endpoints[0],
            credential_name,
        )
        value = self.credentials.get(reference)
        if not value:
            raise ValueError(f"本地未保存 {provider['name']} 的 API Key：{name}")
        return value

    def update_provider(
        self,
        provider_id: str,
        update: dict[str, Any],
    ) -> dict[str, Any]:
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
                ref_name = (
                    f"{provider_id}-shared"
                    if provider.get("credential_scope") == "provider"
                    else f"{provider_id}-{endpoint['id']}"
                )
                reference = self.credentials.put(ref_name, str(update_endpoint["api_key"]))
                provider.setdefault("credentials", {})[DEFAULT_CREDENTIAL_NAME] = reference
                for provider_endpoint in provider.get("endpoints", []):
                    provider_endpoint["credential_ref"] = reference
        credential_updates = update.get("credentials", [])
        if not isinstance(credential_updates, list):
            raise ValueError("credentials must be a list")
        credentials = provider.setdefault("credentials", {})
        normalized_updates: list[tuple[str, str]] = []
        for credential in credential_updates:
            if not isinstance(credential, dict):
                continue
            name = self._normalize_credential_name(credential.get("name"))
            value = str(credential.get("value") or "").strip()
            if not value:
                raise ValueError(f"Credential {name} value is required")
            normalized_updates.append((name, value))
        if (
            normalized_updates
            and not credentials.get(DEFAULT_CREDENTIAL_NAME)
            and all(name != DEFAULT_CREDENTIAL_NAME for name, _ in normalized_updates)
        ):
            raise ValueError("Configure the default credential before adding named credentials")
        for name, value in normalized_updates:
            reference = self.credentials.put(f"{provider_id}-credential-{name}", value)
            credentials[name] = reference
            if name == DEFAULT_CREDENTIAL_NAME:
                for provider_endpoint in provider.get("endpoints", []):
                    provider_endpoint["credential_ref"] = reference
        self._save(payload)
        return self.display()

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
        credential_name: str | None = None,
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
        if api_key:
            # A name is only a local alias. While adding a new key there is no
            # saved alias yet, so test the explicitly entered value directly.
            resolved_key = api_key
        else:
            _, credential_ref = self._credential_reference(provider, endpoint, credential_name)
            resolved_key = self.credentials.get(credential_ref)
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
