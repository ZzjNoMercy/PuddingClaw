"""ModelClient 单元测试。"""

import os
from unittest import mock

import pytest
from langchain_core.language_models.chat_models import BaseChatModel

import capabilities
from llm.model_client import (
    ModelClient,
    ModelTransportInterruptedError,
    _retryable_stream_error,
)


@pytest.fixture(autouse=True)
def _clear_env():
    """清除 AI_GATEWAY_URL，避免影响测试。"""
    os.environ.pop("AI_GATEWAY_URL", None)
    capabilities.invalidate_capabilities()
    yield
    os.environ.pop("AI_GATEWAY_URL", None)
    capabilities.invalidate_capabilities()


@pytest.fixture
def mock_config():
    """Mock config.json 的 fallback_llm 配置。"""
    cfg = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "api_key": "test-key",
        "temperature": 0.7,
    }
    with mock.patch("llm.model_client.get_fallback_llm_config", return_value=cfg):
        yield cfg


def test_model_client_direct_deepseek(mock_config):
    """无 Higress 时返回 ChatDeepSeek。"""
    client = ModelClient(role="agent", force_direct=True)
    llm = client.get_chat_model()
    assert isinstance(llm, BaseChatModel)
    # ChatDeepSeek 类名验证
    assert llm.__class__.__name__ == "ChatDeepSeek"


def test_model_client_direct_openai(mock_config):
    """provider=openai 时返回 ChatOpenAI。"""
    mock_config["provider"] = "openai"
    mock_config["base_url"] = "https://api.openai.com/v1"
    client = ModelClient(role="agent", force_direct=True)
    llm = client.get_chat_model()
    assert llm.__class__.__name__ == "ChatOpenAI"


def test_model_client_direct_zhipu_uses_openai_compatible_transport(mock_config):
    mock_config.update(
        {
            "provider": "zhipu",
            "protocol": "openai_compatible",
            "model": "glm-5.3",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
        }
    )

    llm = ModelClient(role="agent", force_direct=True).get_chat_model()

    assert llm.__class__.__name__ == "ChatOpenAI"
    assert str(llm.openai_api_base).rstrip("/") == "https://open.bigmodel.cn/api/paas/v4"


def test_direct_models_disable_hidden_sdk_retries(mock_config):
    deepseek = ModelClient(role="agent", force_direct=True).get_chat_model()
    assert deepseek.root_client.max_retries == 0

    mock_config["provider"] = "openai"
    mock_config["base_url"] = "https://api.openai.com/v1"
    openai = ModelClient(role="agent", force_direct=True).get_chat_model()
    assert openai.root_client.max_retries == 0


@pytest.mark.parametrize(
    ("class_name", "status_code"),
    [
        ("APIConnectionError", None),
        ("APITimeoutError", None),
        ("APIStatusError", 429),
        ("APIStatusError", 503),
    ],
)
def test_openai_compatible_transport_errors_are_retryable(class_name, status_code):
    error_type = type(class_name, (Exception,), {})
    error = error_type("provider transport failed")
    if status_code is not None:
        error.status_code = status_code

    assert _retryable_stream_error(error) is True


def test_non_retryable_provider_request_error_is_not_retried():
    error_type = type("APIStatusError", (Exception,), {})
    error = error_type("bad request")
    error.status_code = 400

    assert _retryable_stream_error(error) is False


def test_model_client_temperature_override(mock_config):
    """构造时传入 temperature 应覆盖配置。"""
    client = ModelClient(role="title", temperature=0.3, force_direct=True)
    llm = client.get_chat_model()
    assert llm.temperature == 0.3


def test_model_client_forces_kimi_k3_temperature_to_one():
    cfg = {
        "provider": "kimi",
        "model": "kimi-k3",
        "protocol": "openai_compatible",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key": "test-key",
        "temperature": 0.7,
    }
    with mock.patch("llm.model_client.get_fallback_llm_config", return_value=cfg):
        client = ModelClient(role="agent", temperature=0.2, force_direct=True)

    assert client.temperature == 1.0
    assert client.cfg["temperature"] == 1.0


def test_model_client_role_passed():
    """role 应被正确保存。"""
    client = ModelClient(role="summary")
    assert client.role == "summary"


def test_model_client_resolves_explicit_workload_binding():
    cfg = {
        "provider": "openai",
        "model": "qwen-vl-max",
        "base_url": "https://example.test/v1",
        "api_key": "test-key",
        "temperature": 0.2,
    }
    with mock.patch("llm.model_client.get_fallback_llm_config", return_value=cfg) as resolve:
        client = ModelClient(role="subagent", binding="image_analyzer")

    resolve.assert_called_once_with(
        thinking_enabled_override=None,
        binding="image_analyzer",
    )
    assert client.binding == "image_analyzer"
    assert client.cfg["model"] == "qwen-vl-max"


def test_model_client_unknown_provider(mock_config):
    """未知 provider 应抛出 ValueError。"""
    mock_config["provider"] = "unknown"
    client = ModelClient(force_direct=True)
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        client.get_chat_model()


@pytest.mark.asyncio
async def test_model_client_ainvoke_emits_provider_usage_without_database(mock_config):
    """ainvoke publishes provider facts to the Run instead of writing a usage DB."""
    from langchain_core.messages import AIMessage

    fake_response = AIMessage(
        content="hi",
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_token_details": {"cache_read": 8},
            "output_token_details": {"reasoning": 2},
        },
    )

    client = ModelClient(role="title", force_direct=True)
    with mock.patch.object(client, "get_chat_model") as mock_get_model:
        mock_llm = mock.AsyncMock()
        mock_llm.ainvoke.return_value = fake_response
        mock_get_model.return_value = mock_llm

        with mock.patch("llm.model_client._emit_model_stream_event") as emit:
            result = await client.ainvoke([], user_id="u1", session_id="s1", round_num=1)
            assert result == fake_response
            usage_event = emit.call_args.args[0]
            assert usage_event["type"] == "model_usage"
            assert usage_event["role"] == "title"
            assert usage_event["input_tokens"] == 10
            assert usage_event["output_tokens"] == 5
            assert usage_event["cache_read_tokens"] == 8
            assert usage_event["reasoning_tokens"] == 2
            assert usage_event["measured"] is True


def test_model_client_patches_chatopenai_to_preserve_reasoning_content():
    """ChatOpenAI drops provider-specific reasoning_content; our patch preserves it."""
    from langchain_core.messages import AIMessageChunk
    from langchain_openai.chat_models.base import _convert_delta_to_message_chunk

    # Importing model_client applies the patch; keep reference to avoid F401.
    from llm import model_client as _model_client_module

    assert _model_client_module is not None

    delta = {"role": "assistant", "content": "", "reasoning_content": "step 1"}
    chunk = _convert_delta_to_message_chunk(delta, AIMessageChunk)
    assert isinstance(chunk, AIMessageChunk)
    assert chunk.additional_kwargs.get("reasoning_content") == "step 1"


def test_model_client_direct_deepseek_passes_thinking_params():
    """直连 DeepSeek 时，thinking 参数按官方文档传入。"""
    effective_cfg = {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "base_url": "https://api.deepseek.com",
        "api_key": "test-key",
        "temperature": 0.7,
        "max_tokens": 4096,
        "reasoning_effort": "high",
        "extra_body": {"thinking": {"type": "enabled"}},
    }

    with mock.patch(
        "llm.model_client.get_fallback_llm_config",
        return_value=effective_cfg,
    ):
        client = ModelClient(role="agent", force_direct=True)
        llm = client.get_chat_model()

    assert llm.__class__.__name__ == "ChatDeepSeek"
    assert llm.model == "deepseek-v4-pro"
    assert llm.reasoning_effort == "high"
    assert llm.extra_body == {"thinking": {"type": "enabled"}}


@pytest.mark.asyncio
async def test_model_client_retries_transport_error_before_first_chunk(mock_config):
    from langchain_core.messages import AIMessageChunk

    class FakeStreamingModel:
        calls = 0

        async def astream(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionResetError("connection reset before first chunk")
            yield AIMessageChunk(content="recovered")

    fake = FakeStreamingModel()
    client = ModelClient(role="agent", force_direct=True)
    policy = {"harness": {"model_resilience": {"transport_retry": {
        "enabled": True,
        "max_attempts": 2,
        "initial_delay_seconds": 0,
        "max_delay_seconds": 0,
    }}}}
    with (
        mock.patch.object(client, "get_chat_model", return_value=fake),
        mock.patch("llm.model_client.load_config", return_value=policy),
    ):
        chunks = [chunk async for chunk in client.astream([])]

    assert fake.calls == 2
    assert "".join(str(chunk.content) for chunk in chunks) == "recovered"


@pytest.mark.asyncio
async def test_model_client_never_replays_after_a_chunk_was_emitted(mock_config):
    from langchain_core.messages import AIMessageChunk

    class FakeInterruptedStreamingModel:
        calls = 0

        async def astream(self, *_args, **_kwargs):
            self.calls += 1
            yield AIMessageChunk(content="partial")
            raise ConnectionResetError("connection reset after first chunk")

    fake = FakeInterruptedStreamingModel()
    client = ModelClient(role="agent", force_direct=True)
    policy = {"harness": {"model_resilience": {"transport_retry": {
        "enabled": True,
        "max_attempts": 5,
        "initial_delay_seconds": 0,
        "max_delay_seconds": 0,
    }}}}
    received = []
    with (
        mock.patch.object(client, "get_chat_model", return_value=fake),
        mock.patch("llm.model_client.load_config", return_value=policy),
    ):
        with pytest.raises(ModelTransportInterruptedError):
            async for chunk in client.astream([]):
                received.append(chunk)

    assert fake.calls == 1
    assert "".join(str(chunk.content) for chunk in received) == "partial"
