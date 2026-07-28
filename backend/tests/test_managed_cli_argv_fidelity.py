"""Round-trip fidelity tests for the managed CLI argv parser.

The promise: whatever payload text the agent puts inside standard shell
quotes reaches lark-cli byte-for-byte.  Only provably dangerous shell
composition outside quotes (and command substitution outside single
quotes) may be rejected — with a reason that names the actual problem.
"""

from __future__ import annotations

import pytest

from runtime_identity.adapters import (
    ManagedCliRegistry,
    UnsupportedManagedCliCommand,
    _standalone_argv,
)


def _argv(command: str) -> list[str]:
    parsed = _standalone_argv(command)
    assert parsed is not None, f"expected argv for: {command!r}"
    tokens, _env = parsed
    return tokens


class TestPayloadFidelity:
    def test_multiline_double_quoted_text_is_byte_exact(self):
        payload = "## 标题\n\n第一行\n第二行：含;分号|竖线&符号 <tag> `code`"
        tokens = _argv(f'lark-cli im +messages-send --user-id ou_xxx --text "{payload}" 2>&1')
        assert tokens[-1] == payload

    def test_multiline_single_quoted_markdown_is_byte_exact(self):
        payload = "# Report\n\n- item $(not-a-substitution)\n- `ticks` and ${vars}\n"
        tokens = _argv(f"lark-cli im +messages-send --markdown '{payload}'")
        assert tokens[-1] == payload

    def test_dollar_digits_and_prices_survive(self):
        payload = "ESP32-S3 成本 $8，约合 $9.99，$8 不是变量"
        tokens = _argv(f'lark-cli im +messages-send --text "{payload}"')
        assert tokens[-1] == payload

    def test_emoji_and_cjk_survive(self):
        payload = "👋 测试消息！🔥 AI 圈重点"
        tokens = _argv(f'lark-cli im +messages-send --text "{payload}" 2>&1')
        assert tokens[-1] == payload

    def test_backslash_n_inside_double_quotes_stays_literal(self):
        payload = r"line1\nline2"
        tokens = _argv(f'lark-cli im +messages-send --text "{payload}"')
        assert tokens[-1] == payload

    def test_quoted_redirect_lookalike_is_payload(self):
        payload = "对比 2>&1 的语义"
        tokens = _argv(f'lark-cli im +messages-send --text "{payload}"')
        assert tokens[-1] == payload

    def test_trailing_display_redirect_is_stripped(self):
        tokens = _argv('lark-cli config show 2>&1')
        assert tokens == ["lark-cli", "config", "show"]

    def test_exit_diagnostic_suffix_with_arbitrary_wording_is_stripped(self):
        tokens = _argv('lark-cli auth status 2>&1 || echo "EXIT_CODE: $?"')
        assert tokens == ["lark-cli", "auth", "status"]

    def test_exit_diagnostic_suffix_without_redirect_is_stripped(self):
        tokens = _argv('lark-cli auth status || echo "EXIT:$?"')
        assert tokens == ["lark-cli", "auth", "status"]

    def test_exit_diagnostic_echo_payload_never_reaches_argv(self):
        tokens = _argv('lark-cli auth status 2>&1 || echo "FAIL; rm -rf /"')
        assert tokens == ["lark-cli", "auth", "status"]

    def test_env_prefix_still_works(self):
        tokens = _argv(
            "LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 lark-cli auth status --json"
        )
        assert tokens == ["lark-cli", "auth", "status", "--json"]


    def test_dollar_substitution_lookalike_inside_quotes_is_payload(self):
        payload = "文档里写着 $(cat file) 和 ${VAR} 只是示例文本"
        tokens = _argv(f'lark-cli im +messages-send --text "{payload}"')
        assert tokens[-1] == payload


class TestRejections:
    @pytest.mark.parametrize(
        "command,fragment",
        [
            ("lark-cli config show; rm -rf /", "outside quotes"),
            ("lark-cli config show && ls", "outside quotes"),
            ("lark-cli config show | cat", "outside quotes"),
            ("lark-cli im +messages-send --text $(cat /etc/passwd)", "substitution"),
            ("lark-cli im +messages-send --text `id`", "substitution"),
            ("lark-cli im +messages-send --text $'a\\nb'", "ANSI-C"),
            ('lark-cli im +messages-send --text "unterminated', "unterminated"),
        ],
    )
    def test_unsafe_surface_rejected_with_reason(self, command, fragment):
        assert _standalone_argv(command) is None
        registry = ManagedCliRegistry()
        with pytest.raises(UnsupportedManagedCliCommand, match=fragment):
            registry.match(command)

    def test_non_lark_shell_composition_falls_through(self):
        registry = ManagedCliRegistry()
        assert registry.match("ls -la | grep foo && cat bar") is None


class TestRegistryReason:
    def test_newline_rejection_names_the_problem(self):
        registry = ManagedCliRegistry()
        with pytest.raises(UnsupportedManagedCliCommand) as excinfo:
            registry.match("lark-cli config show\nls")
        assert "newline" in str(excinfo.value)
