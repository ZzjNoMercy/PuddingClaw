"""Compression middlewares for LangChain create_agent."""

import collections
import logging
from typing import Any

logger = logging.getLogger(__name__)
import logging as _logging
_tk_logger = _logging.getLogger(__name__)

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, SummarizationMiddleware
from langchain.messages import RemoveMessage
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage, trim_messages
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

# 优先使用 DeepSeek 官方 tokenizer；失败降级到 tiktoken cl100k_base。
_DEEPSEEK_TOKENIZER = None
_TIKTOKEN_ENC = None


def _get_tokenizer():
    """Lazy-load tokenizer. 返回 (encode_fn, name)。

    默认优先使用 DeepSeek 官方 tokenizer（Docker 镜像已预缓存，无需网络）。
    若缓存缺失或加载失败，降级到 tiktoken cl100k_base；再失败则用字符估算。
    如需强制使用 tiktoken，可设置环境变量 USE_TIKTOKEN=1。
    """
    import os

    global _DEEPSEEK_TOKENIZER, _TIKTOKEN_ENC
    if _DEEPSEEK_TOKENIZER is not None:
        return _DEEPSEEK_TOKENIZER, "deepseek"
    if _TIKTOKEN_ENC is not None:
        return _TIKTOKEN_ENC, "tiktoken"

    # 可选：强制 tiktoken
    if os.getenv("USE_TIKTOKEN") == "1":
        _tk_logger.info("USE_TIKTOKEN=1, skip deepseek tokenizer")
    else:
        # 默认：DeepSeek 官方 tokenizer（镜像已预缓存到 /app/.hf_cache）
        try:
            from transformers import AutoTokenizer
            tok = AutoTokenizer.from_pretrained("deepseek-ai/DeepSeek-V3", trust_remote_code=True)
            _DEEPSEEK_TOKENIZER = tok
            _tk_logger.info("token counter: using deepseek-ai/DeepSeek-V3 tokenizer")
            return tok, "deepseek"
        except Exception as e:
            _tk_logger.warning("failed to load deepseek tokenizer (%s), falling back to tiktoken", e)

    # 降级：tiktoken
    try:
        import tiktoken
        _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
        _tk_logger.info("token counter: using tiktoken cl100k_base")
        return _TIKTOKEN_ENC, "tiktoken"
    except Exception as e:
        _tk_logger.warning("failed to load tiktoken (%s), falling back to rough estimate", e)

        class _RoughEncoder:
            def encode(self, text: str) -> list[int]:
                return [0] * max(1, len(text) // 4)

        return _RoughEncoder(), "rough"


def _encode_text(text: str) -> int:
    """统一的文本→token 数接口。"""
    tok, name = _get_tokenizer()
    if name == "deepseek":
        return len(tok.encode(text, add_special_tokens=False))
    return len(tok.encode(text))  # tiktoken / rough


def count_tokens_tiktoken(messages) -> int:
    """Count tokens across messages, using deepseek tokenizer if available else tiktoken cl100k_base.

    升级点：额外计入 tool_calls 的 token（修复 V5 遗漏）。
    """
    import json
    total = 0
    for m in messages:
        content = m.content if hasattr(m, "content") else str(m)
        if isinstance(content, list):
            content = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        if not isinstance(content, str):
            content = str(content)
        total += _encode_text(content)
        # 新增：计入 tool_calls 的 token
        if hasattr(m, "tool_calls") and m.tool_calls:
            for tc in m.tool_calls:
                if isinstance(tc, dict):
                    tc_text = json.dumps(tc, ensure_ascii=False)
                else:
                    tc_text = json.dumps(tc.__dict__, ensure_ascii=False)
                total += _encode_text(tc_text)
    return total


def count_text_tokens(text: str) -> int:
    """计算单段文本的 token 数（供 system prompt 等使用）。"""
    return _encode_text(text)


SUMMARY_PREFIX = "[摘要] "
COMPRESSED_CONTEXT_PREFIX = "[历史对话摘要]"

# 按 tool 类型定制的摘要策略（Context Engineering §6）
TOOL_SUMMARY_PROMPTS: dict[str, str] = {
    "patsnap_search": (
        "你正在整理专利检索结果。请从以下工具输出中提取关键字段，按列表形式输出（每条专利一行）："
        "专利号、标题、申请人、申请日、法律状态、核心摘要（100字以内）。\n\n{tool_output}"
    ),
    "patsnap_fetch": (
        "你正在整理专利详情。请结构化提取以下字段：专利号、标题、申请人、发明人、申请日、公开日、"
        "法律状态、摘要、关键权利要求、附图说明。若某字段缺失请标注为“无”。\n\n{tool_output}"
    ),
    "terminal": (
        "请保留以下终端/命令行输出的关键结论、关键数字、错误信息或来源，用一两句话概括：\n\n{tool_output}"
    ),
    "read_file": (
        "请提炼以下文件内容的核心信息，保留关键事实、配置、代码段或结论，不超过200字：\n\n{tool_output}"
    ),
    "execute_skill": (
        "请总结以下技能执行结果，保留技能指引的核心步骤、关键参数和最终结论：\n\n{tool_output}"
    ),
}
DEFAULT_TOOL_SUMMARY_PROMPT = (
    "用一句中文总结以下工具返回的关键发现（不超过80字），保留数字/文件名/错误信息等决策相关细节：\n\n{tool_output}"
)


def _get_tool_summary_prompt(tool_name: str | None) -> str:
    """根据 tool 名称选择对应的摘要 prompt；未知 tool 使用默认 prompt。"""
    return TOOL_SUMMARY_PROMPTS.get(tool_name or "", DEFAULT_TOOL_SUMMARY_PROMPT)


class ToolResultClearMiddleware(AgentMiddleware):
    """工具结果清除：只摘要"最后一条 HumanMessage 之前"的历史 ToolMessage。

    关键行为（Context Engineering §4.2）：
    1. 只考虑最后一条 HumanMessage 之前的 ToolMessage（当前轮次工具结果不摘要）。
    2. 候选 ToolMessage 数量 > keep_recent 时才触发。
    3. 只摘要原始输出长度 >= min_summary_length（默认 500 字符）的 ToolMessage。
    4. 已带 "[摘要] " 前缀的 ToolMessage 不再二次摘要。
    5. 短输出（< min_summary_length）即使位于历史区域也保留原文。
    6. 触发时通过 runtime.stream_writer 发出 tool_result_clear 自定义事件，
       供 chat.py 持久化到 session.json 并标记 summary_source="tool_result_clear"。

    注意：_summary_cache 为实例级，单实例应绑定单个 AgentManager 生命周期。
    若跨 session 复用，必须确保 tool_call_id 全局唯一（UUID 型），否则会命中脏缓存。
    """

    _CACHE_MAX_SIZE = 500

    def __init__(
        self,
        llm,
        keep_recent_tool_results: int = 10,
        min_summary_length: int = 500,
    ) -> None:
        super().__init__()
        self.llm = llm
        self.keep_recent = keep_recent_tool_results
        self.min_summary_length = min_summary_length
        self._summary_cache: collections.OrderedDict[str, str] = collections.OrderedDict()

    async def _asummarize(self, msg: ToolMessage) -> str:
        """异步 LLM 摘要，按 tool 类型选择 prompt。"""
        cache_key = msg.tool_call_id
        if cache_key in self._summary_cache:
            self._summary_cache.move_to_end(cache_key)
            return self._summary_cache[cache_key]

        tool_name = getattr(msg, "name", "unknown")
        prompt = _get_tool_summary_prompt(tool_name).format(tool_output=str(msg.content))
        try:
            if hasattr(self.llm, "ainvoke"):
                resp = await self.llm.ainvoke([HumanMessage(content=prompt)])
            else:
                # 降级：同步调用（可能阻塞事件循环，仅作兼容）
                resp = self.llm.invoke([HumanMessage(content=prompt)])
            summary = resp.content.strip() if hasattr(resp, "content") else str(resp).strip()
        except Exception as e:
            summary = f"{tool_name} 原始输出已清除（摘要失败：{type(e).__name__}）"
        self._summary_cache[cache_key] = summary
        if len(self._summary_cache) > self._CACHE_MAX_SIZE:
            self._summary_cache.popitem(last=False)
        return summary

    def _find_last_human_index(self, messages: list) -> int:
        """返回最后一条 HumanMessage 的下标；不存在则返回 -1。"""
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], HumanMessage):
                return i
        return -1

    async def abefore_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        messages = state["messages"]
        last_human_idx = self._find_last_human_index(messages)

        # 候选区域：最后一条 HumanMessage 之前的 ToolMessage
        candidate_indices = [
            i for i, m in enumerate(messages)
            if isinstance(m, ToolMessage) and (last_human_idx == -1 or i < last_human_idx)
        ]

        if len(candidate_indices) <= self.keep_recent:
            return None

        if self.keep_recent == 0:
            to_clear_indices = candidate_indices
        else:
            to_clear_indices = candidate_indices[:-self.keep_recent]
        changed = False
        new_messages = list(messages)
        emitted_events: list[dict[str, Any]] = []

        for i in to_clear_indices:
            msg = new_messages[i]
            if not isinstance(msg, ToolMessage):
                continue
            content_str = str(msg.content)
            # 已摘要过，不再处理
            if content_str.startswith(SUMMARY_PREFIX):
                continue
            # 短输出保留原文
            if len(content_str) < self.min_summary_length:
                continue

            summary = await self._asummarize(msg)
            summarized_content = f"{SUMMARY_PREFIX}{summary}"
            new_messages[i] = ToolMessage(
                content=summarized_content,
                tool_call_id=msg.tool_call_id,
                name=getattr(msg, "name", None),
            )
            changed = True
            emitted_events.append({
                "tool_call_id": msg.tool_call_id,
                "tool": getattr(msg, "name", "unknown"),
                "summary": summary,
                "summary_source": "tool_result_clear",
            })

        if not changed:
            return None

        # 发出自定义事件，供外层 chat.py 持久化
        if runtime is not None and hasattr(runtime, "stream_writer"):
            for ev in emitted_events:
                runtime.stream_writer({"type": "tool_result_clear", **ev})

        logger.info(
            "[ToolResultClear] summarized %d historical tool messages (candidates=%d, keep_recent=%d)",
            len(emitted_events), len(candidate_indices), self.keep_recent,
        )
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *new_messages]}

    def before_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        """同步兜底：无法异步调用 LLM，因此仅做无需 LLM 的判断并返回 None。

        create_agent 的 astream/ainvoke 会优先调用 abefore_model；
        同步 invoke/stream 才会走到这里。为保持行为一致，同步场景下不执行 LLM 摘要，
        让 TailTrim/Summarization/Compaction 兜底。
        """
        return None


class MessageTrimMiddleware(AgentMiddleware):
    """硬截断：超 max_tokens 时从头砍，字节级零改动，keep_last 兜底。"""

    def __init__(self, max_tokens: int = 12000, keep_last: int = 10,
                 token_counter=None) -> None:
        super().__init__()
        self.max_tokens = max_tokens
        self.keep_last = keep_last
        self.token_counter = token_counter or count_tokens_tiktoken

    def before_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        messages = state["messages"]
        if self.token_counter(messages) <= self.max_tokens:
            return None
        trimmed = trim_messages(
            messages, strategy="last", max_tokens=self.max_tokens,
            token_counter=self.token_counter, include_system=True, allow_partial=False,
        )
        if len(trimmed) < self.keep_last and len(messages) >= self.keep_last:
            trimmed = messages[-self.keep_last:]
        if len(trimmed) == len(messages):
            return None
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *trimmed]}


# SummarizationMiddleware 的中文 summary_prompt（修复 issue #34517 默认英文模板污染）
# 设计要点：
# 1. 简洁叙述，不用 markdown 段落（避免 LLM 把摘要识别为 user 指令）
# 2. 显式前缀标记历史性质，引导 LLM 当 context 处理而非执行
# 3. 强制保留：技术决策 / 已确认事实 / 错误 / 待办 / 用户偏好
# 4. 输出严格限制在 300 中文字符以内
SUMMARIZATION_PROMPT_ZH = """请把下面的对话历史压缩为一段不超过 300 字的中文叙述性摘要，作为后续对话的背景上下文。

严格要求：
1. 用第三人称叙述（"用户先 X，然后助手 Y"），不要分段、不要小标题、不要列表
2. 必须保留：用户做出的技术决策与配置变更、已确认的关键事实和数据、出现过的错误信息、未完成的待办、用户明确表达的偏好
3. 可以省略：礼貌寒暄、重复确认、无信息量的过渡句
4. 输出必须以"[历史对话摘要] "前缀开头，让后续读者明确这是回顾性内容
5. 严禁加 ## 标题、JSON、代码块、英文段落标识符

对话历史：
{messages}

只输出摘要正文（含前缀），不要任何额外说明。
"""

COMPACTION_SUMMARY_PROMPT = """请将以下对话历史压缩为一段简洁的上下文摘要，保留所有关键事实、决策和结论。
不要遗漏任何用户明确告知的信息。

对话历史：
{history}

输出格式：
[对话摘要]
（直接输出摘要内容，不要加前缀）
"""


class CompactionMiddleware(AgentMiddleware):
    """全局压缩重启：超过 trigger_tokens 时执行完整压缩，作为最后保险丝。

    与 SummarizationMiddleware 的区别：
    - Summarization 把摘要包成 HumanMessage + "Here is a summary" 前缀
    - Compaction   把摘要包成 SystemMessage + "[历史对话摘要]" 前缀，且显式保留原 System

    Context Engineering §4.5 行为：
    - trigger_tokens=500000, keep_recent=8, compact_budget_tokens=120000
    - 保留首条 SystemMessage + 最近 keep_recent 条完整消息
    - 其余消息按 compact_budget_tokens 预算动态截断后生成全局摘要
    - 触发时通过 runtime.stream_writer 发出 compaction 自定义事件，
      供 chat.py 调用 compress_history 归档旧消息并写入 compressed_context
    """

    def __init__(
        self,
        model,
        trigger_tokens: int = 500000,
        keep_recent: int = 8,
        compact_budget_tokens: int = 120000,
        token_counter=None,
        summary_prompt: str = COMPACTION_SUMMARY_PROMPT,
    ) -> None:
        super().__init__()
        self.model = model
        self.trigger_tokens = trigger_tokens
        self.keep_recent = keep_recent
        self.compact_budget_tokens = compact_budget_tokens
        self.token_counter = token_counter or count_tokens_tiktoken
        self.summary_prompt = summary_prompt

    def _budget_truncate_messages(self, messages: list) -> str:
        """按 compact_budget_tokens 预算动态截断消息，生成摘要输入文本。

        截断规则（Context Engineering §4.5）：
        - HumanMessage / AIMessage：完整保留
        - ToolMessage ≤ 2K tokens：完整保留
        - ToolMessage 2K-20K tokens：保留约 5K 字符
        - ToolMessage > 20K tokens：保留约 10K 字符
        - 超出预算时截断并追加 "[更多历史已省略]"
        """
        budget = self.compact_budget_tokens
        parts: list[str] = []
        used_tokens = 0
        truncated_any = False

        for m in messages:
            role = getattr(m, "type", "unknown").upper()
            content = str(m.content)
            is_tool = isinstance(m, ToolMessage)

            if not is_tool:
                # Human / AI 消息完整保留
                msg_tokens = self.token_counter([m])
                if used_tokens + msg_tokens > budget:
                    truncated_any = True
                    break
                parts.append(f"[{role}]: {content}")
                used_tokens += msg_tokens
                continue

            # ToolMessage 按长度动态截断
            msg_tokens = self.token_counter([m])
            if msg_tokens <= 2000:
                keep_content = content
            elif msg_tokens <= 20000:
                keep_content = content[:5000]
            else:
                keep_content = content[:10000]

            # 再次按 token 预算检查
            truncated_msg = ToolMessage(content=keep_content, tool_call_id=getattr(m, "tool_call_id", ""))
            truncated_tokens = self.token_counter([truncated_msg])
            if used_tokens + truncated_tokens > budget:
                truncated_any = True
                break
            parts.append(f"[{role}]: {keep_content}")
            used_tokens += truncated_tokens

        if truncated_any:
            parts.append("[更多历史已省略]")
        return "\n".join(parts)

    async def abefore_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        messages = state["messages"]
        total_tokens = self.token_counter(messages)
        if total_tokens <= self.trigger_tokens:
            return None

        # 分离首条 SystemMessage，压缩完成后放回列表最前
        if messages and isinstance(messages[0], SystemMessage):
            system_msg = messages[0]
            rest = messages[1:]
        else:
            system_msg = None
            rest = messages

        if self.keep_recent == 0:
            to_compact = rest
            recent = []
        elif len(rest) > self.keep_recent:
            to_compact = rest[:-self.keep_recent]
            recent = rest[-self.keep_recent:]
        else:
            # 剩余消息不足 keep_recent，压缩无意义
            return None

        if not to_compact:
            return None

        history_text = self._budget_truncate_messages(to_compact)
        prompt = self.summary_prompt.format(history=history_text)
        try:
            if hasattr(self.model, "ainvoke"):
                summary_text = (await self.model.ainvoke([HumanMessage(content=prompt)])).content
            else:
                summary_text = self.model.invoke([HumanMessage(content=prompt)]).content
        except Exception as e:
            # 压缩失败降级：跳过本次压缩，让 TailTrim/Summarization 兜底
            logger.warning("[CompactionMiddleware] 摘要失败降级: %s: %s", type(e).__name__, e)
            return None

        compaction_msg = SystemMessage(content=f"{COMPRESSED_CONTEXT_PREFIX}\n{summary_text}")
        new_messages = []
        if system_msg:
            new_messages.append(system_msg)
        new_messages.append(compaction_msg)
        new_messages.extend(recent)

        # 发出自定义事件，供外层 chat.py 归档旧消息并写入 compressed_context
        if runtime is not None and hasattr(runtime, "stream_writer"):
            runtime.stream_writer({
                "type": "compaction",
                "summary": summary_text,
                "num_to_remove": len(to_compact),
            })

        logger.info(
            "[CompactionMiddleware] compacted %d messages into 1 summary (total %d -> %d tokens estimated)",
            len(to_compact), total_tokens, self.token_counter(new_messages),
        )
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *new_messages]}

    def before_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        """同步兜底：同 ToolResultClearMiddleware，同步场景下不执行 LLM 摘要。"""
        return None


def build_compression_middlewares(llm, config: dict) -> list:
    """工厂函数：根据 config dict 构建压缩 middleware 列表。

    Context Engineering 顺序：Clear → Summarize → Compaction
    （MessageTrim 已由 cache.TailTrimMiddleware 接管）。

    config 格式：
        {
            "enabled": True,
            "tool_clear":    {"keep_recent": 10, "min_summary_length": 500},
            "summarization": {"enabled": True, "trigger_tokens": 200000, "keep_messages": 10},
            "compaction":    {"enabled": True, "trigger_tokens": 500000, "keep_recent": 8,
                              "compact_budget_tokens": 120000},
        }

    阈值设计（DeepSeek V4 1M 上下文）：
    - TailTrim(200K) 日常 cache-friendly 裁剪
    - ToolResultClear(>10 条历史 tool) 轻量摘要
    - Summarization(200K) 叙述性摘要
    - Compaction(500K) 最后保险丝
    """
    if not config.get("enabled", True):
        return []

    middlewares: list = []

    # Layer 1: ToolResultClear（只摘要最后一条 HumanMessage 之前的历史 tool）
    tool_clear_cfg = config.get("tool_clear", {})
    middlewares.append(ToolResultClearMiddleware(
        llm=llm,
        keep_recent_tool_results=tool_clear_cfg.get("keep_recent", 10),
        min_summary_length=tool_clear_cfg.get("min_summary_length", 500),
    ))

    # Layer 2: Summarization（200K 触发，叙述性中文摘要）
    sum_cfg = config.get("summarization", {})
    if sum_cfg.get("enabled", True):
        sum_kwargs = dict(
            model=llm,
            trigger=("tokens", sum_cfg.get("trigger_tokens", 200000)),
            keep=("messages", sum_cfg.get("keep_messages", 10)),
        )
        # 默认启用中文 prompt，可通过 config 关闭回退到 LangChain 默认英文模板
        if sum_cfg.get("use_chinese_prompt", True):
            sum_kwargs["summary_prompt"] = SUMMARIZATION_PROMPT_ZH
        middlewares.append(SummarizationMiddleware(**sum_kwargs))

    # Layer 3: (已移除) MessageTrim → 由 cache.TailTrimMiddleware 接管硬截断职责
    # 保留 MessageTrimMiddleware 类定义供外部/测试使用，默认装配不再引入

    # Layer 4: Compaction（500K 最后保险丝，全局 reset）
    comp_cfg = config.get("compaction", {})
    if comp_cfg.get("enabled", True):
        middlewares.append(CompactionMiddleware(
            model=llm,
            trigger_tokens=comp_cfg.get("trigger_tokens", 500000),
            keep_recent=comp_cfg.get("keep_recent", 8),
            compact_budget_tokens=comp_cfg.get("compact_budget_tokens", 120000),
        ))

    return middlewares
