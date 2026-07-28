from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from analytics.semantic_assets.registry import get_semantic_asset_registry
from analytics.semantic_runtime import (
    build_execution_binding_metadata,
    compile_semantic_query_context,
    render_pandas_semantic_context,
    render_sql_semantic_context,
)
from utils.table_engine.prompts import (
    build_answer_synthesis_prompt,
    build_code_generation_prompt,
)


class _ModelRegistry:
    def __init__(self, context: dict):
        self.context = context

    def get_model_context(self, model_id: str) -> dict:
        assert model_id == self.context["id"]
        return deepcopy(self.context)


def _model_context(*, data_ref: str = "table_asset:sales") -> dict:
    asset_id = data_ref.removeprefix("table_asset:")
    return {
        "id": "sales-model",
        "name": "销量分析",
        "version": "1.0.0",
        "path": "analytics-models/sales-model/model.md",
        "description": "销量分析模型",
        "frontmatter": {
            "id": "sales-model",
            "version": "1.0.0",
            "semantic_assets": {"dimensions": ["dimension:能源类型"]},
            "data_assets": {"tables": [data_ref]},
        },
        "body": "默认按车型颗粒度统计，并遵守能源类型的已发布分类。",
        "semantic_assets": [
            {
                "id": "dimension:能源类型",
                "name": "能源类型",
                "type": "dimension",
            }
        ],
        "asset_relations": [],
        "data_assets": [
            {
                "ref": data_ref,
                "asset_id": asset_id,
                "asset_type": "table_asset",
            }
        ],
        "missing_references": [],
        "missing_data_assets": [],
    }


def _create_energy_asset(tmp_path) -> None:
    registry = get_semantic_asset_registry(tmp_path)
    registry.create_asset(
        name="能源类型",
        asset_type="dimension",
        description="能源大类分类。",
        aliases=["能源大类"],
        dimension_definition={
            "mode": "source_field",
            "bindings": [
                {
                    "asset_ref": "table_asset:sales",
                    "fields": {"value": "燃料种类_细分"},
                }
            ],
        },
    )


def test_compiler_preserves_structured_frontmatter_and_legacy_trace(tmp_path) -> None:
    _create_energy_asset(tmp_path)

    context = compile_semantic_query_context(
        question="按能源大类统计销量",
        model_id="sales-model",
        selected_semantic_asset_ids=["dimension:能源类型"],
        base_dir=tmp_path,
        model_registry=_ModelRegistry(_model_context()),
    )

    assert context.context_id.startswith("semctx-")
    assert context.semantic_hash.startswith("sha256:")
    assert context.trace["matched"][0]["id"] == "dimension:能源类型"
    assert context.trace["matched"][0]["frontmatter"]["resolution"]["bindings"][0]["fields"]["value"] == "燃料种类_细分"
    assert context.trace["references"] == []
    assert context.trace["analytics_model"]["id"] == "sales-model"


def test_semantic_hash_ignores_binding_and_selected_id_order(tmp_path) -> None:
    registry = get_semantic_asset_registry(tmp_path)
    registry.create_asset(name="品牌", asset_type="dimension", description="品牌维度。")
    _create_energy_asset(tmp_path)
    first_model = _model_context(data_ref="table_asset:sales-a")
    first_model["semantic_assets"].append(
        {"id": "dimension:品牌", "name": "品牌", "type": "dimension"}
    )
    first_model["frontmatter"]["semantic_assets"]["dimensions"].append("dimension:品牌")
    second_model = deepcopy(first_model)
    second_model["frontmatter"]["data_assets"] = {"tables": ["table_asset:sales-b"]}
    second_model["data_assets"] = [
        {"ref": "table_asset:sales-b", "asset_id": "sales-b", "asset_type": "table_asset"}
    ]

    first = compile_semantic_query_context(
        question="按品牌和能源类型统计销量",
        model_id="sales-model",
        selected_semantic_asset_ids=["dimension:能源类型", "dimension:品牌"],
        base_dir=tmp_path,
        model_registry=_ModelRegistry(first_model),
    )
    second = compile_semantic_query_context(
        question="按品牌和能源类型统计销量",
        model_id="sales-model",
        selected_semantic_asset_ids=["dimension:品牌", "dimension:能源类型"],
        base_dir=tmp_path,
        model_registry=_ModelRegistry(second_model),
    )

    assert first.semantic_hash == second.semantic_hash
    sql_binding = build_execution_binding_metadata(
        first,
        adapter="sql",
        source_refs=["db.sales"],
    )
    pandas_binding = build_execution_binding_metadata(
        second,
        adapter="pandas",
        source_refs=["table_asset:sales-b"],
        fields=["品牌", "燃料种类_细分", "销量"],
    )
    assert sql_binding["semantic_hash"] == pandas_binding["semantic_hash"]
    assert sql_binding["binding_hash"] != pandas_binding["binding_hash"]


def test_generic_pandas_mode_does_not_fuzzy_load_global_assets(tmp_path) -> None:
    _create_energy_asset(tmp_path)

    context = compile_semantic_query_context(
        question="按能源大类统计销量",
        base_dir=tmp_path,
        allow_global_fuzzy=False,
    )

    assert context.resolution["resolution_mode"] == "generalized"
    assert context.resolution["matched"] == []


def test_strict_model_selection_normalizes_unique_suffix_and_rejects_outside_asset(tmp_path) -> None:
    _create_energy_asset(tmp_path)
    registry = _ModelRegistry(_model_context())

    selected = compile_semantic_query_context(
        question="按能源大类统计销量",
        model_id="sales-model",
        selected_semantic_asset_ids=["能源类型"],
        base_dir=tmp_path,
        model_registry=registry,
        normalize_selected_ids=True,
        strict_selected_ids=True,
    )
    assert selected.semantic_asset_ids == ("dimension:能源类型",)

    with pytest.raises(ValueError, match="不属于当前分析模型"):
        compile_semantic_query_context(
            question="统计配置率",
            model_id="sales-model",
            selected_semantic_asset_ids=["measure:config_rate"],
            base_dir=tmp_path,
            model_registry=registry,
            normalize_selected_ids=True,
            strict_selected_ids=True,
        )


def test_sql_and_pandas_renderers_share_semantics_but_keep_binding_separate(tmp_path) -> None:
    _create_energy_asset(tmp_path)
    context = compile_semantic_query_context(
        question="按能源大类统计销量",
        model_id="sales-model",
        selected_semantic_asset_ids=["dimension:能源类型"],
        base_dir=tmp_path,
        model_registry=_ModelRegistry(_model_context()),
    )

    sql_prompt = render_sql_semantic_context(context)
    pandas_context = render_pandas_semantic_context(
        context,
        dataframe_columns=["燃料种类_细分", "销量"],
        source_ref="table_asset:sales",
    )
    code_prompt = build_code_generation_prompt(
        query="按能源大类统计销量",
        profile={"shape": [2, 2], "columns": ["燃料种类_细分", "销量"]},
        semantic_context=pandas_context,
    )
    answer_prompt = build_answer_synthesis_prompt(
        query="按能源大类统计销量",
        code="result = df.groupby('燃料种类_细分')['销量'].sum()",
        rendered_result="纯电 10",
        semantic_context=pandas_context,
    )

    assert "能源类型" in sql_prompt
    assert pandas_context["semantic_hash"] == context.semantic_hash
    assert pandas_context["binding_hash"].startswith("sha256:")
    assert "燃料种类_细分" in code_prompt
    assert context.context_id in code_prompt
    assert context.context_id in answer_prompt


def test_pandas_tool_uses_exact_model_asset_and_shared_context(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pd = pytest.importorskip("pandas")
    import tools.pandas_knowledge_tool as pandas_tool_module

    _create_energy_asset(tmp_path)
    context = compile_semantic_query_context(
        question="按能源大类统计销量",
        model_id="sales-model",
        selected_semantic_asset_ids=["dimension:能源类型"],
        base_dir=tmp_path,
        model_registry=_ModelRegistry(_model_context()),
    )
    captured: dict = {}
    asset = pandas_tool_module.TableAsset(
        asset_id="sales",
        path=tmp_path / "sales.xlsx",
        virtual_path="/knowledge/sales.xlsx",
        sheet_name="工作表1",
        source_type="excel",
        columns=["燃料种类_细分", "销量"],
        rows=2,
        score=100,
    )

    def fake_list(_self, **kwargs):
        captured["list_kwargs"] = kwargs
        return [asset]

    class _FakeEngine:
        def __init__(self, _df, **kwargs):
            captured["semantic_context"] = kwargs["semantic_context"]

        @staticmethod
        def query(_query):
            return SimpleNamespace(
                answer="纯电销量为 10",
                profile={"shape": [2, 2]},
                to_metadata=lambda: {
                    "semantic_context_id": captured["semantic_context"]["context_id"],
                    "semantic_context_hash": captured["semantic_context"]["content_hash"],
                },
            )

    monkeypatch.setattr(pandas_tool_module, "compile_semantic_query_context", lambda **_kwargs: context)
    monkeypatch.setattr(pandas_tool_module.PandasKnowledgeQueryTool, "_list_table_assets", fake_list)
    monkeypatch.setattr(
        pandas_tool_module.PandasKnowledgeQueryTool,
        "_load_dataframe",
        staticmethod(lambda _asset: pd.DataFrame({"燃料种类_细分": ["纯电", "汽油"], "销量": [10, 5]})),
    )
    monkeypatch.setattr(pandas_tool_module, "PuddingClawPandasQueryEngine", _FakeEngine)

    result = pandas_tool_module.PandasKnowledgeQueryTool(base_dir=str(tmp_path)).query_structured(
        "按能源大类统计销量",
        asset_id="table_asset:sales",
        model_id="sales-model",
        selected_semantic_asset_ids=["dimension:能源类型"],
    )

    assert captured["list_kwargs"]["asset_id"] == "sales"
    assert captured["list_kwargs"]["allowed_asset_ids"] == {"sales"}
    assert captured["semantic_context"]["semantic_hash"] == context.semantic_hash
    assert captured["semantic_context"]["source_ref"] == "table_asset:sales"
    assert result["asset"]["asset_id"] == "sales"
    assert result["semantic_assets"]["semantic_context_id"] == context.context_id
