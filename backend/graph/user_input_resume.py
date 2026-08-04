"""In-process bridge for generic, structured user-input HITL requests.

The active SSE Run owns the LangGraph checkpoint. Requests use a stable replay
key so a tool-node replay cannot create duplicate cards. This registry is not a
permission channel: it only accepts preference/clarification answers.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any


class UserInputResumeRegistry:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._requests: dict[str, dict[str, Any]] = {}
        self._by_replay_key: dict[str, str] = {}

    def _prune(self, now: float | None = None) -> None:
        cutoff = (now if now is not None else time.time()) - 3600
        for request_id, request in list(self._requests.items()):
            if request.get("status") == "pending" or float(request.get("resolved_at") or 0) >= cutoff:
                continue
            self._requests.pop(request_id, None)
            self._pending.pop(request_id, None)
            replay_key = str(request.get("replay_key") or "")
            if self._by_replay_key.get(replay_key) == request_id:
                self._by_replay_key.pop(replay_key, None)

    @staticmethod
    def _replay_key(*, session_id: str, query_id: str, run_id: str, tool_call_id: str) -> str:
        payload = "\0".join((session_id, query_id, run_id, tool_call_id))
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def create(
        self,
        *,
        session_id: str,
        query_id: str,
        run_id: str,
        goal_id: str,
        goal_revision: int | None,
        tool_call_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._prune()
        replay_key = self._replay_key(
            session_id=session_id,
            query_id=query_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
        )
        existing_id = self._by_replay_key.get(replay_key)
        if existing_id:
            existing = self._requests.get(existing_id)
            if existing is not None:
                return dict(existing)

        run_requests = [
            request
            for request in self._requests.values()
            if request.get("session_id") == session_id
            and request.get("run_id") == run_id
            and request.get("replay_key") != replay_key
        ]
        if len(run_requests) >= 3:
            raise ValueError("每个 Run 最多发起 3 次用户输入请求；请合并问题后再询问。")

        request_id = f"user-input-{hashlib.sha256(replay_key.encode('utf-8')).hexdigest()[:16]}"
        request = {
            "id": request_id,
            "version": 1,
            "type": "user_input",
            "session_id": session_id,
            "query_id": query_id,
            "run_id": run_id,
            "goal_id": goal_id or None,
            "goal_revision": goal_revision,
            "tool_call_id": tool_call_id,
            "replay_key": replay_key,
            "status": "pending",
            "created_at": time.time(),
            **payload,
        }
        self._requests[request_id] = request
        self._by_replay_key[replay_key] = request_id
        self._pending[request_id] = asyncio.get_running_loop().create_future()
        return dict(request)

    async def wait(self, request_id: str) -> dict[str, Any]:
        future = self._pending.get(request_id)
        request = self._requests.get(request_id)
        if future is None:
            if request is not None and isinstance(request.get("decision"), dict):
                return dict(request["decision"])
            return {"action": "cancel", "message": "User input request is no longer active."}
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    @staticmethod
    def _normalize_answers(request: dict[str, Any], answers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        questions = {
            str(item.get("id") or ""): item
            for item in request.get("questions") or []
            if isinstance(item, dict) and item.get("id")
        }
        answer_map: dict[str, dict[str, Any]] = {}
        for raw in answers:
            question_id = str(raw.get("question_id") or "")
            if question_id not in questions or question_id in answer_map:
                raise ValueError(f"未知或重复的问题 id：{question_id or '<empty>'}")
            answer_map[question_id] = {
                "question_id": question_id,
                "option_ids": list(dict.fromkeys(str(item) for item in raw.get("option_ids") or [])),
                "text": str(raw.get("text") or "").strip(),
            }

        normalized: list[dict[str, Any]] = []
        for question_id, question in questions.items():
            answer = answer_map.get(question_id, {"question_id": question_id, "option_ids": [], "text": ""})
            option_ids = answer["option_ids"]
            text = answer["text"]
            valid_options = {
                str(item.get("id") or "")
                for item in question.get("options") or []
                if isinstance(item, dict)
            }
            invalid = [item for item in option_ids if item not in valid_options]
            if invalid:
                raise ValueError(f"问题 {question_id} 包含无效选项：{', '.join(invalid)}")
            kind = str(question.get("type") or "")
            required = bool(question.get("required", True))
            allow_other = bool(question.get("allow_other", False))
            if text and kind != "text" and not allow_other:
                raise ValueError(f"问题 {question_id} 不允许填写其他答案")
            if len(text) > int(question.get("max_length") or 1000):
                raise ValueError(f"问题 {question_id} 的回答过长")
            if kind == "single_select" and len(option_ids) + (1 if text else 0) > 1:
                raise ValueError(f"问题 {question_id} 只能选择一项（固定选项或其他）")
            if kind == "multi_select":
                minimum = int(question.get("min_selections") or (1 if required else 0))
                maximum = question.get("max_selections")
                selection_count = len(option_ids) + (1 if text else 0)
                if selection_count < minimum:
                    raise ValueError(f"问题 {question_id} 至少选择 {minimum} 项")
                if maximum is not None and selection_count > int(maximum):
                    raise ValueError(f"问题 {question_id} 最多选择 {maximum} 项")
            elif required and not option_ids and not text:
                raise ValueError(f"问题 {question_id} 必须回答")
            normalized.append(answer)
        return normalized

    def resolve(self, request_id: str, decision: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        request = self._requests.get(request_id)
        if request is None:
            return None, False
        action = str(decision.get("action") or "")
        if action not in {"submit", "cancel", "agent_decide"}:
            raise ValueError("action 必须是 submit、cancel 或 agent_decide")
        normalized = {"action": action, "answers": []}
        if action == "submit":
            normalized["answers"] = self._normalize_answers(
                request,
                [dict(item) for item in decision.get("answers") or [] if isinstance(item, dict)],
            )
        elif action == "agent_decide" and not request.get("allow_agent_decide", True):
            raise ValueError("此请求不允许由 Agent 决定")

        if request.get("status") == "resolved":
            previous = request.get("decision")
            if previous == normalized:
                return dict(previous), False
            raise RuntimeError("用户输入请求已被不同答案解决")
        if request.get("status") != "pending":
            raise RuntimeError("用户输入请求已不再等待回答")

        future = self._pending.get(request_id)
        if future is None:
            raise RuntimeError("用户输入请求所属 Run 已不再活动")
        request["status"] = "resolved"
        request["resolved_at"] = time.time()
        request["decision"] = normalized
        if not future.done():
            future.set_result(normalized)
        return dict(normalized), True

    def reject_run(self, session_id: str, run_id: str, message: str) -> int:
        count = 0
        for request_id, request in list(self._requests.items()):
            if (
                request.get("session_id") != session_id
                or request.get("run_id") != run_id
                or request.get("status") != "pending"
            ):
                continue
            decision = {"action": "cancel", "answers": [], "reason": message}
            request["status"] = "cancelled"
            request["resolved_at"] = time.time()
            request["decision"] = decision
            future = self._pending.get(request_id)
            if future is not None and not future.done():
                future.set_result(decision)
            count += 1
        return count

    def reject_session(self, session_id: str, message: str) -> int:
        run_ids = {
            str(request.get("run_id") or "")
            for request in self._requests.values()
            if request.get("session_id") == session_id and request.get("status") == "pending"
        }
        return sum(self.reject_run(session_id, run_id, message) for run_id in run_ids if run_id)

    def has_pending_session(self, session_id: str) -> bool:
        """Return whether a live user-input decision belongs to the Session."""

        return any(
            request.get("session_id") == session_id
            and request.get("status") == "pending"
            and request_id in self._pending
            and not self._pending[request_id].done()
            for request_id, request in self._requests.items()
        )

    def get(self, request_id: str) -> dict[str, Any] | None:
        request = self._requests.get(request_id)
        return dict(request) if request else None


user_input_resume_registry = UserInputResumeRegistry()
