from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from graph.llm_input_logger import log_llm_input


def _entry(log_dir: Path) -> dict:
    path = next(log_dir.glob("*.jsonl"))
    return json.loads(path.read_text(encoding="utf-8").splitlines()[-1])


def test_llm_input_log_omits_bodies_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLM_INPUT_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("LLM_INPUT_LOG_BODY_ENABLED", raising=False)
    message = SimpleNamespace(
        content="private user body",
        type="human",
        role="user",
        name="",
        id="message-1",
        tool_call_id="",
        tool_calls=[{"id": "call-1", "name": "query", "args": "private tool args"}],
        additional_kwargs={},
    )

    log_llm_input(
        source="test",
        messages=[message],
        system_message="private system body",
        metadata={"phase": "test", "prompt": "private metadata body"},
    )
    entry = _entry(tmp_path)

    assert entry["body_logging_enabled"] is False
    assert "content" not in entry["system"]
    assert "content" not in entry["messages"][0]
    assert "args" not in entry["messages"][0]["tool_calls"][0]
    assert entry["messages"][0]["content_len"] == len("private user body")
    assert entry["messages"][0]["content_sha256"]
    assert entry["metadata"] == {"phase": "test"}


def test_llm_input_log_body_requires_explicit_debug_flag(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLM_INPUT_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_INPUT_LOG_BODY_ENABLED", "1")
    message = SimpleNamespace(
        content="debug body",
        type="human",
        role="user",
        name="",
        id="message-1",
        tool_call_id="",
        tool_calls=[],
        additional_kwargs={},
    )

    log_llm_input(source="test", messages=[message], system_message="debug system")
    entry = _entry(tmp_path)

    assert entry["body_logging_enabled"] is True
    assert entry["system"]["content"] == "debug system"
    assert entry["messages"][0]["content"] == "debug body"
