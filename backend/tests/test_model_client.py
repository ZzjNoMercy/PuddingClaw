"""ModelClient 单元测试。"""

import os
from unittest import mock

import pytest
from langchain_core.language_models.chat_models import BaseChatModel

import capabilities
from llm.model_client import ModelClient


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
    """Mock config.json 的 llm 配置。"""
    cfg = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "api_key": "test-key",
        "temperature": 0.7,
    }
    with mock.patch("llm.model_client.get_llm_config", return_value=cfg):
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


def test_model_client_temperature_override(mock_config):
    """构造时传入 temperature 应覆盖配置。"""
    client = ModelClient(role="title", temperature=0.3, force_direct=True)
    llm = client.get_chat_model()
    assert llm.temperature == 0.3


def test_model_client_role_passed():
    """role 应被正确保存。"""
    client = ModelClient(role="summary")
    assert client.role == "summary"


def test_model_client_unknown_provider(mock_config):
    """未知 provider 应抛出 ValueError。"""
    mock_config["provider"] = "unknown"
    client = ModelClient(force_direct=True)
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        client.get_chat_model()


@pytest.mark.asyncio
async def test_model_client_ainvoke_records_usage(mock_config):
    """ainvoke 应记录 token 用量。"""
    from langchain_core.messages import AIMessage

    fake_response = AIMessage(content="hi", usage_metadata={
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    })

    client = ModelClient(role="title", force_direct=True)
    with mock.patch.object(client, "get_chat_model") as mock_get_model:
        mock_llm = mock.AsyncMock()
        mock_llm.ainvoke.return_value = fake_response
        mock_get_model.return_value = mock_llm

        with mock.patch("llm.model_client.record_token_usage") as mock_record:
            result = await client.ainvoke([], user_id="u1", session_id="s1", round_num=1)
            assert result == fake_response
            mock_record.assert_called_once()
            _, kwargs = mock_record.call_args
            assert kwargs["role"] == "title"
            assert kwargs["user_id"] == "u1"
            assert kwargs["session_id"] == "s1"
            assert kwargs["round_num"] == 1
            assert kwargs["input_tokens"] == 10
            assert kwargs["output_tokens"] == 5


@pytest.mark.asyncio
async def test_model_client_gateway_failure_falls_back_to_direct(mock_config):
    """Gateway 在首个响应前失败时，应回退直连 Provider。"""
    from langchain_core.messages import AIMessage

    client = ModelClient(role="title")
    client.gateway_cfg = {
        "enabled": True,
        "base_url": "http://gateway:8080/v1",
        "fallback_to_direct": True,
    }
    gateway = mock.AsyncMock()
    gateway.ainvoke.side_effect = RuntimeError("gateway down")
    direct = mock.AsyncMock()
    direct.ainvoke.return_value = AIMessage(content="fallback")

    with mock.patch.object(client, "_should_use_gateway", return_value=True):
        with mock.patch.object(client, "get_chat_model", return_value=gateway):
            with mock.patch.object(client, "_direct_model", return_value=direct):
                with mock.patch("llm.model_client.record_token_usage"):
                    result = await client.ainvoke([])

    assert result.content == "fallback"
    direct.ainvoke.assert_awaited_once()
