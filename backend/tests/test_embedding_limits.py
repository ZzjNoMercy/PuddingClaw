from llm.embedding_limits import clamp_embedding_batch_size


def test_dashscope_embedding_batch_limits_are_model_specific():
    assert clamp_embedding_batch_size("text-embedding-v4", 20) == 10
    assert clamp_embedding_batch_size("text-embedding-v3", 50) == 10
    assert clamp_embedding_batch_size("qwen3.7-text-embedding", 50) == 20
    assert clamp_embedding_batch_size("text-embedding-v2", 50) == 25
    assert clamp_embedding_batch_size("custom-embedding", 32) == 32


def test_llamaindex_embedding_client_clamps_workspace_direct_url(monkeypatch):
    import llm.embed_client as embed_client_module

    captured: dict[str, object] = {}

    class FakeOpenAIEmbedding:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        embed_client_module,
        "get_fallback_embedding_config",
        lambda: {
            "protocol": "openai_compatible",
            "model": "text-embedding-v4",
            "api_base": "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "api_key": "test-key",
            "dimension": 1024,
            "batch_size": 20,
        },
    )
    monkeypatch.setattr(embed_client_module, "OpenAIEmbedding", FakeOpenAIEmbedding)

    embed_client_module.get_embedding_model()

    assert captured["embed_batch_size"] == 10


def test_vanna_qwen_embedding_client_uses_same_limit():
    from analytics.nl2sql.improve.clients.embedding_providers import QwenEmbedding

    client = QwenEmbedding(
        api_url="https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        api_key="test-key",
        model_name="text-embedding-v4",
        batch_size=20,
    )

    assert client.batch_size == 10
