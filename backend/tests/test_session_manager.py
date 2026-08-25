"""SessionManager 持久化与 reasoning_content 处理测试。"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from graph.session_manager import session_manager


def test_new_sessions_default_to_agent_runtime(tmp_path):
    session_manager.initialize(tmp_path)

    metadata = session_manager.create_session("default-runtime")

    assert metadata["runtime_mode"] == "agent"


def test_metadata_cannot_create_or_overwrite_control_plane_state(tmp_path):
    session_manager.initialize(tmp_path)

    with pytest.raises(FileNotFoundError):
        session_manager.update_metadata("missing", {"runtime_mode": "agent"})

    session_manager.create_session("protected")
    with pytest.raises(ValueError, match="Unsupported Session metadata"):
        session_manager.update_metadata(
            "protected",
            {"permissions": {"approval_mode": "smart"}},
        )
    assert session_manager.get_permission_policy("protected")["approval_mode"] == "smart"


def test_deleted_session_cannot_be_recreated_by_message_writes(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("deleted")
    session_manager.delete_session("deleted")

    with pytest.raises(FileNotFoundError):
        session_manager.save_message("deleted", "user", "must not reappear")
    with pytest.raises(FileNotFoundError):
        session_manager.upsert_assistant_message(
            "deleted",
            query_id="query-1",
            content="must not reappear",
        )

    assert not (tmp_path / "sessions" / "deleted.json").exists()


def test_assistant_usage_summary_round_trips_in_session_json(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("usage-session")
    usage_summary = {
        "run_id": "run-1",
        "rounds": 2,
        "steps": 3,
        "input_tokens": 48100,
        "output_tokens": 2700,
        "cache_hit_rate": 80.0,
    }

    session_manager.upsert_assistant_message(
        "usage-session",
        query_id="query-1",
        content="done",
        usage_summary=usage_summary,
        status="completed",
    )

    message = session_manager.load_session("usage-session")[-1]
    assert message["usage_summary"] == usage_summary


def test_analytics_model_id_round_trips_in_session_metadata(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session(
        "analytics-model-session",
        metadata={"analytics_model_id": "产品配置分析"},
    )

    assert session_manager.get_metadata("analytics-model-session")["analytics_model_id"] == "产品配置分析"
    listed = {item["id"]: item for item in session_manager.list_sessions()}
    assert listed["analytics-model-session"]["analytics_model_id"] == "产品配置分析"

    cleared = session_manager.update_metadata(
        "analytics-model-session",
        {"analytics_model_id": None},
    )
    assert "analytics_model_id" in cleared
    assert cleared["analytics_model_id"] is None
    assert session_manager.get_metadata("analytics-model-session")["analytics_model_id"] is None


def test_background_job_execution_context_is_not_listed_as_conversation(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("visible-session", metadata={"runtime_mode": "agent"})
    session_manager.create_session("background-job-job_123", metadata={"runtime_mode": "agent"})

    assert [item["id"] for item in session_manager.list_sessions()] == ["visible-session"]


def test_todo_ledgers_continue_same_goal_revision_without_cross_contamination(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("todo-scope")
    todos = [
        {
            "id": "todo-1",
            "content": "更新图表",
            "status": "in_progress",
            "goal_id": "goal-1",
            "goal_revision": 1,
            "created_run_id": "run-1",
        }
    ]
    session_manager.update_todos(
        "todo-scope",
        todos,
        goal_id="goal-1",
        goal_revision=1,
        run_id="run-1",
    )

    assert session_manager.get_todos(
        "todo-scope", goal_id="goal-1", goal_revision=1, run_id="run-2"
    ) == todos
    assert session_manager.get_todos(
        "todo-scope", goal_id="goal-1", goal_revision=2, run_id="run-3"
    ) == []
    assert session_manager.get_todos(
        "todo-scope", goal_id="goal-2", goal_revision=1, run_id="run-4"
    ) == []
    assert session_manager.get_todos("todo-scope", run_id="standalone-run") == []


def test_atomic_todo_patch_is_durable_revisioned_and_idempotent(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("todo-atomic")

    def create(items):
        return [*items, {"id": "todo-1", "content": "查询数据", "status": "pending"}], [
            {"action": "create", "todo_id": "todo-1"}
        ]

    first = session_manager.apply_todo_patch(
        "todo-atomic",
        goal_id="goal-1",
        goal_revision=1,
        operation_id="call-1",
        mutator=create,
    )
    replay = session_manager.apply_todo_patch(
        "todo-atomic",
        goal_id="goal-1",
        goal_revision=1,
        operation_id="call-1",
        mutator=lambda _items: (_ for _ in ()).throw(AssertionError("must not replay")),
    )

    assert first["ledger_revision"] == 1
    assert first["replayed"] is False
    assert replay["ledger_revision"] == 1
    assert replay["replayed"] is True
    assert session_manager.get_todos(
        "todo-atomic", goal_id="goal-1", goal_revision=1
    ) == first["todos"]


def test_atomic_todo_patches_merge_concurrent_stable_id_updates(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("todo-concurrent")
    session_manager.apply_todo_patch(
        "todo-concurrent",
        run_id="run-1",
        operation_id="seed",
        mutator=lambda _items: (
            [
                {"id": "todo-a", "content": "A", "status": "pending"},
                {"id": "todo-b", "content": "B", "status": "pending"},
            ],
            [{"action": "seed"}],
        ),
    )

    def update(todo_id):
        def mutator(items):
            updated = [dict(item) for item in items]
            next(item for item in updated if item["id"] == todo_id)["status"] = "completed"
            return updated, [{"action": "complete", "todo_id": todo_id}]

        return session_manager.apply_todo_patch(
            "todo-concurrent",
            run_id="run-1",
            operation_id=f"complete-{todo_id}",
            mutator=mutator,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(update, ["todo-a", "todo-b"]))

    assert sorted(receipt["ledger_revision"] for receipt in receipts) == [2, 3]
    snapshot = session_manager.get_todo_snapshot("todo-concurrent", run_id="run-1")
    assert snapshot["ledger_revision"] == 3
    assert {item["id"]: item["status"] for item in snapshot["todos"]} == {
        "todo-a": "completed",
        "todo-b": "completed",
    }


def test_transactional_todo_ledger_rejects_stale_revision_and_list_overwrite(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("todo-conflict")
    session_manager.apply_todo_patch(
        "todo-conflict",
        run_id="run-1",
        operation_id="create",
        mutator=lambda _items: (
            [{"id": "todo-1", "content": "new", "status": "in_progress"}],
            [{"action": "create", "todo_id": "todo-1"}],
        ),
    )

    with pytest.raises(ValueError, match="revision conflict"):
        session_manager.apply_todo_patch(
            "todo-conflict",
            run_id="run-1",
            operation_id="stale-reorder",
            expected_revision=0,
            mutator=lambda items: (items, [{"action": "reorder"}]),
        )
    with pytest.raises(ValueError, match="list replacement"):
        session_manager.update_todos(
            "todo-conflict",
            [{"id": "todo-old", "content": "old", "status": "pending"}],
            run_id="run-1",
        )
    assert session_manager.get_todo_snapshot("todo-conflict", run_id="run-1")["todos"][0][
        "content"
    ] == "new"


def test_todo_patch_rejects_superseded_goal_revision(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("todo-goal-revision")
    data = session_manager._read_file("todo-goal-revision")
    data["harness"] = {
        "active_goal_id": "goal-1",
        "goals": {
            "goal-1": {
                "goal_id": "goal-1",
                "status": "active",
                "objective_revision": 2,
            }
        },
        "runs": {},
        "run_order": [],
    }
    session_manager._write_file("todo-goal-revision", data)

    with pytest.raises(ValueError, match="Goal revision conflict"):
        session_manager.apply_todo_patch(
            "todo-goal-revision",
            goal_id="goal-1",
            goal_revision=1,
            operation_id="stale-goal-write",
            mutator=lambda items: (items, []),
        )


def test_running_goal_inspection_projects_context_goal_todos(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("todo-inspection")
    todos = [{"id": "todo-1", "content": "保留进度", "status": "in_progress"}]
    session_manager.update_todos(
        "todo-inspection", todos, goal_id="goal-1", goal_revision=1
    )
    data = session_manager._read_file("todo-inspection")
    data["harness"] = {
        "active_goal_id": "goal-1",
        "goals": {"goal-1": {"goal_id": "goal-1", "objective_revision": 1, "status": "active"}},
        "runs": {
            "run-inspect": {
                "run_id": "run-inspect",
                "run_kind": "goal_inspection",
                "context_goal_id": "goal-1",
                "context_goal_revision": 1,
                "goal_id": None,
                "status": "running",
            }
        },
        "run_order": ["run-inspect"],
        "latest_run_id": "run-inspect",
    }
    session_manager._write_file("todo-inspection", data)

    snapshot = session_manager.get_todo_snapshot("todo-inspection")
    assert snapshot["todos"] == todos
    assert snapshot["authority"] == {
        "kind": "goal",
        "goal_id": "goal-1",
        "goal_revision": 1,
    }
    assert snapshot["ledger_revision"] == 1


def test_raw_message_todos_project_only_current_goal_or_nonterminal_run(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("todo-projection")
    goal_todos = [{"id": "goal-todo", "content": "完成目标", "status": "completed"}]
    run_todos = [{"id": "run-todo", "content": "处理追问", "status": "in_progress"}]
    session_manager.update_todos(
        "todo-projection",
        goal_todos,
        goal_id="goal-1",
        goal_revision=1,
        run_id="run-goal",
    )
    data = session_manager._read_file("todo-projection")
    data["harness"] = {
        "active_goal_id": None,
        "goals": {
            "goal-1": {"goal_id": "goal-1", "objective_revision": 1, "status": "achieved"}
        },
        "runs": {
            "run-goal": {"run_id": "run-goal", "status": "completed", "goal_id": "goal-1"}
        },
        "run_order": ["run-goal"],
        "latest_run_id": "run-goal",
    }
    session_manager._write_file("todo-projection", data)

    # Completed work remains in its scoped ledger but is not current UI state.
    completed = session_manager.get_raw_messages("todo-projection")
    assert completed["todos"] == []
    assert completed["todos_authority"] == {"kind": "none"}
    assert session_manager.get_todos(
        "todo-projection", goal_id="goal-1", goal_revision=1
    ) == goal_todos

    session_manager.update_todos("todo-projection", run_todos, run_id="run-followup")
    data = session_manager._read_file("todo-projection")
    data["harness"]["runs"]["run-followup"] = {
        "run_id": "run-followup",
        "status": "running",
        "goal_id": None,
    }
    data["harness"]["run_order"].append("run-followup")
    data["harness"]["latest_run_id"] = "run-followup"
    session_manager._write_file("todo-projection", data)

    active_run = session_manager.get_raw_messages("todo-projection")
    assert active_run["todos"] == run_todos
    assert active_run["todos_authority"] == {"kind": "run", "run_id": "run-followup"}

    data = session_manager._read_file("todo-projection")
    data["harness"]["runs"]["run-followup"]["status"] = "completed"
    data["harness"]["active_goal_id"] = "goal-2"
    data["harness"]["goals"]["goal-2"] = {
        "goal_id": "goal-2",
        "objective_revision": 2,
        "status": "active",
    }
    data.setdefault("todo_ledgers", {})["goal:goal-2:revision:2"] = [
        {"id": "goal-2-todo", "content": "新目标", "status": "pending"}
    ]
    session_manager._write_file("todo-projection", data)

    active_goal = session_manager.get_raw_messages("todo-projection")
    assert active_goal["todos"][0]["id"] == "goal-2-todo"
    assert active_goal["todos_authority"] == {
        "kind": "goal",
        "goal_id": "goal-2",
        "goal_revision": 2,
    }

    standalone_todos = [
        {"id": "standalone-todo", "content": "回答独立追问", "status": "in_progress"}
    ]
    session_manager.update_todos(
        "todo-projection", standalone_todos, run_id="run-standalone"
    )
    data = session_manager._read_file("todo-projection")
    data["harness"]["runs"]["run-standalone"] = {
        "run_id": "run-standalone",
        "status": "running",
        "goal_id": None,
    }
    data["harness"]["run_order"].append("run-standalone")
    data["harness"]["latest_run_id"] = "run-standalone"
    session_manager._write_file("todo-projection", data)

    standalone = session_manager.get_raw_messages("todo-projection")
    assert standalone["todos"] == standalone_todos
    assert standalone["todos_authority"] == {
        "kind": "run",
        "run_id": "run-standalone",
    }


def test_save_and_load_reasoning_content_for_tool_call_turn(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("reasoning-session")

    session_manager.save_message(
        "reasoning-session",
        "assistant",
        "正式回答",
        tool_calls=[{"tool": "terminal", "input": "ls"}],
        reasoning_content="我需要先列出目录内容。",
    )

    history = session_manager.load_session("reasoning-session")
    assistant = history[0]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "正式回答"
    assert assistant["reasoning_content"] == "我需要先列出目录内容。"


def test_load_session_for_agent_excludes_cross_run_reasoning(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("agent-reasoning-session")

    session_manager.save_message(
        "agent-reasoning-session",
        "assistant",
        "正式回答",
        tool_calls=[{"tool": "terminal", "input": "ls"}],
        reasoning_content="我需要先列出目录内容。",
    )

    messages = session_manager.load_session_for_agent("agent-reasoning-session")
    assistant = messages[0]
    assert assistant["role"] == "assistant"
    assert "reasoning_content" not in assistant


def test_load_session_for_agent_preserves_attachment_references(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("agent-attachment-session")
    attachment = {
        "id": "att_image123",
        "name": "image.png",
        "type": "image",
        "mime_type": "image/png",
        "size": 128,
        "source": "clipboard",
        "sha256": "sha256:test",
        "download_url": "/api/attachments/att_image123/download?session_id=agent-attachment-session",
        "preview_url": "/api/attachments/att_image123/preview?session_id=agent-attachment-session",
    }

    session_manager.save_message(
        "agent-attachment-session",
        "user",
        "这图讲了什么",
        attachments=[attachment],
    )

    messages = session_manager.load_session_for_agent("agent-attachment-session")

    assert messages[0]["role"] == "user"
    assert messages[0]["attachments"][0]["id"] == "att_image123"
    assert messages[0]["attachments"][0]["name"] == "image.png"


def test_load_session_for_agent_restores_cross_run_tool_output_as_evidence(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("agent-tool-output-session")

    session_manager.save_message(
        "agent-tool-output-session",
        "assistant",
        "现在查询比亚迪 2023 年 5 月销量。",
        tool_calls=[
            *[
                {
                    "tool": "pandas_knowledge_query",
                    "input": f'{{"query": "前置长输出 {idx}"}}',
                    "output": "前置工具输出。" * 120,
                }
                for idx in range(5)
            ],
            {
                "tool": "pandas_knowledge_query",
                "input": '{"query": "比亚迪汽车 2023年5月 销量"}',
                "output": "筛选比亚迪汽车且月份为5后，2023年销量总和为205390。",
            },
        ],
    )

    messages = session_manager.load_session_for_agent("agent-tool-output-session")
    assistant = messages[0]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "现在查询比亚迪 2023 年 5 月销量。"
    assert len(assistant["tool_calls"]) == 6
    restored = assistant["tool_calls"][-1]
    assert restored["output"].endswith("205390。")
    assert restored["historical"] is True
    assert restored["evidence_id"].startswith("evidence-")
    assert restored["raw_output_ref"]["kind"] == "session_tool_call"


def test_read_evidence_resolves_large_result_from_source_query(tmp_path):
    (tmp_path / "backend").mkdir()
    session_manager.initialize(tmp_path / "backend")
    session_manager.create_session("evidence-session")
    workspace = tmp_path / "workspace"
    raw_text = "complete historical payload"
    raw_ref = session_manager.materialize_large_tool_result(
        workspace_path=workspace,
        session_id="evidence-session",
        query_id="query-old",
        tool_call_id="call-large",
        output=raw_text,
    )
    source_hash = raw_ref["source_hash"]
    session_manager.upsert_assistant_message(
        "evidence-session",
        content="old result",
        query_id="query-old",
        tool_calls=[
            {
                "tool": "read_file",
                "id": "call-large",
                "input": {"file_path": "/workspace/big.txt"},
                "output": "Result saved to /large_tool_results/call-large",
                "source_hash": source_hash,
                "raw_output_ref": raw_ref,
            }
        ],
    )

    history = session_manager.load_session_for_agent("evidence-session")
    evidence_id = history[0]["tool_calls"][0]["evidence_id"]
    restored = session_manager.read_evidence(
        "evidence-session",
        evidence_id,
    )

    assert restored["status"] == "success"
    assert restored["content"] == raw_text
    assert restored["raw_result_available"] is True
    assert restored["hash_matches"] is True
    assert raw_ref["workspace_digest"] == hashlib.sha256(
        str(workspace.resolve()).encode("utf-8")
    ).hexdigest()[:20]


def test_explicit_sessions_dir_keeps_large_results_below_puddingclaw_home(tmp_path):
    user_root = tmp_path / ".puddingclaw"
    sessions_dir = user_root / "sessions"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_manager.initialize(sessions_dir=sessions_dir)

    raw_ref = session_manager.materialize_large_tool_result(
        workspace_path=workspace,
        session_id="production-layout-session",
        query_id="query-1",
        tool_call_id="call-large",
        output="complete payload",
    )

    artifact = (
        user_root
        / "data"
        / "large-tool-results"
        / "projects"
        / raw_ref["workspace_digest"]
        / "production-layout-session"
        / "query-1"
        / "call-large"
    )
    assert session_manager._base_dir == user_root
    assert artifact.read_text(encoding="utf-8") == "complete payload"
    assert not (tmp_path / "data" / "large-tool-results").exists()


def test_read_sql_evidence_pages_saved_jsonl_without_preview_fallback(tmp_path):
    base_dir = tmp_path / "home"
    base_dir.mkdir()
    session_manager.initialize(base_dir)
    session_manager.create_session("sql-evidence-session")
    result_dir = base_dir / "data" / "query-results"
    result_dir.mkdir(parents=True)
    (result_dir / "qr-evidence.jsonl").write_text(
        '{"id":1}\n{"id":2}\n{"id":3}\n',
        encoding="utf-8",
    )
    artifact = result_dir / "qr-evidence.jsonl"
    catalog_dir = result_dir / ".catalog"
    catalog_dir.mkdir()
    (catalog_dir / "qr-evidence.json").write_text(
        json.dumps(
            {
                "result_id": "qr-evidence",
                "session_id": "sql-evidence-session",
                "tool_call_id": "call-sql",
                "artifact_path": "qr-evidence.jsonl",
                "artifact_format": "jsonl",
                "artifact_sha256": f"sha256:{hashlib.sha256(artifact.read_bytes()).hexdigest()}",
                "row_count": 3,
                "status": "ready",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    session_manager.upsert_assistant_message(
        "sql-evidence-session",
        content="query done",
        query_id="query-sql",
        tool_calls=[
            {
                "tool": "database_sql_execute",
                "id": "call-sql",
                "input": {"generation_id": "gen-1"},
                "output": (
                    "generation_id：gen-1\nvalidation_receipt_id：receipt-1\n"
                    "sql_sha256：sha256:abc\nresult_id：qr-evidence\npreview only"
                ),
            }
        ],
    )

    history = session_manager.load_session_for_agent("sql-evidence-session")
    call = history[0]["tool_calls"][0]
    restored = session_manager.read_evidence(
        "sql-evidence-session",
        call["evidence_id"],
        page=2,
        page_size=2,
    )

    assert call["raw_output_ref"]["generation_id"] == "gen-1"
    assert call["raw_output_ref"]["validation_receipt_id"] == "receipt-1"
    assert restored["rows"] == [{"id": 3}]
    assert restored["has_next"] is False
    (result_dir / "qr-evidence.jsonl").unlink()
    expired = session_manager.read_evidence(
        "sql-evidence-session",
        call["evidence_id"],
    )
    assert expired["status"] == "missing"
    assert expired["output_complete"] is False
    assert expired["raw_result_available"] is False


def test_sql_evidence_rejects_wrong_owner_expiry_and_tampering(tmp_path):
    base_dir = tmp_path / "home"
    base_dir.mkdir()
    session_manager.initialize(base_dir)
    session_manager.create_session("sql-owner-session")
    result_dir = base_dir / "data" / "query-results"
    catalog_dir = result_dir / ".catalog"
    catalog_dir.mkdir(parents=True)
    artifact = result_dir / "qr-secure.jsonl"
    artifact.write_text('{"id":1}\n', encoding="utf-8")
    catalog = {
        "result_id": "qr-secure",
        "session_id": "sql-owner-session",
        "tool_call_id": "call-secure",
        "artifact_path": "qr-secure.jsonl",
        "artifact_format": "jsonl",
        "artifact_sha256": f"sha256:{hashlib.sha256(artifact.read_bytes()).hexdigest()}",
        "row_count": 1,
        "status": "ready",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    catalog_path = catalog_dir / "qr-secure.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    session_manager.upsert_assistant_message(
        "sql-owner-session",
        query_id="query-secure",
        content="done",
        tool_calls=[
            {
                "tool": "database_sql_execute",
                "id": "call-secure",
                "output": "result_id：qr-secure",
            }
        ],
    )
    evidence_id = session_manager.load_session_for_agent("sql-owner-session")[0]["tool_calls"][0][
        "evidence_id"
    ]

    catalog["session_id"] = "another-session"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    assert session_manager.read_evidence("sql-owner-session", evidence_id)["status"] == "unauthorized"

    catalog["session_id"] = "sql-owner-session"
    catalog["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    assert session_manager.read_evidence("sql-owner-session", evidence_id)["status"] == "expired"

    catalog["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    artifact.write_text('{"id":999}\n', encoding="utf-8")
    corrupted = session_manager.read_evidence("sql-owner-session", evidence_id)
    assert corrupted["status"] == "corrupt"
    assert corrupted["output_complete"] is False
    assert corrupted["rows"] == []


def test_standalone_run_inherits_latest_unfinished_todo_ledger(tmp_path):
    from harness.models import RunOutcome, RunRecord

    session_manager.initialize(tmp_path)
    session_manager.create_session("todo-continuation-session")
    prior = RunRecord(
        run_id="run-prior",
        query_id="query-prior",
        session_id="todo-continuation-session",
        objective="prepare report",
    )
    session_manager.upsert_run_state(
        "todo-continuation-session",
        prior.model_dump(mode="json"),
    )
    session_manager.update_todos(
        "todo-continuation-session",
        [{"id": "todo-stable", "content": "finish report", "status": "in_progress"}],
        run_id=prior.run_id,
    )
    terminal = prior.model_copy(update={"outcome": RunOutcome.FAILED})
    session_manager.terminalize_run_state(
        "todo-continuation-session",
        prior.run_id,
        terminal.model_dump(mode="json"),
    )
    current = RunRecord(
        run_id="run-current",
        query_id="query-current",
        session_id="todo-continuation-session",
        objective="continue",
    )
    session_manager.upsert_run_state(
        "todo-continuation-session",
        current.model_dump(mode="json"),
    )

    inherited = session_manager.inherit_unfinished_todos_for_run(
        "todo-continuation-session",
        current.run_id,
        continuation_requested=True,
    )

    assert inherited == [
        {"id": "todo-stable", "content": "finish report", "status": "in_progress"}
    ]
    assert session_manager.get_todos(
        "todo-continuation-session",
        run_id=current.run_id,
    ) == inherited


def test_unrelated_standalone_run_does_not_inherit_todos(tmp_path):
    from harness.models import RunRecord

    session_manager.initialize(tmp_path)
    session_manager.create_session("todo-isolation-session")
    prior = RunRecord(
        run_id="run-report",
        query_id="query-report",
        session_id="todo-isolation-session",
        objective="prepare report",
    )
    current = RunRecord(
        run_id="run-weather",
        query_id="query-weather",
        session_id="todo-isolation-session",
        objective="what is the weather",
    )
    session_manager.upsert_run_state("todo-isolation-session", prior.model_dump(mode="json"))
    session_manager.update_todos(
        "todo-isolation-session",
        [{"id": "todo-report", "content": "finish report", "status": "in_progress"}],
        run_id=prior.run_id,
    )
    session_manager.upsert_run_state("todo-isolation-session", current.model_dump(mode="json"))

    assert session_manager.inherit_unfinished_todos_for_run(
        "todo-isolation-session",
        current.run_id,
        continuation_requested=False,
    ) == []


def test_read_evidence_disambiguates_reused_tool_call_ids_by_query(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("reused-evidence-session")
    for query_id, output in (("query-one", "FIRST"), ("query-two", "SECOND")):
        session_manager.upsert_assistant_message(
            "reused-evidence-session",
            query_id=query_id,
            content=output,
            tool_calls=[{"tool": "read_file", "id": "call-reused", "output": output}],
        )

    history = session_manager.load_session_for_agent("reused-evidence-session")
    second_evidence = history[1]["tool_calls"][0]["evidence_id"]
    restored = session_manager.read_evidence("reused-evidence-session", second_evidence)

    assert restored["content"] == "SECOND"
    assert restored["hash_matches"] is True


def test_legacy_same_hash_tool_calls_keep_distinct_query_provenance(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("same-hash-evidence-session")
    for query_id in ("query-one", "query-two"):
        session_manager.upsert_assistant_message(
            "same-hash-evidence-session",
            query_id=query_id,
            content="SAME",
            tool_calls=[
                {
                    "tool": "read_file",
                    "id": "call-reused",
                    "output": "SAME",
                }
            ],
        )

    history = session_manager.load_session_for_agent(
        "same-hash-evidence-session"
    )
    evidence_ids = [
        message["tool_calls"][0]["evidence_id"]
        for message in history
    ]

    assert len(set(evidence_ids)) == 2
    assert session_manager.get_evidence(
        "same-hash-evidence-session",
        evidence_ids[0],
    )["source_query_id"] == "query-one"
    assert session_manager.get_evidence(
        "same-hash-evidence-session",
        evidence_ids[1],
    )["source_query_id"] == "query-two"


def test_missing_tool_output_is_incomplete_interruption() -> None:
    status, complete = session_manager._evidence_status(
        {
            "output": (
                "Tool execution did not return a result before the agent "
                "finished."
            ),
            "summary_source": "missing_tool_output",
            "is_error": True,
            "completed_at": 1.0,
        },
        {"role": "assistant"},
    )

    assert status == "interrupted"
    assert complete is False


def test_legacy_result_owner_requires_one_occurrence_not_one_id_value(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("ambiguous-owner-session")
    for query_id in ("query-one", "query-two"):
        session_manager.upsert_assistant_message(
            "ambiguous-owner-session",
            query_id=query_id,
            content="done",
            tool_calls=[
                {
                    "tool": "database_sql_execute",
                    "id": "call-shared",
                    "output": "result_id：qr-shared",
                }
            ],
        )

    assert session_manager.result_owner_tool_call(
        "ambiguous-owner-session",
        "qr-shared",
    ) is None


def test_archive_legacy_tool_calls_get_unique_ids_and_are_deleted_with_session(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("archive-evidence-session")
    session_manager.save_message("archive-evidence-session", "user", "head")
    for output in ("secret-one", "secret-two"):
        session_manager.save_message(
            "archive-evidence-session",
            "assistant",
            output,
            tool_calls=[{"tool": "read_file", "output": output}],
        )
    session_manager.save_message("archive-evidence-session", "user", "tail")
    session_manager.middle_trim_history(
        "archive-evidence-session",
        "summary",
        1,
        3,
    )

    history = session_manager.load_session_for_agent("archive-evidence-session")
    calls = [call for message in history for call in message.get("tool_calls") or []]
    assert len({call["id"] for call in calls}) == 2
    assert all(call["evidence_id"].startswith("evidence-") for call in calls)

    session_manager.delete_session("archive-evidence-session")
    session_manager.create_session("archive-evidence-session")
    assert session_manager.load_session("archive-evidence-session") == []


def test_terminal_run_persists_structured_handoff(tmp_path):
    from harness.models import RunOutcome, RunRecord, RunStatus

    session_manager.initialize(tmp_path)
    session_manager.create_session("handoff-session")
    run = RunRecord(
        run_id="run-handoff",
        query_id="query-handoff",
        session_id="handoff-session",
        objective="刷新报告",
    )
    session_manager.upsert_run_state("handoff-session", run.model_dump(mode="json"))
    session_manager.update_todos(
        "handoff-session",
        [
            {"id": "todo-1", "content": "读取数据", "status": "completed"},
            {"id": "todo-2", "content": "写入报告", "status": "pending"},
        ],
        run_id=run.run_id,
    )
    run.transition(RunStatus.RUNNING)
    session_manager.upsert_run_state("handoff-session", run.model_dump(mode="json"))
    run.finish(RunOutcome.CANCELLED, error="client_cancelled")

    saved = session_manager.terminalize_run_state(
        "handoff-session",
        run.run_id,
        run.model_dump(mode="json"),
    )
    handoff = saved["handoff_summary"]

    assert handoff["source_run_id"] == run.run_id
    assert handoff["terminal_status"] == "cancelled"
    assert handoff["completed_todos"] == [
        {"id": "todo-1", "content": "读取数据", "status": "completed"}
    ]


def test_terminal_run_abandons_uncommitted_leases_and_filters_temporary_handoff(tmp_path):
    from harness.models import (
        RunOutcome,
        RunRecord,
        RunStatus,
        VerificationActivation,
    )

    session_manager.initialize(tmp_path)
    session_manager.create_session("terminal-artifacts")
    run = RunRecord(
        run_id="run-terminal",
        query_id="query-terminal",
        session_id="terminal-artifacts",
        objective="更新报告",
        verification_activations=[
            VerificationActivation(
                activation_id="artifact-activation",
                run_id="run-terminal",
                query_id="query-terminal",
                tool_call_id="commit-call",
                tool_name="commit_external_artifact",
                pack="artifact",
                evidence_refs=[
                    {
                        "kind": "artifact_write",
                        "artifact_id": "artifact-temporary",
                        "scope": "scratch",
                        "role": "temporary",
                        "path": "/scratch/validation/check.py",
                        "host_path": "/tmp/check.py",
                        "content_sha256": "sha256:temporary",
                    },
                    {
                        "kind": "artifact_write",
                        "artifact_id": "artifact-delivered",
                        "scope": "external",
                        "role": "candidate",
                        "path": "/outside/report.html",
                        "host_path": "/outside/report.html",
                        "content_sha256": "sha256:committed",
                    },
                    {
                        "kind": "artifact_write",
                        "artifact_id": "artifact-uncommitted",
                        "scope": "external",
                        "role": "candidate",
                        "path": "/outside/draft.html",
                        "host_path": "/outside/draft.html",
                        "content_sha256": "sha256:draft",
                    },
                ],
            )
        ],
    )
    session_manager.upsert_run_state(
        "terminal-artifacts", run.model_dump(mode="json")
    )
    run.transition(RunStatus.RUNNING)
    session_manager.upsert_run_state(
        "terminal-artifacts", run.model_dump(mode="json")
    )
    session_manager.upsert_external_artifact_lease(
        "terminal-artifacts",
        {
            "lease_id": "lease-draft",
            "status": "staged",
            "run_id": run.run_id,
            "query_id": run.query_id,
            "goal_id": "",
            "goal_revision": None,
            "target_path": "/outside/draft.html",
            "staged_path": "/scratch/external/lease-draft/draft.html",
        },
    )
    session_manager.upsert_external_artifact_lease(
        "terminal-artifacts",
        {
            "lease_id": "lease-committed",
            "status": "committed",
            "run_id": run.run_id,
            "query_id": run.query_id,
            "goal_id": "",
            "goal_revision": None,
            "target_path": "/outside/report.html",
            "staged_path": "/scratch/external/lease-committed/report.html",
            "committed_sha256": "sha256:committed",
        },
    )
    delivered = session_manager.register_delivered_artifact(
        "terminal-artifacts",
        target_path="/outside/report.html",
        content_sha256="sha256:committed",
        source_run_id=run.run_id,
        source_query_id=run.query_id,
    )
    session_manager.upsert_external_directory_lease(
        "terminal-artifacts",
        {
            "lease_id": "directory-draft",
            "status": "prepared",
            "run_id": run.run_id,
            "query_id": run.query_id,
            "goal_id": "",
            "goal_revision": None,
            "directory_path": "/outside/project",
            "staged_dir": "/scratch/external-directories/directory-draft",
        },
    )

    incoming = run.model_copy(deep=True)
    incoming.finish(RunOutcome.COMPLETED)
    first = session_manager.terminalize_run_state(
        "terminal-artifacts",
        run.run_id,
        incoming.model_dump(mode="json"),
    )
    second = session_manager.terminalize_run_state(
        "terminal-artifacts",
        run.run_id,
        incoming.model_dump(mode="json"),
    )

    artifact_leases = {
        item["lease_id"]: item
        for item in session_manager.list_external_artifact_leases(
            "terminal-artifacts"
        )
    }
    directory_leases = {
        item["lease_id"]: item
        for item in session_manager.list_external_directory_leases(
            "terminal-artifacts"
        )
    }
    assert artifact_leases["lease-draft"]["status"] == "abandoned"
    assert artifact_leases["lease-committed"]["status"] == "committed"
    assert directory_leases["directory-draft"]["status"] == "abandoned"
    assert first == second
    expected_ref = {"type": "artifact", "id": "artifact-delivered"}
    assert first["handoff_summary"]["artifact_refs"] == [expected_ref]
    assert first["handoff_summary"]["evidence_refs"] == [expected_ref]
    resolved = session_manager.resolve_evidence_ref(
        "terminal-artifacts",
        expected_ref,
    )
    assert resolved is not None
    assert resolved["payload"]["role"] == "candidate"
    assert resolved["content_sha256"] == delivered["content_sha256"]
    assert resolved["payload"]["path"] == delivered["target_path"]


def test_terminal_goal_abandons_goal_revision_drafts_but_keeps_committed_lease(tmp_path):
    from harness.models import GoalRecord, GoalStatus

    session_manager.initialize(tmp_path)
    session_manager.create_session("terminal-goal-leases")
    goal = GoalRecord(
        goal_id="goal-1",
        session_id="terminal-goal-leases",
        objective="完成报告",
        current_run_id="run-1",
        run_ids=["run-1"],
        round=1,
    )
    session_manager.upsert_goal_state(
        "terminal-goal-leases", goal.model_dump(mode="json")
    )
    for lease_id, status in (("goal-draft", "staged"), ("goal-commit", "committed")):
        session_manager.upsert_external_artifact_lease(
            "terminal-goal-leases",
            {
                "lease_id": lease_id,
                "status": status,
                "run_id": "run-1",
                "query_id": "query-1",
                "goal_id": goal.goal_id,
                "goal_revision": goal.objective_revision,
                "target_path": f"/outside/{lease_id}.html",
                "staged_path": f"/scratch/external/{lease_id}/report.html",
                "committed_sha256": "sha256:done" if status == "committed" else None,
            },
        )

    goal.transition(GoalStatus.COMPLETED)
    goal.current_run_id = None
    session_manager.finalize_goal_run_state(
        "terminal-goal-leases",
        goal.model_dump(mode="json"),
        run_id="run-1",
    )

    leases = {
        item["lease_id"]: item
        for item in session_manager.list_external_artifact_leases(
            "terminal-goal-leases"
        )
    }
    assert leases["goal-draft"]["status"] == "abandoned"
    assert leases["goal-commit"]["status"] == "committed"


def test_compact_agent_context_is_scoped_to_source_run(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("agent-context-scope")
    payload = [{"type": "ai", "data": {"content": "old run", "tool_calls": []}}]
    session_manager.update_agent_context_messages(
        "agent-context-scope",
        payload,
        run_id="run-old",
    )

    assert session_manager.get_agent_context_messages(
        "agent-context-scope",
        run_id="run-old",
    ) == payload
    assert session_manager.get_agent_context_messages(
        "agent-context-scope",
        run_id="run-new",
    ) == []


def test_agent_context_usage_update_does_not_rebind_run_snapshot(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("agent-context-rebind")
    payload = [{"type": "ai", "data": {"content": "old run", "tool_calls": []}}]
    session_manager.update_agent_context_state(
        "agent-context-rebind",
        used_tokens=100,
        messages=payload,
        run_id="run-old",
    )

    session_manager.update_agent_context_state(
        "agent-context-rebind",
        used_tokens=120,
        messages=None,
        run_id="run-new",
    )

    assert session_manager.get_agent_context_messages(
        "agent-context-rebind",
        run_id="run-old",
    ) == payload
    assert session_manager.get_agent_context_messages(
        "agent-context-rebind",
        run_id="run-new",
    ) == []


def test_session_summary_projection_is_readable_across_runs(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("summary-projection")
    recent = [{"type": "human", "data": {"content": "recent", "additional_kwargs": {}}}]

    session_manager.update_session_summary_projection(
        "summary-projection",
        summary_text="## Objective\n- continue",
        recent_messages=recent,
        transcript_boundary={"source_query_id": "query-old", "message_count": 4},
        source_run_id="run-old",
        history_ref="/conversation_history/old.md",
        tokens_after=1200,
    )

    projection = session_manager.get_session_summary_projection("summary-projection")
    assert projection is not None
    assert projection["summary_text"] == "## Objective\n- continue"
    assert projection["source_run_id"] == "run-old"
    assert projection["transcript_boundary"] == {
        "source_query_id": "query-old",
        "message_count": 4,
    }
    assert projection["recent_messages"] == recent


def test_clear_messages_removes_run_snapshot_and_session_summary_projection(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("clear-summary-state")
    session_manager.update_agent_context_messages(
        "clear-summary-state",
        [{"type": "human", "data": {"content": "run"}}],
        run_id="run-1",
    )
    session_manager.update_session_summary_projection(
        "clear-summary-state",
        summary_text="summary",
        recent_messages=[],
        transcript_boundary={"source_query_id": "query-1", "message_count": 2},
        source_run_id="run-1",
    )

    session_manager.clear_messages("clear-summary-state")

    assert session_manager.get_agent_context_messages("clear-summary-state") == []
    assert session_manager.get_session_summary_projection("clear-summary-state") is None


def test_upsert_assistant_message_replaces_same_query_draft(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("draft-session")

    session_manager.save_message("draft-session", "user", "查一下销量")
    session_manager.upsert_assistant_message(
        "draft-session",
        query_id="query-1",
        content="先查到一部分",
        tool_calls=[{"id": "call-1", "tool": "database_knowledge_query", "output": "100"}],
        status="running",
    )
    session_manager.upsert_assistant_message(
        "draft-session",
        query_id="query-1",
        content="最终结果",
        tool_calls=[{"id": "call-1", "tool": "database_knowledge_query", "output": "100"}],
        error_notice="模型连接中断",
        status="error",
    )

    history = session_manager.load_session("draft-session")
    assert [message["role"] for message in history] == ["user", "assistant"]
    assistant = history[1]
    assert assistant["query_id"] == "query-1"
    assert assistant["content"] == "最终结果"
    assert assistant["status"] == "error"
    assert assistant["error_notice"] == "模型连接中断"


def test_running_tool_snapshot_becomes_one_final_evidence_record(tmp_path):
    """A ToolCall start event is lifecycle state, not interrupted evidence."""

    session_manager.initialize(tmp_path)
    session_manager.create_session("tool-lifecycle-session")
    session_manager.upsert_assistant_message(
        "tool-lifecycle-session",
        query_id="query-lifecycle",
        content="正在检索",
        tool_calls=[{
            "id": "call-lifecycle",
            "tool": "llamaindex_knowledge_query",
            "input": {"query": "多智能体省 token"},
            "status": "running",
        }],
        status="running",
    )

    session_manager.ensure_tool_call_ids("tool-lifecycle-session")
    pending = session_manager._read_file("tool-lifecycle-session")
    pending_call = pending["messages"][0]["tool_calls"][0]
    assert "evidence_id" not in pending_call
    assert not pending.get("evidence_index")

    session_manager.upsert_assistant_message(
        "tool-lifecycle-session",
        query_id="query-lifecycle",
        content="检索完成",
        tool_calls=[{
            "id": "call-lifecycle",
            "tool": "llamaindex_knowledge_query",
            "input": {"query": "多智能体省 token"},
            "output": "文本命中 3；图片命中 2。",
            "status": "success",
            "completed_at": 123.0,
        }],
        status="completed",
    )

    history = session_manager.load_session_for_agent("tool-lifecycle-session")
    final_call = history[0]["tool_calls"][0]
    persisted = session_manager._read_file("tool-lifecycle-session")
    evidence = list(persisted["evidence_index"].values())

    assert final_call["evidence_id"].startswith("evidence-")
    assert final_call["status"] == "success"
    assert final_call["output_complete"] is True
    assert len(evidence) == 1
    assert evidence[0]["tool_call_id"] == "call-lifecycle"
    assert evidence[0]["status"] == "success"


def test_evidence_migration_keeps_success_id_and_prunes_interrupted_duplicate(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("evidence-migration-session")
    session_manager.upsert_assistant_message(
        "evidence-migration-session",
        query_id="query-migration",
        content="完成",
        tool_calls=[{
            "id": "call-migration",
            "tool": "read_file",
            "output": "最终证据",
            "status": "success",
            "completed_at": 123.0,
            "evidence_id": "evidence-success-existing",
        }],
        status="completed",
    )
    data = session_manager._read_file("evidence-migration-session")
    common = {
        "tool_call_id": "call-migration",
        "tool": "read_file",
        "source_session_id": "evidence-migration-session",
        "source_run_id": "",
        "source_query_id": "query-migration",
        "source_hash": "sha256:legacy",
        "raw_output_ref": {"kind": "session_tool_call"},
        "projection": {"profile": "detailed", "version": "evidence-projection-v1"},
    }
    data["evidence_index"] = {
        "evidence-interrupted-old": {
            **common,
            "evidence_id": "evidence-interrupted-old",
            "status": "interrupted",
            "output_complete": False,
        },
        "evidence-success-existing": {
            **common,
            "evidence_id": "evidence-success-existing",
            "status": "success",
            "output_complete": True,
        },
    }
    session_manager._write_file("evidence-migration-session", data)

    session_manager.ensure_tool_call_ids("evidence-migration-session")
    migrated = session_manager._read_file("evidence-migration-session")
    migrated_call = migrated["display_messages"][0]["tool_calls"][0]

    assert migrated_call["evidence_id"] == "evidence-success-existing"
    assert set(migrated["evidence_index"]) == {"evidence-success-existing"}


def test_message_timestamp_uses_explicit_query_input_time(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("timestamp-session")

    session_manager.save_message(
        "timestamp-session",
        "user",
        "查询输入",
        created_at=1_785_824_745.5,
    )

    history = session_manager.load_session("timestamp-session")
    assert history[0]["created_at"] == 1_785_824_745.5


def test_assistant_draft_upsert_preserves_first_timestamp(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("assistant-timestamp-session")

    session_manager.upsert_assistant_message(
        "assistant-timestamp-session",
        query_id="query-time",
        content="处理中",
        status="running",
    )
    first_timestamp = session_manager.load_session("assistant-timestamp-session")[0]["created_at"]
    session_manager.upsert_assistant_message(
        "assistant-timestamp-session",
        query_id="query-time",
        content="完成",
        status="completed",
    )

    assert session_manager.load_session("assistant-timestamp-session")[0]["created_at"] == first_timestamp


def test_legacy_turn_timestamp_is_projected_from_harness_run(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("legacy-timestamp-session")
    path = session_manager._session_path("legacy-timestamp-session")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["messages"] = [
        {"role": "user", "content": "旧查询"},
        {
            "role": "assistant",
            "content": "旧回答",
            "query_id": "query-legacy-time",
        },
    ]
    data["harness"] = {
        "runs": {
            "run-legacy-time": {
                "query_id": "query-legacy-time",
                "created_at": 1_785_824_745.5,
                "completed_at": 1_785_824_925.5,
            }
        }
    }
    path.write_text(json.dumps(data), encoding="utf-8")

    history = session_manager.load_session("legacy-timestamp-session")
    assert [message["created_at"] for message in history] == [
        1_785_824_745.5,
        1_785_824_925.5,
    ]


def test_reasoning_content_saved_for_plain_assistant(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("plain-session")

    # 为了历史回看，即使没有工具调用也持久化 reasoning_content
    session_manager.save_message(
        "plain-session",
        "assistant",
        "你好",
        reasoning_content="单纯问候也保存推理",
    )

    history = session_manager.load_session("plain-session")
    assistant = history[0]
    assert assistant["reasoning_content"] == "单纯问候也保存推理"


def test_update_and_get_todos(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("todo-session")

    todos = [
        {"id": "todo-1", "content": "step one", "status": "pending"},
        {"id": "todo-2", "content": "step two", "status": "in_progress"},
    ]
    session_manager.update_todos("todo-session", todos)

    loaded = session_manager.get_todos("todo-session")
    assert loaded == todos

    raw = session_manager.get_raw_messages("todo-session")
    assert raw["todos"] == todos


def test_update_and_get_trace(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("trace-session")

    trace = {
        "trace_id": "trace-1",
        "query_id": "query-1",
        "session_id": "trace-session",
        "started_at": 1.0,
        "completed_at": 2.0,
        "status": "completed",
        "spans": [
            {"id": "root", "parent_id": None, "type": "root", "name": "agent.run"}
        ],
    }
    session_manager.update_trace("trace-session", trace, query_id="query-1")

    loaded = session_manager.get_trace("trace-session")
    assert loaded == trace

    raw = session_manager.get_raw_messages("trace-session")
    assert "trace" not in raw
    assert "traces" not in raw

    persisted_session = session_manager._read_file("trace-session")
    assert "trace" not in persisted_session
    assert "traces" not in persisted_session
    trace_state = session_manager.get_trace_state("trace-session")
    assert trace_state["latest_query_id"] == "query-1"
    assert trace_state["latest_trace_id"] == "trace-1"
    assert trace_state["traces"]["query-1"] == trace


def test_update_trace_keeps_history_without_duplicating_latest_trace(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("trace-session-2")

    first = {
        "trace_id": "trace-1",
        "query_id": "query-1",
        "session_id": "trace-session-2",
        "started_at": 1.0,
        "completed_at": 2.0,
        "status": "completed",
        "spans": [],
    }
    second = {
        "trace_id": "trace-2",
        "query_id": "query-2",
        "session_id": "trace-session-2",
        "started_at": 3.0,
        "completed_at": 4.0,
        "status": "completed",
        "spans": [],
    }

    session_manager.update_trace("trace-session-2", first, query_id="query-1")
    session_manager.update_trace("trace-session-2", second, query_id="query-2")

    assert session_manager.get_trace("trace-session-2") == second
    traces = session_manager.get_traces("trace-session-2")
    assert traces["query-1"] == first
    assert traces["query-2"] == second
    trace_sidecar = session_manager._read_trace_file("trace-session-2")
    assert "trace" not in trace_sidecar


def test_update_trace_records_cross_run_cache_continuity(tmp_path, monkeypatch):
    session_manager.initialize(tmp_path)
    session_manager.create_session("trace-cache-continuity")
    metrics: list[tuple[str, int | float]] = []
    monkeypatch.setattr(
        "graph.session_manager.emit_harness_metric",
        lambda _logger, name, **kwargs: metrics.append((name, kwargs["value"])),
    )

    def model_input(
        *,
        span_id: str,
        call_index: int,
        system_hash: str,
        tool_hash: str,
        message_hash: str,
        messages: list[dict],
    ) -> dict:
        return {
            "id": span_id,
            "type": "model_input",
            "started_at": float(call_index),
            "metadata": {"model_call_index": call_index},
            "output": {
                "messages_preview": messages,
                "model_call_contract": {
                    "fingerprints": {
                        "system_prompt_hash": system_hash,
                        "tool_schema_hash": tool_hash,
                        "messages_hash": message_hash,
                    }
                },
            },
        }

    shared_messages = [
        {"role": "human", "content": "first", "chars": 5},
        {"role": "ai", "content": "working", "chars": 7},
    ]
    first = {
        "trace_id": "trace-cache-1",
        "query_id": "query-cache-1",
        "spans": [
            model_input(
                span_id="first-input",
                call_index=0,
                system_hash="system-stable",
                tool_hash="tools-stable",
                message_hash="messages-first",
                messages=[
                    {"role": "system", "content": "stable", "chars": 6},
                    *shared_messages,
                ],
            )
        ],
    }
    second = {
        "trace_id": "trace-cache-2",
        "query_id": "query-cache-2",
        "spans": [
            model_input(
                span_id="second-input",
                call_index=0,
                system_hash="system-stable",
                tool_hash="tools-stable",
                message_hash="messages-second",
                messages=[
                    {"role": "system", "content": "stable", "chars": 6},
                    *shared_messages,
                    {"role": "human", "content": "continue", "chars": 8},
                ],
            )
        ],
    }

    session_manager.update_trace(
        "trace-cache-continuity",
        first,
        query_id="query-cache-1",
    )
    saved = session_manager.update_trace(
        "trace-cache-continuity",
        second,
        query_id="query-cache-2",
    )

    assert saved["cache_continuity"] == {
        "previous_query_id": "query-cache-1",
        "system_prompt_hash_match": True,
        "tool_schema_hash_match": True,
        "messages_hash_match": False,
        "previous_message_count": 2,
        "current_message_count": 3,
        "message_prefix_match_count": 2,
        "message_prefix_ratio": 1.0,
        "stable_boundary_match": True,
        "full_previous_request_prefix_match": True,
    }
    assert metrics == [
        ("cache_continuity_system_prompt_match", 1),
        ("cache_continuity_tool_schema_match", 1),
        ("cache_continuity_message_prefix_ratio", 1.0),
    ]


def test_legacy_embedded_traces_are_migrated_once(tmp_path):
    import json

    session_manager.initialize(tmp_path)
    session_manager.create_session("legacy-trace-session")
    path = session_manager._session_path("legacy-trace-session")
    data = json.loads(path.read_text(encoding="utf-8"))
    trace = {
        "trace_id": "legacy-trace-1",
        "query_id": "legacy-query-1",
        "session_id": "legacy-trace-session",
        "spans": [{"id": "root", "type": "root"}],
    }
    data.update({
        "trace": trace,
        "traces": {"legacy-query-1": trace},
        "latest_query_id": "legacy-query-1",
        "latest_trace_id": "legacy-trace-1",
    })
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert session_manager.list_sessions()[0]["id"] == "legacy-trace-session"
    assert session_manager.load_session("legacy-trace-session") == []
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert "trace" not in migrated
    assert "traces" not in migrated
    assert session_manager.get_trace("legacy-trace-session") == trace
    sidecar = json.loads(
        session_manager._trace_path("legacy-trace-session").read_text(encoding="utf-8")
    )
    assert "trace" not in sidecar
    assert sidecar["traces"] == {"legacy-query-1": trace}


def test_conversation_history_does_not_read_trace_sidecar(tmp_path, monkeypatch):
    from pathlib import Path

    session_manager.initialize(tmp_path)
    session_manager.create_session("fast-history-session")
    session_manager.save_message(
        "fast-history-session",
        "user",
        "只读取消息",
        created_at=1234.5,
    )
    session_manager.update_trace(
        "fast-history-session",
        {"trace_id": "trace-large", "query_id": "query-large", "spans": []},
        query_id="query-large",
    )
    trace_path = session_manager._trace_path("fast-history-session")
    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if path == trace_path:
            raise AssertionError("conversation history must not read the trace sidecar")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    assert session_manager.load_session("fast-history-session") == [
        {"role": "user", "content": "只读取消息", "created_at": 1234.5}
    ]
    assert session_manager.get_raw_messages("fast-history-session")["messages"] == [
        {"role": "user", "content": "只读取消息", "created_at": 1234.5}
    ]


def test_assistant_output_attachments_survive_draft_upserts_and_history_reload(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("attachment-output-session")
    output = {
        "id": "att_generated123",
        "name": "修改版.html",
        "type": "file",
        "mime_type": "text/html",
        "size": 321,
        "source": "generated",
        "sha256": "sha256:abc",
        "derived_from": "att_source123",
        "created_by_run_id": "run-1",
        "created_by_query_id": "query-1",
        "created_by_tool_call_id": "call-generate-1",
        "created_at": 1234.5,
        "download_url": "/api/attachments/att_generated123/download?session_id=attachment-output-session",
        "preview_url": "/api/attachments/att_generated123/preview?session_id=attachment-output-session",
        "preview_mime_type": "image/png",
        "width": 640,
        "height": 480,
    }

    session_manager.upsert_assistant_message(
        "attachment-output-session",
        query_id="query-1",
        content="已生成",
        output_attachments=[output],
        status="running",
    )
    session_manager.upsert_assistant_message(
        "attachment-output-session",
        query_id="query-1",
        content="已生成并验证",
        output_attachments=[output],
        status="completed",
    )

    history = session_manager.load_session("attachment-output-session")
    assert len(history) == 1
    assert history[0]["status"] == "completed"
    restored = history[0]["output_attachments"][0]
    assert {key: restored[key] for key in output} == output


def test_delivered_artifact_registry_resolves_standalone_follow_up_without_scratch(
    tmp_path,
):
    session_manager.initialize(tmp_path)
    session_manager.create_session("artifact-followup-session")
    html_path = tmp_path / "产品配置分析_2026.html"
    js_path = tmp_path / "product-config-charts-2026.js"
    html_path.write_text("<script src='product-config-charts-2026.js'></script>", encoding="utf-8")
    js_path.write_text("const heatmapByYear = {};", encoding="utf-8")
    html_sha = "sha256:" + hashlib.sha256(html_path.read_bytes()).hexdigest()
    js_sha = "sha256:" + hashlib.sha256(js_path.read_bytes()).hexdigest()
    html = session_manager.register_delivered_artifact(
        "artifact-followup-session",
        target_path=str(html_path),
        content_sha256=html_sha,
        source_run_id="run-delivery",
        source_query_id="query-delivery",
        source_goal_id="goal-delivery",
        source_goal_revision=1,
    )
    js = session_manager.register_delivered_artifact(
        "artifact-followup-session",
        target_path=str(js_path),
        content_sha256=js_sha,
        source_run_id="run-delivery",
        source_query_id="query-delivery",
        source_goal_id="goal-delivery",
        source_goal_revision=1,
        related_artifact_ids=[html["artifact_id"]],
    )
    html = session_manager.register_delivered_artifact(
        "artifact-followup-session",
        target_path=html["target_path"],
        content_sha256=html["content_sha256"],
        source_run_id="run-delivery",
        source_query_id="query-delivery",
        source_goal_id="goal-delivery",
        source_goal_revision=1,
        related_artifact_ids=[js["artifact_id"]],
    )
    session_manager.upsert_assistant_message(
        "artifact-followup-session",
        query_id="query-historical",
        content="旧回答曾提到 product-config-charts-2024.js。",
        status="completed",
    )
    session_manager.upsert_assistant_message(
        "artifact-followup-session",
        query_id="query-delivery",
        content=f"已交付 {html_path.name} 和 {js_path.name}",
        status="completed",
    )

    resolved = session_manager.resolve_follow_up_artifacts(
        "artifact-followup-session",
        "这个热力图还是没有更新，补上 2025/2026",
    )

    assert {item["artifact_id"] for item in resolved} == {
        html["artifact_id"],
        js["artifact_id"],
    }
    assert all(not item["target_path"].startswith("/scratch/") for item in resolved)
    read_only = session_manager.resolve_follow_up_artifacts(
        "artifact-followup-session",
        "HTML 中 HUD 配置率数据是多少，用的是哪个 JS？",
    )
    assert {Path(item["target_path"]).name for item in read_only} == {
        "产品配置分析_2026.html",
        "product-config-charts-2026.js",
    }
    assert session_manager.resolve_follow_up_artifacts(
        "artifact-followup-session", "你好"
    ) == []

    session_manager.upsert_assistant_message(
        "artifact-followup-session",
        query_id="query-explanation",
        content="刚才只是解释了设计原则。",
        status="completed",
    )
    assert session_manager.resolve_follow_up_artifacts(
        "artifact-followup-session", "刚才解释不对"
    ) == []


def test_legacy_declared_write_is_hash_bound_and_inherited_by_same_goal(tmp_path):
    from harness.coordinators import HarnessRunCoordinator
    from harness.deterministic_checks import _evaluate_artifact_delivery
    from harness.models import RunStatus

    state = tmp_path / "state"
    report_dir = tmp_path / "reports"
    state.mkdir()
    report_dir.mkdir()
    report = report_dir / "product-config-v2.html"
    content = "<!doctype html><title>V2</title>\n"
    report.write_text(content, encoding="utf-8")

    session_manager.initialize(state)
    session_manager.create_session("legacy-write-backfill-session")
    coordinator = HarnessRunCoordinator(session_manager)
    run, goal = coordinator.start_run(
        session_id="legacy-write-backfill-session",
        query_id="query-legacy-write",
        objective=f"写入 V2 HTML 到 {report}",
        goal_mode=True,
        verification_enabled=False,
    )
    assert goal is not None
    assert str(report) in run.declared_artifact_targets
    coordinator.transition(run, RunStatus.RUNNING)
    session_manager.upsert_assistant_message(
        "legacy-write-backfill-session",
        query_id=run.query_id,
        content=f"已写入 {report}",
        tool_calls=[
            {
                "id": "call-legacy-write",
                "tool": "write_file",
                "input": {
                    "file_path": str(report),
                    "content": content,
                },
                "output": f"Wrote {report}",
                "status": "success",
                "source_run_id": run.run_id,
                "completed_at": 10.0,
            }
        ],
        status="completed",
    )

    backfilled = session_manager.backfill_goal_declared_artifact_writes(
        "legacy-write-backfill-session",
        goal.goal_id,
        goal.objective_revision,
    )

    assert len(backfilled) == 1
    artifact = backfilled[0]
    assert artifact["path"] == str(report.resolve())
    assert artifact["authorized"] is True
    assert artifact["authority_kind"] == "legacy_declared_artifact_backfill"
    assert artifact["permission_grant_id"] == f"declared-artifact:{run.run_id}"
    assert artifact["mutation_receipt_id"] == (
        "legacy-write-backfill:call-legacy-write"
    )
    assert artifact["content_sha256"] == (
        "sha256:" + hashlib.sha256(content.encode()).hexdigest()
    )
    assert (
        session_manager.backfill_goal_declared_artifact_writes(
            "legacy-write-backfill-session",
            goal.goal_id,
            goal.objective_revision,
        )
        == []
    )

    inherited = [
        item["payload"]
        for item in session_manager.resolve_goal_evidence_records(
            "legacy-write-backfill-session",
            goal.goal_id,
            goal.objective_revision,
        )
        if isinstance(item.get("payload"), dict)
    ]
    evaluation = _evaluate_artifact_delivery(
        "artifact_delivery",
        {
            "run_id": "run-same-goal-continuation",
            "goal_id": goal.goal_id,
            "goal_revision": goal.objective_revision,
            "declared_artifact_targets": [str(report)],
            "goal_evidence_records": inherited,
            "permission_grants_authoritative": True,
            "active_permission_grant_ids": [],
            "final_content": f"已交付 {report}",
            "evaluation_phase": "terminal",
        },
    )
    assert evaluation.passed is True

    report.write_text("<!doctype html><title>changed</title>\n", encoding="utf-8")
    changed = _evaluate_artifact_delivery(
        "artifact_delivery",
        {
            "run_id": "run-same-goal-continuation",
            "goal_id": goal.goal_id,
            "goal_revision": goal.objective_revision,
            "declared_artifact_targets": [str(report)],
            "goal_evidence_records": inherited,
            "permission_grants_authoritative": True,
            "active_permission_grant_ids": [],
            "final_content": f"已交付 {report}",
            "evaluation_phase": "terminal",
        },
    )
    assert changed.passed is False
    assert "发生变化" in str(changed.gap)


def test_goal_revision_preserves_only_hash_bound_artifact_evidence(tmp_path):
    from harness.coordinators import HarnessRunCoordinator
    from harness.deterministic_checks import _evaluate_artifact_delivery
    from harness.models import RunStatus

    state = tmp_path / "state"
    report = tmp_path / "report.html"
    content = "<!doctype html><title>stable</title>\n"
    state.mkdir()
    report.write_text(content, encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("revision-artifact-session")
    coordinator = HarnessRunCoordinator(session_manager)
    run, goal = coordinator.start_run(
        session_id="revision-artifact-session",
        query_id="query-write",
        objective=f"写入报告到 {report}",
        goal_mode=True,
        verification_enabled=False,
    )
    assert goal is not None
    coordinator.transition(run, RunStatus.RUNNING)
    session_manager.upsert_assistant_message(
        "revision-artifact-session",
        query_id=run.query_id,
        content=f"已写入 {report}",
        tool_calls=[
            {
                "id": "call-write-stable",
                "tool": "write_file",
                "input": {"file_path": str(report), "content": content},
                "output": f"Wrote {report}",
                "status": "success",
                "source_run_id": run.run_id,
                "completed_at": 10.0,
            }
        ],
        status="completed",
    )
    session_manager.backfill_goal_declared_artifact_writes(
        "revision-artifact-session",
        goal.goal_id,
        1,
    )

    revised = session_manager.update_goal_objective(
        "revision-artifact-session",
        goal.goal_id,
        objective=f"仍写入 {report}，但不要复制依赖",
        expected_revision=1,
        contract=None,
    )

    assert revised["objective_revision"] == 2
    assert revised["evidence_refs"]
    resolved = session_manager.resolve_goal_evidence_records(
        "revision-artifact-session",
        goal.goal_id,
        2,
    )
    artifact = next(item for item in resolved if item["kind"] == "artifact")
    assert artifact["goal_revision"] == 1
    assert artifact["content_sha256"] == (
        "sha256:" + hashlib.sha256(content.encode()).hexdigest()
    )
    inherited_records = [
        {
            **dict(item["payload"]),
            "revision_inherited": item["goal_revision"] < 2,
        }
        for item in resolved
        if isinstance(item.get("payload"), dict)
    ]
    evaluation = _evaluate_artifact_delivery(
        "artifact_delivery",
        {
            "run_id": "run-revision-2",
            "goal_id": goal.goal_id,
            "goal_revision": 2,
            "declared_artifact_targets": [str(report)],
            "goal_evidence_records": inherited_records,
            "permission_grants_authoritative": True,
            "active_permission_grant_ids": [],
            "final_content": f"已交付 {report}",
            "evaluation_phase": "terminal",
        },
    )
    assert evaluation.passed is True


def test_legacy_declared_write_backfill_rejects_symlink_target(tmp_path):
    from harness.coordinators import HarnessRunCoordinator
    from harness.models import RunStatus

    state = tmp_path / "state"
    report_dir = tmp_path / "reports"
    state.mkdir()
    report_dir.mkdir()
    real_report = report_dir / "real-report.html"
    declared_link = report_dir / "product-config-v2.html"
    content = "<!doctype html><title>V2</title>\n"
    real_report.write_text(content, encoding="utf-8")
    declared_link.symlink_to(real_report)

    session_manager.initialize(state)
    session_manager.create_session("legacy-symlink-backfill-session")
    coordinator = HarnessRunCoordinator(session_manager)
    run, goal = coordinator.start_run(
        session_id="legacy-symlink-backfill-session",
        query_id="query-legacy-symlink",
        objective=f"写入 V2 HTML 到 {declared_link}",
        goal_mode=True,
        verification_enabled=False,
    )
    assert goal is not None
    coordinator.transition(run, RunStatus.RUNNING)
    session_manager.upsert_assistant_message(
        "legacy-symlink-backfill-session",
        query_id=run.query_id,
        content=f"已写入 {declared_link}",
        tool_calls=[
            {
                "id": "call-legacy-symlink-write",
                "tool": "write_file",
                "input": {
                    "file_path": str(declared_link),
                    "content": content,
                },
                "output": f"Wrote {declared_link}",
                "status": "success",
                "source_run_id": run.run_id,
            }
        ],
        status="completed",
    )

    assert (
        session_manager.backfill_goal_declared_artifact_writes(
            "legacy-symlink-backfill-session",
            goal.goal_id,
            goal.objective_revision,
        )
        == []
    )


def test_follow_up_registry_rejects_deleted_or_externally_modified_targets(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("artifact-freshness-session")
    target = tmp_path / "report.html"
    target.write_text("v1", encoding="utf-8")
    delivered = session_manager.register_delivered_artifact(
        "artifact-freshness-session",
        target_path=str(target),
        content_sha256="sha256:" + hashlib.sha256(target.read_bytes()).hexdigest(),
        source_run_id="run-delivery",
        source_query_id="query-delivery",
    )
    session_manager.upsert_assistant_message(
        "artifact-freshness-session",
        query_id="query-delivery",
        content="已交付 report.html",
        status="completed",
    )

    target.write_text("changed outside registry", encoding="utf-8")
    assert session_manager.resolve_follow_up_artifacts(
        "artifact-freshness-session", "继续修复这个报告文件"
    ) == []
    stale = session_manager.list_delivered_artifacts(
        "artifact-freshness-session", verify_freshness=True
    )
    assert stale[0]["artifact_id"] == delivered["artifact_id"]
    assert stale[0]["status"] == "stale"
    assert stale[0]["stale_reason"] == "target_hash_mismatch"

    session_manager.mark_delivered_artifact_deleted(
        "artifact-freshness-session",
        target_path=str(target),
        source_run_id="run-delete",
        source_query_id="query-delete",
    )
    tombstone = session_manager.list_delivered_artifacts(
        "artifact-freshness-session"
    )[0]
    assert tombstone["status"] == "deleted"


def test_terminal_scratch_resolution_uses_latest_fresh_delivery_hash(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("terminal-latest-delivery")
    target = tmp_path / "report.html"
    target.write_text("v1", encoding="utf-8")
    first_sha = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
    first = session_manager.register_delivered_artifact(
        "terminal-latest-delivery",
        target_path=str(target),
        content_sha256=first_sha,
        source_run_id="run-1",
        source_query_id="query-1",
    )
    session_manager.upsert_external_artifact_lease(
        "terminal-latest-delivery",
        {
            "lease_id": "lease-old",
            "status": "committed",
            "target_path": str(target),
            "staged_path": "/scratch/external/lease-old/report.html",
            "committed_sha256": first_sha,
            "delivered_artifact_id": first["artifact_id"],
        },
    )

    target.write_text("v2", encoding="utf-8")
    second_sha = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
    session_manager.register_delivered_artifact(
        "terminal-latest-delivery",
        target_path=str(target),
        content_sha256=second_sha,
        source_run_id="run-2",
        source_query_id="query-2",
    )

    resolved = session_manager.resolve_terminal_scratch_reference(
        "terminal-latest-delivery", "/scratch/external/lease-old/report.html"
    )
    assert resolved == {
        "status": "durable",
        "formal_target_path": str(target.resolve()),
        "content_sha256": second_sha,
        "delivered_artifact_id": first["artifact_id"],
    }
