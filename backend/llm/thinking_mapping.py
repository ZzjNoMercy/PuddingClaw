"""Provider/model specific reasoning controls.

The UI deals in a small normalized vocabulary.  This module is the single
translation table from that vocabulary to the request fields accepted by each
provider.  Keep provider quirks out of the composer and the Agent runtime.
"""

from __future__ import annotations

from typing import Any

THINKING_LEVELS = {"low", "high", "max"}


def normalize_model_temperature(
    *,
    provider_id: str,
    model_name: str,
    temperature: float,
) -> float:
    """Apply hard Provider sampling constraints before constructing a client.

    Kimi K3 rejects every temperature except ``1``.  Keep this rule next to
    the model-specific reasoning map so migrated registry defaults and caller
    overrides cannot produce an invalid request.
    """

    provider = provider_id.strip().lower()
    model = model_name.strip().lower()
    if provider == "kimi" and ("kimi-k3" in model or "kimi/kimi-k3" in model):
        return 1.0
    return float(temperature)


def thinking_profile(*, provider_id: str, model_name: str, endpoint_id: str = "") -> dict[str, Any]:
    """Return the normalized reasoning UI profile for one registered model."""

    provider = provider_id.strip().lower()
    model = model_name.strip().lower()
    endpoint = endpoint_id.strip().lower()

    if model.startswith("qwen3.7"):
        return {
            "kind": "qwen_fixed",
            "thinking_enabled": True,
            "strength_control": "disabled",
            "levels": [],
            "default_level": None,
            "disabled_label": "默认",
        }

    if "kimi-k3" in model or "kimi/kimi-k3" in model:
        # Bailian currently exposes Kimi K3 with max reasoning only.  Direct
        # Moonshot/Kimi supports the three documented effort levels.
        if provider == "dashscope" or endpoint.startswith("dashscope-"):
            return {
                "kind": "kimi_bailian_fixed",
                "thinking_enabled": True,
                "strength_control": "disabled",
                "levels": [],
                "default_level": "max",
                "disabled_label": "最大",
            }
        return {
            "kind": "kimi_levels",
            "thinking_enabled": True,
            "strength_control": "levels",
            "levels": ["low", "high", "max"],
            "default_level": "low",
            "disabled_label": "",
        }

    if provider == "deepseek" or model.startswith("deepseek-"):
        return {
            "kind": "deepseek_levels",
            "thinking_enabled": True,
            "strength_control": "levels",
            "levels": ["high", "max"],
            "default_level": "high",
            "disabled_label": "",
        }

    return {
        "kind": "none",
        "thinking_enabled": False,
        "strength_control": "hidden",
        "levels": [],
        "default_level": None,
        "disabled_label": "不支持",
    }


def map_thinking_request(profile: dict[str, Any], level: str | None) -> dict[str, Any]:
    """Map a normalized strength to provider request kwargs.

    A fixed profile ignores a missing UI level but still emits the provider's
    required "thinking on" request.  Selectable profiles reject unsupported
    levels instead of silently degrading to another strength.
    """

    kind = str(profile.get("kind") or "none")
    normalized = str(level or profile.get("default_level") or "").strip().lower() or None

    if kind == "qwen_fixed":
        return {
            "thinking_enabled": True,
            "thinking_level": None,
            "reasoning_effort": None,
            "extra_body": {"enable_thinking": True},
        }
    if kind == "kimi_bailian_fixed":
        return {
            "thinking_enabled": True,
            "thinking_level": "max",
            "reasoning_effort": "max",
            "extra_body": None,
        }
    if kind == "kimi_levels":
        if normalized not in {"low", "high", "max"}:
            raise ValueError("Kimi K3 推理强度仅支持：低、高、最大")
        return {
            "thinking_enabled": True,
            "thinking_level": normalized,
            "reasoning_effort": normalized,
            "extra_body": None,
        }
    if kind == "deepseek_levels":
        if normalized not in {"high", "max"}:
            raise ValueError("DeepSeek 推理强度仅支持：高、最大")
        return {
            "thinking_enabled": True,
            "thinking_level": normalized,
            "reasoning_effort": normalized,
            "extra_body": {"thinking": {"type": "enabled"}},
        }
    if level:
        raise ValueError("当前模型不支持推理强度设置")
    return {
        "thinking_enabled": False,
        "thinking_level": None,
        "reasoning_effort": None,
        "extra_body": None,
    }
