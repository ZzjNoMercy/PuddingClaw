from unittest.mock import Mock

from api import tokens


def test_missing_tiktoken_cache_never_calls_network_capable_loader(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(tokens, "_encoder", None)
    monkeypatch.setattr(tokens, "_fallback_logged", False)
    get_encoding = Mock(side_effect=AssertionError("must not load without cache"))
    monkeypatch.setattr(tokens.tiktoken, "get_encoding", get_encoding)

    assert tokens._get_cached_encoder() is None
    get_encoding.assert_not_called()


def test_verified_tiktoken_cache_allows_lazy_encoder_load(monkeypatch) -> None:
    encoder = Mock()
    monkeypatch.setattr(tokens, "_encoder", None)
    monkeypatch.setattr(tokens, "_has_verified_tiktoken_cache", lambda: True)
    get_encoding = Mock(return_value=encoder)
    monkeypatch.setattr(tokens.tiktoken, "get_encoding", get_encoding)

    assert tokens._get_cached_encoder() is encoder
    get_encoding.assert_called_once_with("cl100k_base")


def test_offline_estimate_handles_empty_latin_and_cjk_text() -> None:
    assert tokens._estimate_tokens("") == 0
    assert tokens._estimate_tokens("abcdefgh") == 2
    assert tokens._estimate_tokens("中文测试") == 5
