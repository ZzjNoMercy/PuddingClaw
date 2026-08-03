from evaluation.contracts import (
    DatasetBundle,
    EvalCase,
    EvalDataset,
    EvalExpectations,
    EvalInput,
    EvaluatorBinding,
)
from evaluation.dataset_io import export_dataset, import_dataset


def test_bundle_jsonl_and_csv_import_export():
    dataset = EvalDataset(
        name="Portable",
        cases=[
            EvalCase(
                name="Case",
                input=EvalInput(message="hello"),
                expectations=EvalExpectations(contains_all=["world"]),
                dimensions=["task_completion"],
                evaluator_bindings=[EvaluatorBinding(evaluator_id="task_completion.v1", required=True)],
                data_classification="sensitive",
            )
        ],
    )
    bundle = DatasetBundle(dataset=dataset)
    for format in ("bundle", "jsonl", "csv"):
        raw = export_dataset(bundle, format)
        restored = import_dataset(raw, format, name="Imported")
        assert restored.name == "Imported"
        assert len(restored.cases) == 1
        assert restored.cases[0].expectations.contains_all == ["world"]
        assert restored.cases[0].dimensions == ["task_completion"]
        assert restored.cases[0].evaluator_bindings[0].required is True
        assert restored.cases[0].data_classification == "sensitive"
        assert restored.dataset_id != dataset.dataset_id
        assert restored.cases[0].case_id != dataset.cases[0].case_id


def test_notebook_question_answer_csv_dialect_imports_without_column_mapping():
    raw = "question,answer,case_type\n法国首都是什么？,巴黎,core\n"

    restored = import_dataset(raw, "csv", name="Notebook")

    case = restored.cases[0]
    assert case.input.message == "法国首都是什么？"
    assert case.expectations.reference_answer == "巴黎"
    assert case.expectations.contains_any == ["巴黎"]
    assert case.tags == ["core"]
    assert case.case_id.startswith("case_")


def test_notebook_csv_answer_alternatives_become_contains_any():
    restored = import_dataset(
        'question,answer,case_type\n金额是多少？,"1000|1,000",core\n',
        "csv",
    )
    assert restored.cases[0].expectations.contains_any == ["1000", "1,000"]
