"""SessionManager 持久化与 reasoning_content 处理测试。"""

import hashlib
from pathlib import Path

import pytest

from graph.session_manager import session_manager


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
    assert session_manager.get_permission_policy("protected")["approval_mode"] == "strict"


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


def test_load_session_for_agent_excludes_cross_run_tool_output(tmp_path):
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
    assert "tool_calls" not in assistant
    assert assistant["content"] == "现在查询比亚迪 2023 年 5 月销量。"
    assert "历史工具结果摘要" not in assistant["content"]
    assert "205390" not in assistant["content"]


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

    goal.transition(GoalStatus.ACHIEVED)
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
    session_manager.save_message("fast-history-session", "user", "只读取消息")
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
        {"role": "user", "content": "只读取消息"}
    ]
    assert session_manager.get_raw_messages("fast-history-session")["messages"] == [
        {"role": "user", "content": "只读取消息"}
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
        "download_url": "/api/attachments/att_generated123/download?session_id=attachment-output-session",
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
