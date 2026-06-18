"""Cache middlewares：DeepSeek prefix cache 守护与友好裁剪（课程 Ch5 落地）。

与 compression 中间件的本质差异：
- compression.* 把消息中段压缩/摘要替换 → 每次触发都破坏 DeepSeek prefix cache
- cache.* 保护前缀字节不变，只成对删中段消息 → cache 命中率稳定

叠加顺序（从外到内）：
    cache_boundary（最外，observer）
    → tail_trim（日常裁剪主力，cache-friendly）
    → [tool_clear, summarization, compaction]（高阈值兜底，会破 cache）
    → skills_router → task_state

关键实现差异（vs 课程 Ch5 参考版）：
- CacheBoundary 重写为 wrap_model_call 钩子：create_agent 的 system_prompt 只在
  LLM invoke 时 prepend 到 ModelRequest.system_message，不进 state["messages"]，
  before_model 里读不到真实 system 字节。必须在 wrap_model_call 里读 request.system_message。
- TailTrim 做 AI↔Tool 原子配对：DeepSeek/OpenAI 拒绝没有匹配 AIMessage.tool_calls
  的 ToolMessage。中段删除必须保证配对完整性，否则下轮 LLM 调用 400。

使用方式：
1. 把本文件保存为 backend/graph/middlewares/cache.py（如已存在则覆盖）。
2. 确保 backend/graph/middlewares/__init__.py 导出：
       from graph.middlewares.cache import (
           DeepSeekCacheBoundaryMiddleware,
           TailTrimMiddleware,
           build_cache_middlewares,
       )
3. 在 agent.py 中通过 build_cache_middlewares({...}) 注入，推荐阈值见下方注释。

Context Engineering 默认阈值（DeepSeek V4 1M 上下文）：
- max_tokens = 200000  ：1M 窗口的 20%，日常长对话才触发
- head_keep    = 2     ：保护前两条 state 消息（首轮 user + assistant），稳定 prefix cache
- keep_recent  = 30    ：保留最近 15 轮对话（近因窗口），配合 HumanMessage 边界保护
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.messages import RemoveMessage
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.runtime import Runtime

from graph.middlewares.compression import count_tokens_tiktoken

logger = logging.getLogger(__name__)


class DeepSeekCacheBoundaryMiddleware(AgentMiddleware):
    """前缀字节守护观测器（课程 5.3 落地）。

    锚点：ModelRequest.system_message.content 的字节快照。首次 wrap_model_call
    调用时锁定，后续每次校验前缀是否被上游改动。检测到漂移时 logger.warning，
    不阻断 pipeline —— 生产可根据日志统计决定是否升级为强制 raise。

    为什么是 wrap_model_call 而不是 before_model：
    LangChain 1.x create_agent(system_prompt=...) 把 system prompt 存到
    ModelRequest.system_message，仅在调 LLM 时 prepend 到消息列表。state["messages"]
    里从不出现 SystemMessage（除非 SummarizationMiddleware 注入摘要 SystemMessage）。
    要观测真实 cache 锚点字节，必须在 wrap_model_call 里读 request.system_message。

    并发说明：类变量跨所有请求共享。锁定值对同一个 agent cache_key 是确定性的，
    并发首锁是幂等写；_drift_count 非原子，极端并发下可能少计一次，不影响正确性。
    """

    # 类变量：跨请求共享，首次锁定后所有后续请求都能看到
    # _frozen_system_bytes 保留为完整 system 的兼容观测值；真正判断 prefix cache
    # 风险的是 _frozen_static_prefix_bytes。
    _frozen_system_bytes: bytes | None = None
    _frozen_static_prefix_bytes: bytes | None = None
    _drift_count: int = 0
    _dynamic_change_count: int = 0
    _DYNAMIC_BOUNDARY_MARKERS = (
        "<!-- Long-term Memory",
        "## 工具调用提醒",
    )

    def __init__(self) -> None:
        super().__init__()

    @staticmethod
    @staticmethod
    def _to_text(content: Any) -> str:
        """把 SystemMessage.content 转为文本，兼容 list-of-blocks 多模态格式。"""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in content
            )
        return str(content)

    @classmethod
    def _split_static_prefix(cls, content: Any) -> tuple[bytes, bytes]:
        """返回 (完整 system 字节, 静态前缀字节)。

        DeepSeek prefix cache 关注的是从请求开头开始的稳定字节。项目的
        Long-term Memory / Tool Reminder 是刻意放在 prompt 末尾的动态区，
        这部分变化不应被记录为"前缀漂移"。
        """
        text = cls._to_text(content)
        full = text.encode("utf-8", errors="replace")

        marker_positions = [
            text.find(marker)
            for marker in cls._DYNAMIC_BOUNDARY_MARKERS
            if text.find(marker) != -1
        ]
        if not marker_positions:
            return full, full

        boundary = min(marker_positions)
        static_prefix = text[:boundary].encode("utf-8", errors="replace")
        return full, static_prefix

    @classmethod
    def _encode(cls, content: Any) -> bytes:
        """把 SystemMessage.content 编码为字节，兼容历史调用点。"""
        return cls._to_text(content).encode("utf-8", errors="replace")

    def _check_system_drift(self, request: Any) -> None:
        """核心观测逻辑：首次锁定 system 字节，后续校验。"""
        sm = getattr(request, "system_message", None)
        if sm is None or not hasattr(sm, "content"):
            # create_agent(system_prompt=None) 场景，无 cache 锚点可锁
            return

        current_full, current_static = self._split_static_prefix(sm.content)

        if DeepSeekCacheBoundaryMiddleware._frozen_static_prefix_bytes is None:
            DeepSeekCacheBoundaryMiddleware._frozen_system_bytes = current_full
            DeepSeekCacheBoundaryMiddleware._frozen_static_prefix_bytes = current_static
            logger.info(
                "[cache-boundary] 已锁定静态 system 前缀 %d 字节（完整 system %d 字节）",
                len(current_static), len(current_full)
            )
            return

        frozen_static = DeepSeekCacheBoundaryMiddleware._frozen_static_prefix_bytes
        frozen_full = DeepSeekCacheBoundaryMiddleware._frozen_system_bytes or b""

        if current_static != frozen_static:
            DeepSeekCacheBoundaryMiddleware._drift_count += 1
            logger.warning(
                "[cache-boundary] 静态前缀字节漂移 (累计 %d 次): %d -> %d bytes, "
                "DeepSeek prefix cache 可能受影响",
                DeepSeekCacheBoundaryMiddleware._drift_count,
                len(frozen_static),
                len(current_static),
            )
            return

        if current_full != frozen_full:
            DeepSeekCacheBoundaryMiddleware._dynamic_change_count += 1
            logger.info(
                "[cache-boundary] 静态前缀稳定 %d 字节；动态区字节变化 "
                "(累计 %d 次): full %d -> %d bytes",
                len(current_static),
                DeepSeekCacheBoundaryMiddleware._dynamic_change_count,
                len(frozen_full),
                len(current_full),
            )

    def wrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        self._check_system_drift(request)
        return handler(request)

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        self._check_system_drift(request)
        return await handler(request)


class TailTrimMiddleware(AgentMiddleware):
    """cache-friendly 中段裁剪：保前缀 + 保末尾 + 成对删中段（课程 5.4 落地 + 孤儿保护）。

    触发条件：token 总数 > max_tokens 且消息总数 > head_keep + keep_recent。
    裁剪范围：msgs[head_keep : len(msgs) - keep_recent] 的中段。
    删除策略：
      - middle slice 中的消息作为一个残缺风险区整体移除
      - AIMessage(tool_calls=[x]) + 对应 ToolMessage(tool_call_id=x)：**原子成对**
        仅当全组都在 middle slice 内才删；任何一个在保护区（head/recent）则整组保留
      - 无 id 的消息：跳过（保守策略）

    为什么必须原子配对：
    DeepSeek / OpenAI-compatible API 拒绝 "ToolMessage 无对应 AIMessage.tool_calls"
    的请求（400 error）。单纯按 idx 删除会产生孤儿 → 下轮 LLM 调用失败 → 因 agent
    缓存，同 session 后续所有调用都挂。

    关键修复——保存完整的一轮对话再裁剪：
    tail_start 必须落在 HumanMessage 上（任务边界）。如果 tail_start 落在某个
    任务中间（如 tool 输出内部），往前缩到上一个 HumanMessage。这样 LLM 看到的
    tail 区始终是完整的最近任务，不会出现 "保留了 tool 调用过程但丢了最终结论"
    的幻觉根因，也不会因看到半截 tool 调用而反复开展同一任务。

    注意 state 结构：
    create_agent 的 system_prompt 不在 state["messages"] 里（它在 ModelRequest.system_message）。
    state[0] 通常是首条 HumanMessage，head_keep=2 实际保护"首条 HumanMessage + 首条 AIMessage"。
    DeepSeek prefix cache 锚在 system_message + 稳定的前几条 state 消息上，head_keep=2
    是经验值，可根据业务调整；head_keep=1 也 cache-safe，但 head_keep=2 多一条近因冗余。

    Context Engineering 默认阈值（DeepSeek V4 1M 上下文）：
    - max_tokens=200000：1M 窗口的 20%，日常长对话才触发
    - head_keep=2：保护前两条 state 消息（首轮 user + assistant）
    - keep_recent=30：保留最近 15 轮对话（近因窗口），配合 HumanMessage
      边界保护，确保 tail 区始终从完整任务开始。
    """

    def __init__(
        self,
        max_tokens: int = 200000,
        head_keep: int = 2,
        keep_recent: int = 30,
        token_counter=None,
    ) -> None:
        super().__init__()
        self.max_tokens = max_tokens
        self.head_keep = head_keep
        self.keep_recent = keep_recent
        # 复用 compression.count_tokens_tiktoken：DeepSeek tokenizer 优先，tiktoken 降级
        self.token_counter = token_counter or count_tokens_tiktoken

    @staticmethod
    def _extract_tool_call_ids(ai_msg: AIMessage) -> list[str]:
        """从 AIMessage.tool_calls 抽 tool_call_id，兼容 dict / 对象两种格式。"""
        tcs = getattr(ai_msg, "tool_calls", None) or []
        ids: list[str] = []
        for tc in tcs:
            if isinstance(tc, dict):
                cid = tc.get("id")
            else:
                cid = getattr(tc, "id", None)
            if cid:
                ids.append(cid)
        return ids

    def _build_ai_tool_groups(
        self, messages: list[BaseMessage]
    ) -> list[tuple[int, list[int]]]:
        """为每条 tool-calling AIMessage 找它对应的所有 ToolMessage 下标。

        返回 [(ai_idx, [tool_idx, ...]), ...]。ToolMessage 以 tool_call_id 匹配。
        未匹配到的 ToolMessage（孤儿历史残留）不加入任何 group。
        """
        groups: list[tuple[int, list[int]]] = []
        for i, m in enumerate(messages):
            if not isinstance(m, AIMessage):
                continue
            tc_ids = self._extract_tool_call_ids(m)
            if not tc_ids:
                continue
            tc_id_set = set(tc_ids)
            # 下游扫描直到对应 ToolMessage 全部找到（或遇到下一条 AIMessage(tool_calls)）
            child_indices: list[int] = []
            for j in range(i + 1, len(messages)):
                m2 = messages[j]
                if isinstance(m2, AIMessage) and self._extract_tool_call_ids(m2):
                    break  # 下一个 AI 工具调用段落开始，停止搜索
                if isinstance(m2, ToolMessage):
                    m2_cid = getattr(m2, "tool_call_id", None)
                    if m2_cid in tc_id_set:
                        child_indices.append(j)
                        if len(child_indices) == len(tc_id_set):
                            break
            groups.append((i, child_indices))
        return groups

    def before_model(
        self, state: dict[str, Any], runtime: Runtime
    ) -> dict[str, Any] | None:
        messages: list[BaseMessage] = state.get("messages", [])

        # 未超过 token 阈值 → 跳过
        if self.token_counter(messages) <= self.max_tokens:
            return None

        # 消息总数 ≤ 保护区总和 → 无中段可裁
        if len(messages) <= self.head_keep + self.keep_recent:
            return None

        head_end = self.head_keep
        tail_start = len(messages) - self.keep_recent

        # 关键修复：确保 tail 区从 HumanMessage 开始（任务边界）。
        # 如果 tail_start 落在某个任务中间（如 tool 输出内部），往前缩到上一个
        # HumanMessage。这样 LLM 看到的 tail 区始终是完整的最近任务，不会出现
        # "保留了 tool 调用过程但丢了最终结论" 的幻觉根因。
        while tail_start > head_end and not isinstance(messages[tail_start], HumanMessage):
            tail_start -= 1

        groups = self._build_ai_tool_groups(messages)

        removes: list[RemoveMessage] = []
        removed_ids: set[str] = set()
        protected_indices: set[int] = set()
        skipped_pairs = 0  # 观测：多少组因跨保护区边界被保留

        # 步骤 1：原子删除"整组在 middle slice"的 AI+Tool 配对
        for ai_idx, tool_indices in groups:
            if not (head_end <= ai_idx < tail_start):
                continue  # AI 不在 middle
            if not tool_indices:
                # AIMessage 有 tool_calls 但尚无对应 ToolMessage（pending 响应 / 末尾未完成）
                # 若删 AI 会留下孤立的 tool_call 意图，下轮 DeepSeek 会报 400
                skipped_pairs += 1
                protected_indices.add(ai_idx)
                continue
            if any(not (head_end <= ti < tail_start) for ti in tool_indices):
                skipped_pairs += 1
                protected_indices.add(ai_idx)
                protected_indices.update(tool_indices)
                continue  # 至少一个 Tool 在保护区（head/recent）→ 整组保留
            # 整组可删
            ai_msg = messages[ai_idx]
            ai_id = getattr(ai_msg, "id", None)
            if ai_id:
                removes.append(RemoveMessage(id=ai_id))
                removed_ids.add(ai_id)
            for ti in tool_indices:
                tm_id = getattr(messages[ti], "id", None)
                if tm_id:
                    removes.append(RemoveMessage(id=tm_id))
                    removed_ids.add(tm_id)

        # 步骤 2：删除 middle 中其余消息。这里会删除 HumanMessage，避免留下
        # “用户请求仍在、完成过程被删”的残缺历史误导模型。
        for i in range(head_end, tail_start):
            if i in protected_indices:
                continue
            m = messages[i]
            m_id = getattr(m, "id", None)
            if m_id and m_id not in removed_ids:
                removes.append(RemoveMessage(id=m_id))
                removed_ids.add(m_id)

        if not removes:
            # token 超限但没删到任何消息：可能 middle 全是 Human、或全是跨边界组
            logger.warning(
                "[tail-trim] token 超限 (%d > %d) 但中段无可删消息 "
                "(total=%d, head_keep=%d, keep_recent=%d, skipped_pairs=%d)，"
                "cache-aware trim 本次失效，等待 summarize/compaction 兜底",
                self.token_counter(messages), self.max_tokens,
                len(messages), self.head_keep, self.keep_recent, skipped_pairs,
            )
            return None

        logger.info(
            "[tail-trim] 中段裁剪 %d 条 (head_keep=%d, keep_recent=%d, total=%d, "
            "skipped_pairs=%d)",
            len(removes), self.head_keep, self.keep_recent, len(messages),
            skipped_pairs,
        )
        return {"messages": removes}


def build_cache_middlewares(config: dict) -> list:
    """工厂函数：根据 config dict 构建 cache middleware 列表。

    对齐 build_compression_middlewares / build_skills_router_middlewares /
    build_write_middlewares 的签名模式。

    config 格式：
        {
            "enabled": True,
            "cache_boundary": {"enabled": True},
            "tail_trim": {"enabled": True, "max_tokens": 200000,
                          "head_keep": 2, "keep_recent": 30},
        }

    顺序固定：CacheBoundary（observer，最外）→ TailTrim（日常裁剪主力）。
    """
    if not config.get("enabled", True):
        return []

    middlewares: list = []

    cb_cfg = config.get("cache_boundary", {})
    if cb_cfg.get("enabled", True):
        middlewares.append(DeepSeekCacheBoundaryMiddleware())

    tt_cfg = config.get("tail_trim", {})
    if tt_cfg.get("enabled", True):
        middlewares.append(TailTrimMiddleware(
            max_tokens=tt_cfg.get("max_tokens", 200000),
            head_keep=tt_cfg.get("head_keep", 2),
            keep_recent=tt_cfg.get("keep_recent", 30),
        ))

    return middlewares
