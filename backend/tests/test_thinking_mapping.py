from __future__ import annotations

import pytest

from llm.thinking_mapping import (
    map_thinking_request,
    normalize_model_temperature,
    thinking_profile,
)


def test_qwen_37_has_fixed_thinking_without_strength_control() -> None:
    profile = thinking_profile(
        provider_id="dashscope",
        model_name="qwen3.7-plus",
        endpoint_id="dashscope-compatible",
    )

    assert profile["strength_control"] == "disabled"
    assert profile["default_level"] is None
    assert map_thinking_request(profile, None) == {
        "thinking_enabled": True,
        "thinking_level": None,
        "reasoning_effort": None,
        "extra_body": {"enable_thinking": True},
    }


@pytest.mark.parametrize(
    ("level", "effort"),
    [("high", "high"), ("max", "max")],
)
def test_deepseek_strength_maps_to_reasoning_effort(level: str, effort: str) -> None:
    profile = thinking_profile(
        provider_id="deepseek",
        model_name="deepseek-v4-pro",
        endpoint_id="deepseek-openai",
    )

    mapped = map_thinking_request(profile, level)

    assert profile["levels"] == ["high", "max"]
    assert mapped["reasoning_effort"] == effort
    assert mapped["extra_body"] == {"thinking": {"type": "enabled"}}


@pytest.mark.parametrize("level", ["low", "high", "max"])
def test_direct_kimi_k3_exposes_three_strengths(level: str) -> None:
    profile = thinking_profile(
        provider_id="kimi",
        model_name="kimi-k3",
        endpoint_id="kimi-openai",
    )

    assert profile["levels"] == ["low", "high", "max"]
    assert profile["default_level"] == "low"
    assert map_thinking_request(profile, level)["reasoning_effort"] == level


def test_direct_kimi_k3_defaults_to_low_strength() -> None:
    profile = thinking_profile(
        provider_id="kimi",
        model_name="kimi-k3",
        endpoint_id="kimi-openai",
    )

    mapped = map_thinking_request(profile, None)

    assert mapped["thinking_level"] == "low"
    assert mapped["reasoning_effort"] == "low"


@pytest.mark.parametrize("model_name", ["glm-5.3", "glm-5.3-flash"])
@pytest.mark.parametrize("level", ["low", "high", "max"])
def test_zhipu_glm_53_exposes_documented_reasoning_strengths(
    model_name: str,
    level: str,
) -> None:
    profile = thinking_profile(
        provider_id="zhipu",
        model_name=model_name,
        endpoint_id="zhipu-openai",
    )

    mapped = map_thinking_request(profile, level)

    assert profile["levels"] == ["low", "high", "max"]
    assert profile["default_level"] == "max"
    assert mapped["reasoning_effort"] == level
    assert mapped["extra_body"] == {"thinking": {"type": "enabled"}}


def test_bailian_kimi_k3_is_fixed_to_max() -> None:
    profile = thinking_profile(
        provider_id="dashscope",
        model_name="kimi/kimi-k3",
        endpoint_id="dashscope-compatible",
    )

    assert profile["strength_control"] == "disabled"
    assert map_thinking_request(profile, None)["reasoning_effort"] == "max"


def test_invalid_deepseek_strength_is_rejected() -> None:
    profile = thinking_profile(
        provider_id="deepseek",
        model_name="deepseek-v4-pro",
        endpoint_id="deepseek-openai",
    )

    with pytest.raises(ValueError, match="DeepSeek"):
        map_thinking_request(profile, "low")


@pytest.mark.parametrize("configured", [0.0, 0.2, 0.7, 1.0, 1.5])
def test_direct_kimi_k3_temperature_is_fixed_to_one(configured: float) -> None:
    assert normalize_model_temperature(
        provider_id="kimi",
        model_name="kimi-k3",
        temperature=configured,
    ) == 1.0


def test_other_models_keep_configured_temperature() -> None:
    assert normalize_model_temperature(
        provider_id="deepseek",
        model_name="deepseek-v4-pro",
        temperature=0.7,
    ) == 0.7
