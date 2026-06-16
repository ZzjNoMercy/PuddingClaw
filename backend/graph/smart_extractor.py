"""SmartExtractor — mem0 智能提取节流器，全局单例。

借鉴 Claude Code extractMemories 的三个核心机制：
1. 节流控制：每 N 轮才提取一次（降低成本）
2. 主 Agent 互斥：主 Agent 已通过 save_memory tool 写入则跳过（避免重复）
3. 游标 + 失败重试：失败不丢数据（缓冲区保留，下次重试）

per-session 分桶：Web 服务场景下，每个 session 独立管理节流状态。
状态操作全部在事件循环线程中完成，只有 mem0 I/O 操作放入线程池，无竞态风险。
"""

from dataclasses import dataclass, field
from typing import Any

# 避免循环导入：在模块级别延迟导入 mem0_manager
_mem0_manager = None


def _get_mem0_manager():
    """延迟获取 mem0_manager 单例，避免循环导入和多线程 import lock 问题。"""
    global _mem0_manager
    if _mem0_manager is None:
        from graph.mem0_manager import mem0_manager
        _mem0_manager = mem0_manager
    return _mem0_manager


@dataclass
class _SessionState:
    """单个 session 的节流状态。"""
    turns_since_last: int = 0
    message_buffer: list[dict[str, str]] = field(default_factory=list)
    agent_wrote_this_turn: bool = False


# session 淘汰上限，防止长期运行内存泄漏
_MAX_SESSIONS = 100


class SmartExtractor:
    """mem0 智能提取节流器（全局单例）。"""

    def __init__(self, throttle_every: int = 3) -> None:
        self._throttle_every = throttle_every
        self._sessions: dict[str, _SessionState] = {}

    def _get_state(self, session_id: str) -> _SessionState:
        """获取或创建 session 的节流状态。超过上限时淘汰最早的 session。"""
        if session_id not in self._sessions:
            # LRU 淘汰：超过上限时移除最早插入的 session
            if len(self._sessions) >= _MAX_SESSIONS:
                oldest_key = next(iter(self._sessions))
                del self._sessions[oldest_key]
            self._sessions[session_id] = _SessionState()
        return self._sessions[session_id]

    def mark_agent_wrote(self, session_id: str) -> None:
        """标记本轮主 Agent 已通过 save_memory tool 写入记忆。

        由 chat.py 在检测到 segments 中包含 save_memory tool_call 时调用。
        此方法在事件循环线程中执行，与 async_on_turn_end 的状态操作无竞态。
        """
        state = self._get_state(session_id)
        state.agent_wrote_this_turn = True

    def on_turn_end(
        self, messages: list[dict[str, str]], user_id: str, session_id: str
    ) -> dict[str, Any] | None:
        """每轮对话结束时调用（同步版本，仅用于测试；生产环境请用 async_on_turn_end）。

        返回 mem0 add 结果（如果触发了提取），否则返回 None。
        """
        state = self._get_state(session_id)
        state.message_buffer.extend(messages)
        state.turns_since_last += 1

        # 互斥检测：Agent 已通过 tool 主动写入，跳过本轮
        if state.agent_wrote_this_turn:
            print(f"[SmartExtractor] session={session_id} 跳过——主 Agent 已写入记忆")
            state.agent_wrote_this_turn = False
            # Agent 主动写入时已覆盖缓冲区中所有有价值信息，直接丢弃普通消息
            state.message_buffer.clear()
            state.turns_since_last = 0
            return None

        # 节流控制：未达阈值，缓冲等待
        if state.turns_since_last < self._throttle_every:
            print(f"[SmartExtractor] session={session_id} 节流中（{state.turns_since_last}/{self._throttle_every}轮）")
            return None

        # 达到阈值：先快照缓冲区再清理（防止可变列表引用被 clear 后影响 add 调用方）
        print(f"[SmartExtractor] session={session_id} 触发提取——累积{len(state.message_buffer)}条消息")
        buffer_snapshot = list(state.message_buffer)
        state.message_buffer.clear()
        state.turns_since_last = 0
        try:
            manager = _get_mem0_manager()
            result = manager.add(buffer_snapshot, user_id=user_id)
            print(f"[SmartExtractor] session={session_id} 提取成功")
            return result
        except Exception as e:
            # 失败：恢复缓冲区待重试；设为 throttle_every-1 使下一轮立即触发
            print(f"[SmartExtractor] session={session_id} 提取失败: {e}，缓冲区保留待重试")
            state.message_buffer.extend(buffer_snapshot)
            state.turns_since_last = self._throttle_every - 1
            return None

    async def async_on_turn_end(
        self, messages: list[dict[str, str]], user_id: str, session_id: str
    ) -> dict[str, Any] | None:
        """异步版本：状态操作在事件循环线程中完成，只把 mem0 I/O 放入线程池。

        设计要点：所有对 _sessions 字典和 _SessionState 的读写都在协程中执行（事件循环线程），
        只有 mem0_manager.add() 这个纯 I/O 操作通过 run_in_executor 放入线程池，
        彻底消除跨线程竞态。
        """
        import asyncio

        state = self._get_state(session_id)
        state.message_buffer.extend(messages)
        state.turns_since_last += 1

        # 互斥检测（协程中，无竞态）
        if state.agent_wrote_this_turn:
            print(f"[SmartExtractor] session={session_id} 跳过——主 Agent 已写入记忆")
            state.agent_wrote_this_turn = False
            # Agent 主动写入时已覆盖缓冲区中所有有价值信息，直接丢弃普通消息
            state.message_buffer.clear()
            state.turns_since_last = 0
            return None

        # 节流控制（协程中，无竞态）
        if state.turns_since_last < self._throttle_every:
            print(f"[SmartExtractor] session={session_id} 节流中（{state.turns_since_last}/{self._throttle_every}轮）")
            return None

        # 达到阈值：捕获缓冲区快照后立即清理状态（协程中完成，无竞态）
        print(f"[SmartExtractor] session={session_id} 触发提取——累积{len(state.message_buffer)}条消息")
        buffer_snapshot = list(state.message_buffer)
        state.message_buffer.clear()
        state.turns_since_last = 0

        # 只有 mem0 I/O 放入线程池
        loop = asyncio.get_running_loop()
        try:
            manager = _get_mem0_manager()
            result = await loop.run_in_executor(
                None, manager.add, buffer_snapshot, user_id
            )
            print(f"[SmartExtractor] session={session_id} 提取成功")
            return result
        except Exception as e:
            # 失败：恢复缓冲区（回到协程中操作状态，无竞态）
            print(f"[SmartExtractor] session={session_id} 提取失败: {e}，缓冲区保留待重试")
            state.message_buffer.extend(buffer_snapshot)
            state.turns_since_last = self._throttle_every - 1
            return None

    def cleanup_session(self, session_id: str) -> None:
        """清理已结束 session 的状态，防止内存泄漏。"""
        self._sessions.pop(session_id, None)


# 全局单例：从 config.json 读取 throttle_every，默认 3
def _create_smart_extractor() -> SmartExtractor:
    try:
        from config import get_smart_extractor_config
        cfg = get_smart_extractor_config()
        return SmartExtractor(throttle_every=cfg["throttle_every"])
    except Exception:
        return SmartExtractor()


smart_extractor = _create_smart_extractor()
