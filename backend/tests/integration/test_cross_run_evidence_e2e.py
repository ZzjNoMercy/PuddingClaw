"""End-to-end acceptance path for cross-Run Evidence continuity."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr


class _ScriptedEvidenceModel(BaseChatModel):
    _responses: list[AIMessage] = PrivateAttr()
    _calls: int = PrivateAttr(default=0)

    def __init__(self, responses: list[AIMessage]) -> None:
        super().__init__()
        self._responses = responses

    @property
    def _llm_type(self) -> str:
        return "cross_run_evidence_scripted"

    def bind_tools(self, _tools: list[Any], **_kwargs: Any):
        return self

    def _generate(
        self,
        _messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **_kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager
        response = self._responses[self._calls]
        self._calls += 1
        return ChatResult(generations=[ChatGeneration(message=response)])


def test_followup_reads_saved_sql_evidence_without_replaying_old_tool_events(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from graph import deepagents_manager as manager_module
    from graph.session_manager import session_manager
    from projects.registry import project_registry

    session_manager.initialize(tmp_path)
    project_registry.initialize(tmp_path)
    session_manager.create_session("cross-run-e2e")
    result_dir = tmp_path / "data" / "database-query-results"
    result_dir.mkdir(parents=True)
    artifact = result_dir / "qr-cross-run.jsonl"
    artifact.write_text(
        '{"row":1}\n{"row":2}\n{"row":3}\n',
        encoding="utf-8",
    )
    catalog_dir = result_dir / ".catalog"
    catalog_dir.mkdir()
    (catalog_dir / "qr-cross-run.json").write_text(
        json.dumps(
            {
                "result_id": "qr-cross-run",
                "session_id": "cross-run-e2e",
                "tool_call_id": "call-old-sql",
                "artifact_path": "data/database-query-results/qr-cross-run.jsonl",
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
        "cross-run-e2e",
        query_id="query-old",
        content="旧 Run 已完成 SQL 查询。",
        status="completed",
        tool_calls=[
            {
                "tool": "database_sql_execute",
                "id": "call-old-sql",
                "input": {
                    "generation_id": "gen-old",
                    "validation_receipt_id": "receipt-old",
                },
                "output": (
                    "generation_id：gen-old\nvalidation_receipt_id：receipt-old\n"
                    "sql_sha256：sha256:old\nresult_id：qr-cross-run\npreview only"
                ),
                "completed_at": 1.0,
            }
        ],
    )

    persisted_history = session_manager.load_session_for_agent("cross-run-e2e")
    evidence_id = persisted_history[0]["tool_calls"][0]["evidence_id"]
    read_call = {
        "name": "read_evidence",
        "args": {"evidence_id": evidence_id, "page": 2, "page_size": 2},
        "id": "call-read-evidence",
        "type": "tool_call",
    }
    scripted_model = _ScriptedEvidenceModel(
        [
            AIMessage(content="", tool_calls=[read_call]),
            AIMessage(content="继续读取了第 2 页。"),
        ]
    )

    def create_real_scripted_agent(**kwargs: Any):
        kwargs["model"] = scripted_model
        return create_deep_agent(**kwargs)

    monkeypatch.setattr(
        manager_module,
        "create_deep_agent",
        create_real_scripted_agent,
    )

    async def no_title(_session_id: str):
        return None

    monkeypatch.setattr(manager_module, "_generate_title", no_title)
    runtime = manager_module.DeepAgentsAgentManager()
    runtime.initialize(tmp_path)

    async def collect():
        return [
            event
            async for event in runtime.astream(
                message="继续读取下一页，不要重跑 SQL",
                session_id="cross-run-e2e",
                project_id=None,
                user_id="e2e-user",
            )
        ]

    events = asyncio.run(collect())
    tool_events = [
        event
        for event in events
        if event["event"] in {"tool_start", "tool_end"}
    ]
    assert tool_events
    assert {
        json.loads(event["data"])["id"]
        if isinstance(event["data"], str)
        else event["data"]["id"]
        for event in tool_events
    } == {"call-read-evidence"}
    history = session_manager.load_session("cross-run-e2e")
    new_call = next(
        call
        for message in history
        for call in message.get("tool_calls") or []
        if call.get("id") == "call-read-evidence"
    )
    assert new_call["tool"] == "read_evidence"
    assert '"row": 3' in str(new_call["output"])


def test_real_deep_agent_graph_executes_cross_run_evidence_tool(
    tmp_path: Path,
) -> None:
    from graph.deepagents_manager import DeepAgentsAgentManager
    from graph.session_manager import session_manager
    from tools.read_evidence_tool import ReadEvidenceTool

    session_manager.initialize(tmp_path)
    session_manager.create_session("real-cross-run-e2e")
    result_dir = tmp_path / "data" / "database-query-results"
    catalog_dir = result_dir / ".catalog"
    catalog_dir.mkdir(parents=True)
    artifact = result_dir / "qr-real-cross-run.jsonl"
    artifact.write_text(
        '{"row":1}\n{"row":2}\n{"row":3}\n',
        encoding="utf-8",
    )
    session_manager.upsert_assistant_message(
        "real-cross-run-e2e",
        query_id="query-old",
        content="旧 Run 已完成 SQL 查询。",
        status="completed",
        tool_calls=[
            {
                "tool": "database_sql_execute",
                "id": "call-old-sql",
                "output": "result_id：qr-real-cross-run\npreview only",
                "completed_at": 1.0,
            }
        ],
    )
    history = session_manager.load_session_for_agent(
        "real-cross-run-e2e"
    )
    evidence_id = history[0]["tool_calls"][0]["evidence_id"]
    (catalog_dir / "qr-real-cross-run.json").write_text(
        json.dumps(
            {
                "schema_version": "analytics-query-result-catalog-v1",
                "result_id": "qr-real-cross-run",
                "session_id": "real-cross-run-e2e",
                "tool_call_id": "call-old-sql",
                "source_query_id": "query-old",
                "source_run_id": "",
                "owner_binding_version": "strict-v1",
                "artifact_path": (
                    "data/database-query-results/"
                    "qr-real-cross-run.jsonl"
                ),
                "artifact_format": "jsonl",
                "artifact_sha256": (
                    "sha256:"
                    + hashlib.sha256(artifact.read_bytes()).hexdigest()
                ),
                "row_count": 3,
                "status": "ready",
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(hours=1)
                ).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    graph_messages = DeepAgentsAgentManager._build_messages(
        history,
        "继续读取下一页，不要重跑 SQL",
        session_id="real-cross-run-e2e",
        query_id="query-current",
    )
    read_call = {
        "name": "read_evidence",
        "args": {
            "evidence_id": evidence_id,
            "page": 2,
            "page_size": 2,
        },
        "id": "call-read-real",
        "type": "tool_call",
    }
    model = _ScriptedEvidenceModel(
        [
            AIMessage(content="", tool_calls=[read_call]),
            AIMessage(content="已从历史证据读取第 2 页。"),
        ]
    )
    agent = create_deep_agent(
        model=model,
        tools=[
            ReadEvidenceTool(
                session_id="real-cross-run-e2e",
                workspace_path=str(tmp_path),
            )
        ],
    )

    result = asyncio.run(agent.ainvoke({"messages": graph_messages}))
    evidence_messages = [
        item
        for item in result["messages"]
        if isinstance(item, ToolMessage)
        and item.tool_call_id == "call-read-real"
    ]

    assert len(evidence_messages) == 1
    payload = json.loads(str(evidence_messages[0].content))
    assert payload["rows"] == [{"row": 3}]
    assert payload["raw_result_available"] is True
    assert payload["hash_matches"] is True
