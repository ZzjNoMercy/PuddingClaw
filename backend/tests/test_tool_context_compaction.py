"""DeepAgents Tool Context compaction protocol and concurrency gates."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from graph.middlewares.tool_context_compaction import (
    POLICY_VERSION,
    RAW_OUTPUT_ARTIFACT_KEY,
    ToolContextCompactionMiddleware,
    ToolContextCompactionService,
    ToolContextConfig,
)
from graph.session_manager import SessionManager


def _manager(tmp_path: Path) -> SessionManager:
    manager = SessionManager()
    manager.initialize(tmp_path)
    return manager


def _save_tools(
    manager: SessionManager,
    session_id: str,
    *,
    count: int,
    output_chars: int = 5000,
    tool: str = "read_file",
    first_is_error: bool = False,
) -> None:
    manager.create_session(session_id, metadata={"runtime_mode": "agent"})
    manager.save_message(
        session_id,
        "assistant",
        "工具执行完成",
        tool_calls=[
            {
                "id": f"call-{index}",
                "tool": tool,
                "input": {"path": f"/tmp/{index}.txt"},
                "output": f"result-{index}:" + (str(index % 10) * output_chars),
                "is_error": first_is_error and index == 0,
            }
            for index in range(count)
        ],
    )


def _ids(manager: SessionManager, session_id: str) -> list[str]:
    data = manager.get_raw_messages(session_id)
    return [
        str(tool_call.get("id") or "")
        for message in data.get("messages", [])
        for tool_call in message.get("tool_calls", [])
    ]


def test_legacy_missing_ids_are_stable_persisted_and_mirrored(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    session_id = "legacy-ids"
    manager.create_session(session_id)
    data = manager.get_raw_messages(session_id)
    message = {
        "role": "assistant",
        "content": "done",
        "tool_calls": [{"tool": "terminal", "input": "pwd", "output": "/tmp"}],
    }
    data["messages"] = [json.loads(json.dumps(message))]
    data["display_messages"] = [json.loads(json.dumps(message))]
    manager._write_file(session_id, data)

    assert manager.ensure_tool_call_ids(session_id) is True
    first = manager.get_raw_messages(session_id)
    first_id = first["messages"][0]["tool_calls"][0]["id"]
    assert first_id.startswith("historical_tool_")
    assert first["display_messages"][0]["tool_calls"][0]["id"] == first_id
    assert manager.ensure_tool_call_ids(session_id) is False
    assert _ids(manager, session_id) == [first_id]


def test_candidate_scan_preserves_recent_n_and_short_results(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _save_tools(manager, "candidates", count=6, output_chars=5000)

    candidates = manager.select_tool_context_candidates(
        "candidates",
        min_result_tokens=1000,
        keep_recent=2,
        policy_version=POLICY_VERSION,
    )
    assert [item["tool_call_id"] for item in candidates] == [
        "call-0",
        "call-1",
        "call-2",
        "call-3",
    ]

    assert manager.select_tool_context_candidates(
        "candidates",
        min_result_tokens=10000,
        keep_recent=2,
        policy_version=POLICY_VERSION,
    ) == []
    assert manager.get_tool_context_status("candidates")["status"] == "idle"


def test_recent_window_uses_completion_time_not_call_list_position(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _save_tools(manager, "completion-order", count=3)
    data = manager.get_raw_messages("completion-order")
    calls = data["messages"][0]["tool_calls"]
    calls[0]["completed_at"] = 300
    calls[1]["completed_at"] = 100
    calls[2]["completed_at"] = 200
    manager._write_file("completion-order", data)

    candidates = manager.select_tool_context_candidates(
        "completion-order",
        min_result_tokens=100,
        keep_recent=1,
        policy_version=POLICY_VERSION,
    )
    assert {item["tool_call_id"] for item in candidates} == {"call-1", "call-2"}


def test_result_id_raw_reference_keeps_session_fallback(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    output = 'query complete {"result_id":"qr-2026"}'
    source_hash = manager._tool_context_source_hash(output)
    ref = manager._tool_context_raw_ref("raw-ref", "call-ref", output, source_hash)

    assert ref["kind"] == "result_id"
    assert ref["value"] == "qr-2026"
    assert ref["fallback"] == {
        "kind": "session_tool_call",
        "session_id": "raw-ref",
        "tool_call_id": "call-ref",
        "source_hash": source_hash,
    }


def test_duplicate_tool_ids_fail_closed_without_compaction(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _save_tools(manager, "duplicate", count=2)
    data = manager.get_raw_messages("duplicate")
    data["messages"][0]["tool_calls"][1]["id"] = "call-0"
    manager._write_file("duplicate", data)

    assert manager.select_tool_context_candidates(
        "duplicate",
        min_result_tokens=100,
        keep_recent=0,
        policy_version=POLICY_VERSION,
    ) == []
    assert manager.get_tool_context_status("duplicate")["error"] == "duplicate_tool_call_id"


def test_commit_rejects_duplicate_ids_even_if_session_is_corrupted_after_job_start(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    _save_tools(manager, "duplicate-after-start", count=2)
    candidates = manager.select_tool_context_candidates(
        "duplicate-after-start",
        min_result_tokens=100,
        keep_recent=0,
        policy_version=POLICY_VERSION,
    )
    assert manager.begin_tool_context_job(
        "duplicate-after-start",
        job_id="duplicate-job",
        candidates=candidates,
        policy_version=POLICY_VERSION,
    )
    data = manager.get_raw_messages("duplicate-after-start")
    data["messages"][0]["tool_calls"][1]["id"] = "call-0"
    manager._write_file("duplicate-after-start", data)

    candidate = candidates[0]
    assert manager.complete_tool_context_compaction(
        "duplicate-after-start",
        job_id="duplicate-job",
        tool_call_id=candidate["tool_call_id"],
        source_hash=candidate["source_hash"],
        policy_version=POLICY_VERSION,
        context_output="must not commit",
        method="test",
    ) is False
    calls = manager.get_raw_messages("duplicate-after-start")["messages"][0]["tool_calls"]
    assert all("context_output" not in call for call in calls)


def test_job_commit_keeps_id_count_order_and_ui_output(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _save_tools(manager, "commit", count=3, first_is_error=True)
    before_ids = _ids(manager, "commit")
    before_output = manager.load_session("commit")[0]["tool_calls"][0]["output"]
    candidates = manager.select_tool_context_candidates(
        "commit",
        min_result_tokens=100,
        keep_recent=0,
        policy_version=POLICY_VERSION,
    )
    assert manager.begin_tool_context_job(
        "commit",
        job_id="job-1",
        candidates=candidates,
        policy_version=POLICY_VERSION,
    ) is True

    for candidate in candidates:
        assert manager.complete_tool_context_compaction(
            "commit",
            job_id="job-1",
            tool_call_id=candidate["tool_call_id"],
            source_hash=candidate["source_hash"],
            policy_version=POLICY_VERSION,
            context_output=f"摘要：{candidate['tool_call_id']}",
            method="test",
        ) is True

    assert _ids(manager, "commit") == before_ids
    visible = manager.load_session("commit")[0]["tool_calls"][0]
    assert visible["output"] == before_output
    assert visible["is_error"] is True
    assert visible["context_output"] == "摘要：call-0"
    assert manager.get_raw_messages("commit")["tool_context_revision"] == 3


def test_same_hash_policy_is_idempotent_and_source_change_becomes_stale(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _save_tools(manager, "idempotent", count=1)
    candidates = manager.select_tool_context_candidates(
        "idempotent",
        min_result_tokens=100,
        keep_recent=0,
        policy_version=POLICY_VERSION,
    )
    candidate = candidates[0]
    assert manager.begin_tool_context_job(
        "idempotent",
        job_id="job-ready",
        candidates=candidates,
        policy_version=POLICY_VERSION,
    )
    assert manager.complete_tool_context_compaction(
        "idempotent",
        job_id="job-ready",
        tool_call_id=candidate["tool_call_id"],
        source_hash=candidate["source_hash"],
        policy_version=POLICY_VERSION,
        context_output="ready summary",
        method="test",
    )
    assert manager.update_tool_context_job(
        "idempotent", "job-ready", status="completed", completed_count=1
    )
    assert manager.select_tool_context_candidates(
        "idempotent",
        min_result_tokens=100,
        keep_recent=0,
        policy_version=POLICY_VERSION,
    ) == []

    data = manager.get_raw_messages("idempotent")
    call = data["messages"][0]["tool_calls"][0]
    call["output"] = "new source" * 2000
    manager._write_file("idempotent", data)
    changed = manager.select_tool_context_candidates(
        "idempotent",
        min_result_tokens=100,
        keep_recent=0,
        policy_version=POLICY_VERSION,
    )
    assert len(changed) == 1
    assert manager.begin_tool_context_job(
        "idempotent",
        job_id="job-stale",
        candidates=changed,
        policy_version=POLICY_VERSION,
    )
    stale_candidate = changed[0]
    latest = manager.get_raw_messages("idempotent")
    latest["messages"][0]["tool_calls"][0]["output"] = "changed during job" * 2000
    manager._write_file("idempotent", latest)
    assert manager.complete_tool_context_compaction(
        "idempotent",
        job_id="job-stale",
        tool_call_id=stale_candidate["tool_call_id"],
        source_hash=stale_candidate["source_hash"],
        policy_version=POLICY_VERSION,
        context_output="must not overwrite",
        method="test",
    ) is False
    stale = manager.get_raw_messages("idempotent")["messages"][0]["tool_calls"][0]
    assert stale["context_compaction"]["status"] == "stale"
    assert stale["context_output"] == "ready summary"


def test_job_lease_allows_only_one_owner_and_recovers_after_expiry(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _save_tools(manager, "lease", count=1)
    candidates = manager.select_tool_context_candidates(
        "lease", min_result_tokens=100, keep_recent=0, policy_version=POLICY_VERSION
    )
    assert manager.begin_tool_context_job(
        "lease", job_id="lease-1", candidates=candidates, policy_version=POLICY_VERSION
    )
    assert not manager.begin_tool_context_job(
        "lease", job_id="lease-2", candidates=candidates, policy_version=POLICY_VERSION
    )
    data = manager.get_raw_messages("lease")
    data["tool_context_job"]["updated_at"] = time.time() - 10
    manager._write_file("lease", data)
    assert manager.begin_tool_context_job(
        "lease",
        job_id="lease-3",
        candidates=candidates,
        policy_version=POLICY_VERSION,
        lease_timeout_seconds=1,
    )
    assert manager.get_tool_context_status("lease")["id"] == "lease-3"


def test_service_compacts_in_background_and_does_not_overwrite_new_message(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _save_tools(manager, "service", count=3, output_chars=9000, tool="read_file")
    cfg = ToolContextConfig(background_min_result_tokens=1000, keep_recent_tool_results=0)
    service = ToolContextCompactionService(manager=manager)

    async def run() -> str:
        job_id = await service.enqueue("service", cfg)
        assert job_id
        manager.save_message("service", "user", "压缩期间继续追问")
        manager.save_message(
            "service",
            "assistant",
            "压缩期间新增工具结果",
            tool_calls=[
                {
                    "id": "call-new",
                    "tool": "read_file",
                    "input": {"path": "/tmp/new.txt"},
                    "output": "new evidence",
                    "completed_at": 9999999999,
                }
            ],
        )
        await service.wait("service", timeout=5)
        return job_id

    job_id = asyncio.run(run())
    status = manager.get_tool_context_status("service")
    assert status["id"] == job_id
    assert status["status"] == "completed"
    assert status["metrics"]["tool_context_tokens_after"] < status["metrics"]["tool_context_tokens_before"]
    ready = manager.get_ready_tool_context_outputs("service")
    assert '"path":"/tmp/1.txt"' in ready["call-1"]
    assert '"raw_output_ref"' in ready["call-1"]
    assert manager.load_session("service")[-2]["content"] == "压缩期间继续追问"
    assert manager.load_session("service")[-1]["content"] == "压缩期间新增工具结果"
    assert _ids(manager, "service") == ["call-0", "call-1", "call-2", "call-new"]
    assert manager.get_raw_messages("service")["messages"][-1]["tool_calls"][0]["output"] == "new evidence"


def test_two_workers_only_one_can_acquire_same_session_job(tmp_path: Path) -> None:
    first_manager = _manager(tmp_path)
    second_manager = _manager(tmp_path)
    _save_tools(first_manager, "two-workers", count=3, output_chars=9000, tool="read_file")
    cfg = ToolContextConfig(background_min_result_tokens=1000, keep_recent_tool_results=0)
    first = ToolContextCompactionService(manager=first_manager)
    second = ToolContextCompactionService(manager=second_manager)

    async def run() -> list[str | None]:
        job_ids = await asyncio.gather(
            first.enqueue("two-workers", cfg),
            second.enqueue("two-workers", cfg),
        )
        await asyncio.gather(
            first.wait("two-workers", timeout=5),
            second.wait("two-workers", timeout=5),
        )
        return job_ids

    job_ids = asyncio.run(run())
    assert sum(job_id is not None for job_id in job_ids) == 1
    status = first_manager.get_tool_context_status("two-workers")
    assert status["id"] == next(job_id for job_id in job_ids if job_id is not None)
    assert status["status"] == "completed"


def test_service_no_candidate_is_a_true_noop(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _save_tools(manager, "service-noop", count=2, output_chars=100)
    service = ToolContextCompactionService(manager=manager)
    cfg = ToolContextConfig(background_min_result_tokens=1000, keep_recent_tool_results=0)

    assert asyncio.run(service.enqueue("service-noop", cfg)) is None
    assert manager.get_tool_context_status("service-noop")["status"] == "idle"


def test_disabled_service_returns_before_scan_and_does_not_touch_session(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _save_tools(manager, "service-disabled", count=2, output_chars=9000)
    before = manager.get_raw_messages("service-disabled")
    service = ToolContextCompactionService(manager=manager)
    cfg = ToolContextConfig(
        enabled=False,
        background_min_result_tokens=1000,
        keep_recent_tool_results=0,
    )

    assert asyncio.run(service.enqueue("service-disabled", cfg)) is None
    assert manager.get_raw_messages("service-disabled") == before
    assert manager.get_tool_context_status("service-disabled")["status"] == "idle"


def test_service_candidate_budget_leaves_remainder_for_next_scan(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _save_tools(manager, "service-budget", count=5, output_chars=5000)
    service = ToolContextCompactionService(manager=manager)
    cfg = ToolContextConfig(
        background_min_result_tokens=1000,
        keep_recent_tool_results=0,
        max_candidates_per_job=3,
    )

    async def run() -> None:
        assert await service.enqueue("service-budget", cfg)
        await service.wait("service-budget", timeout=5)

    asyncio.run(run())
    remainder = manager.select_tool_context_candidates(
        "service-budget",
        min_result_tokens=1000,
        keep_recent=0,
        policy_version=POLICY_VERSION,
    )
    assert len(remainder) == 2


def test_candidate_budget_deprioritizes_error_and_user_referenced_results(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _save_tools(manager, "service-priority", count=3, output_chars=5000)
    data = manager.get_raw_messages("service-priority")
    calls = data["messages"][0]["tool_calls"]
    calls[0]["is_error"] = True
    calls[1]["user_referenced"] = True
    manager._write_file("service-priority", data)

    candidates = manager.select_tool_context_candidates(
        "service-priority",
        min_result_tokens=1000,
        keep_recent=0,
        policy_version=POLICY_VERSION,
    )
    assert [item["tool_call_id"] for item in candidates] == ["call-2", "call-0", "call-1"]


def test_llm_fallback_keeps_tool_metadata_and_raw_reference(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _save_tools(
        manager,
        "service-llm",
        count=1,
        output_chars=100000,
        tool="custom_unstructured_blob",
    )

    class FakeSummaryModel:
        prompt = ""

        async def ainvoke(self, _messages: list[Any]) -> AIMessage:
            self.prompt = str(_messages[0].content)
            return AIMessage(content=json.dumps({"call-0": "保留关键事实与数字 2026"}, ensure_ascii=False))

    fake_model = FakeSummaryModel()
    service = ToolContextCompactionService(
        manager=manager,
        model_factory=lambda: fake_model,
    )
    cfg = ToolContextConfig(background_min_result_tokens=1000, keep_recent_tool_results=0)

    async def run() -> None:
        assert await service.enqueue("service-llm", cfg)
        await service.wait("service-llm", timeout=5)

    asyncio.run(run())
    ready = manager.get_ready_tool_context_outputs("service-llm")["call-0"]
    assert "custom_unstructured_blob" in ready
    assert '"path":"/tmp/0.txt"' in ready
    assert '"raw_output_ref"' in ready
    assert "保留关键事实与数字 2026" in ready
    assert "LLM 摘要输入预算裁剪" in fake_model.prompt
    assert len(fake_model.prompt) < 30000
    status = manager.get_tool_context_status("service-llm")
    assert status["metrics"]["llm_summary_count"] == 1


def test_enqueue_returns_while_background_summary_is_still_running(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _save_tools(
        manager,
        "service-nonblocking",
        count=1,
        output_chars=9000,
        tool="custom_unstructured_blob",
    )

    async def run() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingSummaryModel:
            async def ainvoke(self, _messages: list[Any]) -> AIMessage:
                started.set()
                await release.wait()
                return AIMessage(content=json.dumps({"call-0": "后台摘要完成"}, ensure_ascii=False))

        service = ToolContextCompactionService(
            manager=manager,
            model_factory=lambda: BlockingSummaryModel(),
        )
        cfg = ToolContextConfig(background_min_result_tokens=1000, keep_recent_tool_results=0)
        job_id = await service.enqueue("service-nonblocking", cfg)
        assert job_id
        await asyncio.wait_for(started.wait(), timeout=1)
        task = service._tasks["service-nonblocking"]
        assert not task.done()
        assert manager.get_tool_context_status("service-nonblocking")["status"] == "running"
        release.set()
        await service.wait("service-nonblocking", timeout=5)
        assert manager.get_tool_context_status("service-nonblocking")["status"] == "completed"

    asyncio.run(run())


def test_immediate_guard_only_compacts_single_oversized_result_and_keeps_id() -> None:
    cfg = ToolContextConfig(
        immediate_compaction_enabled=True,
        single_tool_trigger_tokens=8000,
    )
    middleware = ToolContextCompactionMiddleware(cfg)

    async def invoke(content: str) -> ToolMessage:
        request = SimpleNamespace(tool_call={"id": "call-guard", "name": "terminal"})

        async def handler(_request: Any) -> ToolMessage:
            return ToolMessage(content=content, tool_call_id="call-guard", name="terminal")

        result = await middleware.awrap_tool_call(request, handler)
        assert isinstance(result, ToolMessage)
        return result

    short = asyncio.run(invoke("short output"))
    assert short.content == "short output"
    assert short.artifact is None

    raw = "important-line\n" + ("x" * 40000) + "\nfinal-line"
    guarded = asyncio.run(invoke(raw))
    assert guarded.tool_call_id == "call-guard"
    assert len(str(guarded.content)) < len(raw)
    assert guarded.artifact[RAW_OUTPUT_ARTIFACT_KEY] == raw
    assert "important-line" in str(guarded.content)
    assert "final-line" in str(guarded.content)


def test_immediate_guard_is_disabled_by_default() -> None:
    middleware = ToolContextCompactionMiddleware(
        ToolContextConfig(single_tool_trigger_tokens=8000)
    )
    raw = "x" * 40_000

    async def invoke() -> ToolMessage:
        request = SimpleNamespace(tool_call={"id": "call-default", "name": "terminal"})

        async def handler(_request: Any) -> ToolMessage:
            return ToolMessage(content=raw, tool_call_id="call-default", name="terminal")

        result = await middleware.awrap_tool_call(request, handler)
        assert isinstance(result, ToolMessage)
        return result

    untouched = asyncio.run(invoke())
    assert untouched.content == raw
    assert untouched.artifact is None


def test_immediate_guard_defers_filesystem_sized_results() -> None:
    cfg = ToolContextConfig(
        immediate_compaction_enabled=True,
        single_tool_trigger_tokens=8000,
    )
    middleware = ToolContextCompactionMiddleware(cfg)
    raw = "x" * 80_001

    async def invoke() -> ToolMessage:
        request = SimpleNamespace(tool_call={"id": "call-large", "name": "fetch_url"})

        async def handler(_request: Any) -> ToolMessage:
            return ToolMessage(content=raw, tool_call_id="call-large", name="fetch_url")

        result = await middleware.awrap_tool_call(request, handler)
        assert isinstance(result, ToolMessage)
        return result

    deferred = asyncio.run(invoke())
    assert deferred.content == raw
    assert deferred.artifact is None


def test_saved_agent_context_does_not_duplicate_raw_tool_artifact() -> None:
    from graph.deepagents_manager import _serialize_agent_context_messages

    serialized = _serialize_agent_context_messages(
        [
            ToolMessage(
                content="model summary",
                tool_call_id="call-artifact",
                artifact={RAW_OUTPUT_ARTIFACT_KEY: "very large raw output", "keep": "metadata"},
            )
        ]
    )
    artifact = serialized[0]["data"]["artifact"]
    assert RAW_OUTPUT_ARTIFACT_KEY not in artifact
    assert artifact["keep"] == "metadata"


def test_model_route_uses_only_ready_entries_without_waiting(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _save_tools(manager, "model-route", count=2)
    candidates = manager.select_tool_context_candidates(
        "model-route", min_result_tokens=100, keep_recent=0, policy_version=POLICY_VERSION
    )
    assert manager.begin_tool_context_job(
        "model-route", job_id="route-job", candidates=candidates, policy_version=POLICY_VERSION
    )
    first = candidates[0]
    assert manager.complete_tool_context_compaction(
        "model-route",
        job_id="route-job",
        tool_call_id=first["tool_call_id"],
        source_hash=first["source_hash"],
        policy_version=POLICY_VERSION,
        context_output="compressed first",
        method="test",
    )
    middleware = ToolContextCompactionMiddleware(ToolContextConfig(), manager=manager)
    observed: list[ToolMessage] = []

    class Request:
        def __init__(self, messages: list[ToolMessage]) -> None:
            self.messages = messages
            self.runtime = SimpleNamespace(context={"session_id": "model-route"})

        def override(self, *, messages: list[ToolMessage]):
            return Request(messages)

    async def run() -> None:
        request = Request(
            [
                ToolMessage(content="raw first", tool_call_id="call-0"),
                ToolMessage(content="raw pending", tool_call_id="call-1"),
            ]
        )

        async def handler(next_request: Request):
            observed.extend(next_request.messages)
            return SimpleNamespace(result=[])

        await middleware.awrap_model_call(request, handler)

    asyncio.run(run())
    assert [(item.tool_call_id, item.content) for item in observed] == [
        ("call-0", "compressed first"),
        ("call-1", "raw pending"),
    ]


def test_effective_context_meter_applies_ready_delta_only_when_enabled(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _save_tools(manager, "meter", count=1, output_chars=12000)
    raw = manager.get_raw_messages("meter")["messages"][0]["tool_calls"][0]["output"]
    manager.update_agent_context_state(
        "meter",
        used_tokens=20000,
        messages=[
            {
                "type": "tool",
                "data": {"content": raw, "tool_call_id": "call-0"},
            }
        ],
    )
    candidates = manager.select_tool_context_candidates(
        "meter", min_result_tokens=100, keep_recent=0, policy_version=POLICY_VERSION
    )
    assert manager.begin_tool_context_job(
        "meter", job_id="meter-job", candidates=candidates, policy_version=POLICY_VERSION
    )
    candidate = candidates[0]
    assert manager.complete_tool_context_compaction(
        "meter",
        job_id="meter-job",
        tool_call_id="call-0",
        source_hash=candidate["source_hash"],
        policy_version=POLICY_VERSION,
        context_output="精简结果",
        method="test",
    )

    enabled_usage = manager.get_effective_agent_context_usage("meter", use_tool_context=True)
    assert enabled_usage < 20000
    assert manager.get_effective_agent_context_usage("meter", use_tool_context=False) == 20000
    saved = manager.get_agent_context_messages("meter")
    assert saved[0]["data"]["content"] == raw


def test_deepagents_tool_context_switch_isolated_from_chat_config(tmp_path: Path, monkeypatch) -> None:
    import config

    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)
    defaults_before = json.loads(json.dumps(config._DEFAULT_CONFIG))
    chat_before = json.loads(json.dumps(config.load_config()["compression"]["middleware"]))

    config.update_settings(
        {
            "compression": {
                "deepagents": {
                    "summarization": {"trigger_tokens": 240000},
                    "tool_context": {
                        "enabled": False,
                        "immediate_compaction_enabled": True,
                        "single_tool_trigger_tokens": 9000,
                        "background_min_result_tokens": 1200,
                        "keep_recent_tool_results": 9,
                    },
                }
            }
        }
    )
    saved = config.load_config()
    assert saved["compression"]["deepagents"]["tool_context"]["enabled"] is False
    assert (
        saved["compression"]["deepagents"]["tool_context"]["immediate_compaction_enabled"]
        is True
    )
    assert "summary_input_tokens" not in saved["compression"]["deepagents"]["summarization"]
    assert saved["compression"]["middleware"] == chat_before
    assert config._DEFAULT_CONFIG == defaults_before


def test_disabled_switch_means_middleware_is_not_registered(tmp_path: Path, monkeypatch) -> None:
    import config
    from graph.deepagents_manager import DeepAgentsAgentManager

    manager = DeepAgentsAgentManager()
    manager.initialize(tmp_path)
    monkeypatch.setattr(
        config,
        "get_deepagents_tool_context_config",
        lambda: {"enabled": False},
    )
    disabled = manager._build_middlewares(project_id=None)
    assert not any(isinstance(item, ToolContextCompactionMiddleware) for item in disabled)

    monkeypatch.setattr(
        config,
        "get_deepagents_tool_context_config",
        lambda: {
            "enabled": True,
            "single_tool_trigger_tokens": 8000,
            "background_min_result_tokens": 1000,
            "keep_recent_tool_results": 12,
        },
    )
    enabled = manager._build_middlewares(project_id=None)
    mounted = [item for item in enabled if isinstance(item, ToolContextCompactionMiddleware)]
    assert len(mounted) == 1
