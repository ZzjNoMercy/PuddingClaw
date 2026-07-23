import json

from harness.artifact_contracts import validate_heatmap_year_contract


def test_heatmap_contract_rejects_missing_year_and_wrong_shape() -> None:
    result = validate_heatmap_year_contract(
        html=(
            '<select id="heatmapYearSelect">'
            '<option value="2024" selected>2024</option>'
            "</select>"
            '<script src="charts.js"></script>'
        ),
        javascript=(
            'const heatmapByYear = {"2024": [[1]], "2025": [[2]]};'
            'let currentHeatYear = "2025";'
            'selector.addEventListener("change", function () {'
            "const data = heatmapByYear[currentHeatYear];"
            "});"
        ),
        javascript_filename="charts.js",
    )

    assert result["passed"] is False
    assert result["checks"]["year_key_set_equal"] is False
    assert result["checks"]["default_year_equal"] is False
    assert result["checks"]["matrix_shape_valid"] is False


def test_heatmap_contract_accepts_matching_selector_data_and_script() -> None:
    matrix_2025 = [[0] * 10 for _ in range(8)]
    matrix_2026 = [[1] * 10 for _ in range(8)]
    result = validate_heatmap_year_contract(
        html=(
            '<select id="heatmapYearSelect">'
            '<option value="2025">2025</option>'
            '<option value="2026" selected>2026</option>'
            "</select>"
            '<script src="charts.js?v=synthetic"></script>'
        ),
        javascript=(
            "const heatmapByYear = "
            + json.dumps({"2025": matrix_2025, "2026": matrix_2026})
            + '; let currentHeatYear = "2026"; '
            + 'selector.addEventListener("change", function () {'
            + "heatmapByYear[currentHeatYear]; });"
        ),
        javascript_filename="charts.js",
    )

    assert result["passed"] is True
    assert result["checks"]["year_key_set_equal"] is True
    assert result["checks"]["default_year_equal"] is True
    assert result["checks"]["matrix_shape_valid"] is True


def test_heatmap_contract_rejects_commented_nodes_and_invalid_javascript() -> None:
    matrix = [[0] * 10 for _ in range(8)]
    result = validate_heatmap_year_contract(
        html=(
            '<!-- <select id="heatmapYearSelect">'
            '<option value="2026" selected>2026</option></select> -->'
            '<script src="charts.js"></script>'
        ),
        javascript=(
            "/* const heatmapByYear = "
            + json.dumps({"2026": matrix})
            + '; let currentHeatYear = "2026"; '
            + 'selector.addEventListener("change", function () {'
            + "heatmapByYear[currentHeatYear]; }); */\n"
            + "this is not valid javascript !!!"
        ),
        javascript_filename="charts.js",
    )

    assert result["passed"] is False
    assert result["checks"]["select_present"] is False
    assert result["checks"]["javascript_syntax_valid"] is False


def test_heatmap_contract_rejects_html_that_loads_a_different_script() -> None:
    matrix = [[0] * 10 for _ in range(8)]
    result = validate_heatmap_year_contract(
        html=(
            '<select id="heatmapYearSelect">'
            '<option value="2026" selected>2026</option></select>'
            '<script src="old-charts.js?v=1"></script>'
        ),
        javascript=(
            "const heatmapByYear = "
            + json.dumps({"2026": matrix})
            + '; let currentHeatYear = "2026"; '
            + 'selector.addEventListener("change", function () {'
            + "heatmapByYear[currentHeatYear]; });"
        ),
        javascript_filename="charts.js",
    )

    assert result["passed"] is False
    assert result["checks"]["script_source_matches"] is False


def test_heatmap_contract_ignores_valid_javascript_comment_lookalikes() -> None:
    matrix = [[0] * 10 for _ in range(8)]
    result = validate_heatmap_year_contract(
        html=(
            '<select id="heatmapYearSelect">'
            '<option value="2026" selected>2026</option></select>'
            '<script src="charts.js"></script>'
        ),
        javascript=(
            "/* const heatmapByYear = "
            + json.dumps({"2026": matrix})
            + '; let currentHeatYear = "2026"; '
            + 'selector.addEventListener("change", function () {'
            + "heatmapByYear[currentHeatYear]; }); */\n"
            + "const unrelated = 1;"
        ),
        javascript_filename="charts.js",
    )

    assert result["passed"] is False
    assert result["checks"]["javascript_syntax_valid"] is True
    assert result["observed"]["data_years"] == []
