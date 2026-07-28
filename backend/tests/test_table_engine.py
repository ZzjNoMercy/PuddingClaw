from __future__ import annotations

import pytest

pd = pytest.importorskip("pandas")

from utils.table_engine.errors import PandasQueryEngineError
from utils.table_engine.executor import blocked_code_token, execute_pandas_code
from utils.table_engine.runner import InProcessPandasRunner


def _df():
    return pd.DataFrame(
        {
            "品牌": ["比亚迪", "比亚迪", "蔚来"],
            "销量": [10, 20, 5],
            "月份": ["2024-01", "2024-02", "2024-01"],
        }
    )


def test_in_process_runner_executes_simple_pandas_expression() -> None:
    result = InProcessPandasRunner().run(_df(), "result = df.loc[df['品牌'] == '比亚迪', '销量'].sum()")

    assert int(result) == 30


def test_in_process_runner_supports_groupby_sort_head() -> None:
    result = execute_pandas_code(
        _df(),
        "grouped = df.groupby('品牌')['销量'].sum().sort_values(ascending=False)\nresult = grouped.head(1)",
    )

    assert result.index[0] == "比亚迪"
    assert int(result.iloc[0]) == 30


def test_in_process_runner_allows_pandas_boolean_masks() -> None:
    result = InProcessPandasRunner().run(
        _df(),
        "filtered = df[((df['品牌'] == '比亚迪') & (df['销量'] >= 10)) | ~(df['月份'] == '2024-01')]\n"
        "result = filtered['销量'].sum()",
    )

    assert int(result) == 30


@pytest.mark.parametrize(
    "code",
    [
        "result = df.__class__",
        "result = pd.read_csv('/etc/passwd')",
        "result = df.to_csv('/tmp/puddingclaw-table-leak.csv')",
        "import os\nresult = 1",
        "result = open('/etc/passwd').read()",
        "while True:\n    result = 1\n    break",
        "def f():\n    return 1\nresult = f()",
        "result = getattr(df, 'shape')",
    ],
)
def test_in_process_runner_blocks_unsafe_generated_code(code: str) -> None:
    with pytest.raises(PandasQueryEngineError):
        InProcessPandasRunner().run(_df(), code)


def test_blocked_code_token_detects_dunder_without_word_boundary_bug() -> None:
    assert blocked_code_token("result = df.__class__") == "__"


def test_in_process_runner_requires_result_assignment() -> None:
    with pytest.raises(PandasQueryEngineError, match="result"):
        InProcessPandasRunner().run(_df(), "df['销量'].sum()")


def test_in_process_runner_does_not_block_plain_text_tokens_inside_strings() -> None:
    df = pd.DataFrame({"备注": ["import ok", "normal"], "销量": [1, 2]})

    result = InProcessPandasRunner().run(df, "result = df[df['备注'].str.contains('import', na=False)]['销量'].sum()")

    assert int(result) == 1
