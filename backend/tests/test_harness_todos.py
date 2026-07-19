from graph.middlewares.harness_todos import TodoPatchOperation, _apply_operations


def test_todo_patch_rename_and_reorder_preserve_stable_identity():
    original = [
        {"id": "todo-a", "content": "验证所有图表", "status": "pending"},
        {"id": "todo-b", "content": "更新摘要", "status": "in_progress"},
    ]

    updated, _ = _apply_operations(
        original,
        [
            TodoPatchOperation(action="update", todo_id="todo-a", content="验证全部 2026 图表"),
            TodoPatchOperation(action="reorder", ordered_ids=["todo-b", "todo-a"]),
        ],
        tool_call_id="call-1",
        run_id="run-2",
        query_id="query-2",
    )

    assert [item["id"] for item in updated] == ["todo-b", "todo-a"]
    assert [item["position"] for item in updated] == [0, 1]
    renamed = next(item for item in updated if item["id"] == "todo-a")
    assert renamed["content"] == "验证全部 2026 图表"
    assert renamed["status"] == "pending"


def test_todo_create_is_idempotent_for_replayed_tool_call_and_cannot_replace_pending_item():
    operations = [TodoPatchOperation(action="create", content="生成趋势总结")]
    first, _ = _apply_operations(
        [{"id": "todo-a", "content": "验证所有图表", "status": "pending"}],
        operations,
        tool_call_id="call-stable",
        run_id="run-1",
        query_id="query-1",
    )
    replayed, _ = _apply_operations(
        first,
        operations,
        tool_call_id="call-stable",
        run_id="run-1",
        query_id="query-1",
    )

    assert len(replayed) == 2
    assert replayed[0] == first[0]
    assert replayed[1]["id"].startswith("todo_")
    assert replayed[1]["id"] == first[1]["id"]


def test_todo_cancel_is_tombstone_not_deletion():
    updated, _ = _apply_operations(
        [{"id": "todo-a", "content": "旧任务", "status": "pending"}],
        [TodoPatchOperation(action="cancel", todo_id="todo-a")],
        tool_call_id="call-2",
        run_id="run-2",
        query_id="query-2",
    )

    assert updated == [
        {
            "id": "todo-a",
            "content": "旧任务",
            "status": "cancelled",
            "updated_at": updated[0]["updated_at"],
            "last_changed_run_id": "run-2",
            "last_changed_query_id": "query-2",
            "position": 0,
        }
    ]
