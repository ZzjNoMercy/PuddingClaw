"""In-process HITL bridge for logical dataset merge-rule selection."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any


class LogicalDatasetResumeRegistry:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._requests: dict[str, dict[str, Any]] = {}

    def create(self, *, session_id: str, query_id: str, tool_call_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = f"concat-rule-{uuid.uuid4().hex[:12]}"
        request = {
            "id": request_id,
            "type": "logical_dataset_rule",
            "session_id": session_id,
            "query_id": query_id,
            "tool_call_id": tool_call_id,
            "status": "pending",
            "created_at": time.time(),
            **payload,
        }
        self._requests[request_id] = request
        self._pending[request_id] = asyncio.get_running_loop().create_future()
        return dict(request)

    async def wait(self, request_id: str) -> dict[str, Any]:
        future = self._pending.get(request_id)
        if future is None:
            return {"action": "cancel", "message": "Logical dataset request is no longer active."}
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    def get(self, request_id: str) -> dict[str, Any] | None:
        request = self._requests.get(request_id)
        return dict(request) if request is not None else None

    def resolve(self, request_id: str, decision: dict[str, Any]) -> dict[str, Any] | None:
        request = self._requests.get(request_id)
        future = self._pending.get(request_id)
        if request is None or future is None or request.get("status") != "pending":
            return None
        if decision.get("action") == "cancel":
            normalized = {"action": "cancel"}
        else:
            operation = str(request.get("operation") or "create")
            candidates = {str(item.get("asset_id")): item for item in request.get("candidates") or [] if isinstance(item, dict)}
            source_asset_ids = [str(item) for item in decision.get("source_asset_ids") or []]
            baseline_asset_id = str(decision.get("baseline_asset_id") or "")
            schema_mode = str(decision.get("schema_mode") or "strict")
            name = str(decision.get("name") or "").strip()
            description = str(decision.get("description") or "").strip()
            tags = [str(item).strip() for item in decision.get("tags") or [] if str(item).strip()]
            preferred_intents = [str(item).strip() for item in decision.get("preferred_intents") or [] if str(item).strip()]
            if not name:
                raise ValueError("请填写逻辑数据集名称。")
            if operation == "append":
                if not request.get("target_asset_id"):
                    raise ValueError("追加逻辑数据集缺少目标数据集。")
                if not source_asset_ids:
                    raise ValueError("请至少选择一张要追加的原始表。")
                if str(request["target_asset_id"]) in source_asset_ids:
                    raise ValueError("目标逻辑数据集不是追加来源；请只选择新的原始表。")
                baseline_asset_id = str(request["target_asset_id"])
            elif len(source_asset_ids) < 2 or baseline_asset_id not in source_asset_ids:
                raise ValueError("请填写名称，并选择至少两张表及其中一张基准表。")
            if any(asset_id not in candidates for asset_id in source_asset_ids):
                raise ValueError("选择了当前确认卡之外的数据资产。")
            if schema_mode not in {"strict", "baseline_fill_missing", "union_fill_missing"}:
                raise ValueError("不支持的字段合并策略。")
            ordered_ids = (
                source_asset_ids
                if operation == "append"
                else [baseline_asset_id, *[asset_id for asset_id in source_asset_ids if asset_id != baseline_asset_id]]
            )
            normalized = {
                "action": "confirm",
                "dataset_rule": {
                    "name": name,
                    "description": description,
                    "tags": tags,
                    "operation": operation,
                    "target_asset_id": request.get("target_asset_id") or "",
                    "baseline_asset_id": baseline_asset_id,
                    "source_asset_ids": ordered_ids,
                    "schema_mode": schema_mode,
                    "preferred_intents": preferred_intents,
                    "direct_source_allowed": bool(decision.get("direct_source_allowed", True)),
                },
            }
        request["status"] = "resolved"
        request["resolved_at"] = time.time()
        request["decision"] = normalized
        if not future.done():
            future.set_result(normalized)
        return normalized

    def reject_session(self, session_id: str, message: str) -> int:
        """Reject all live logical-dataset decisions for a cancelled Session."""

        count = 0
        for request_id, request in list(self._requests.items()):
            if request.get("session_id") == session_id and request.get("status") == "pending":
                if self.resolve(request_id, {"action": "cancel", "message": message}) is not None:
                    count += 1
        return count

    def has_pending_session(self, session_id: str) -> bool:
        """Return whether a live logical-dataset decision belongs to the Session."""

        return any(
            request.get("session_id") == session_id
            and request.get("status") == "pending"
            and request_id in self._pending
            and not self._pending[request_id].done()
            for request_id, request in self._requests.items()
        )


logical_dataset_resume_registry = LogicalDatasetResumeRegistry()
