"""Deep Research Tool — spawn 独立 sub-agent 处理大输入研究任务。

设计要点：
- 用户消息匹配"研究/综述/分析/深入了解/帮我看 X"等场景时，主 agent 应优先调用此工具
- 子 agent 拥有独立的 messages 上下文，主 agent 不会看到子 agent 的中间步骤和原始数据
- 子 agent 工具集仅 read_file + terminal + fetch_url（read-only 类），不含任何写入或递归 deep_research
- 复用主 agent 的 LLM 实例（避免双初始化）
- 复用 compression middleware 链（让子 agent 自己也节流）
- 失败降级：子 agent 异常时返回错误字符串，不抛异常给主 agent
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


RESEARCH_SUBAGENT_SYSTEM_PROMPT = """你是研究子 agent，基于 query 和 scope 收集信息并产出结构化摘要。

工作流程：
1. 调用 read_file / terminal / fetch_url 收集证据（建议 4-6 次，最多 10 次）
2. 产出 400-500 字符的中文摘要（**少于 400 字符视为不合格**）
3. 引用具体证据（文件:行号 / URL / 命令输出）

输出格式（必须包含以下四部分）：
[发现] 核心发现（100-150 字）
[证据] 关键证据点（150-200 字）
[结论] 一句话回答（50-100 字）
[工具调用] X/10 次

质量标准：
- 总字符数必须在 400-500 之间
- 每个部分必须有实质内容，不能只有标题
- 证据必须具体（不能只说"文件中提到"，要说"cache.py:88 行"）
"""


# 子 agent 子工具最大调用次数（防止失控循环消耗 token）
_SUBAGENT_TOOL_CALL_LIMIT = 10

# 摘要最大返回字符数（主 agent 看到的内容）
_SUMMARY_MAX_CHARS = 500


class DeepResearchInput(BaseModel):
    query: str = Field(
        description="要研究的具体问题，例如 '这个目录下的 markdown 主要讲什么' 或 '为什么这个 log 里会有 OOM'"
    )
    scope: str = Field(
        description="研究范围的自然语言描述，可包含文件路径、目录、URL、glob 模式。例如 'docs/ 下所有 md' 或 'logs/app.log 最后 200 行' 或 'https://example.com/spec'"
    )


class DeepResearchTool(BaseTool):
    """子 agent 隔离的深度研究工具。

    调用语义：
    - 主 agent 把 query + scope 传进来
    - 内部构造独立 create_agent + 三个 read-only 工具
    - 子 agent 自主完成多步检索 + 综合
    - 返回 ≤500 字符的结构化摘要
    """

    name: str = "deep_research"
    description: str = (
        "Spawn an isolated sub-agent to perform deep research on a topic involving "
        "multiple files, large files, or web sources. The sub-agent runs in a separate "
        "context and only returns a 500-char structured summary. "
        "USE THIS when the user asks to: research/analyze/summarize multiple files, "
        "investigate logs, study a project structure, or read & synthesize web pages. "
        "DO NOT use for single small file reads (use read_file directly) or trivial questions."
    )
    args_schema: Type[BaseModel] = DeepResearchInput
    risk_level: str = "safe"
    base_dir: str = ""

    def _run(self, query: str, scope: str) -> str:
        """主入口：构造并执行子 agent，返回摘要字符串。"""
        logger.info("[deep_research] start: query=%.60s, scope=%.60s", query, scope)

        try:
            summary = self._invoke_subagent(query, scope)
            truncated = summary[:_SUMMARY_MAX_CHARS]
            logger.info(
                "[deep_research] done: produced %d chars (truncated to %d)",
                len(summary), len(truncated),
            )
            return f"[deep_research result]\n{truncated}"
        except Exception as e:
            logger.warning("[deep_research] failed: %s: %s", type(e).__name__, e)
            return f"[deep_research error] sub-agent failed: {type(e).__name__}: {e}"

    def _invoke_subagent(self, query: str, scope: str) -> str:
        """构造独立子 agent 并 invoke 一次。"""
        # 延迟 import 防循环依赖
        from langchain.agents import create_agent
        from langchain_core.messages import HumanMessage
        from graph.agent import agent_manager
        from graph.middlewares import build_compression_middlewares
        from config import get_middleware_config
        from tools import get_tools_by_categories

        base_dir = Path(self.base_dir)

        # 拿主 agent 的 llm 实例（已配置好 DeepSeek + temperature + streaming）
        llm = agent_manager._llm
        if llm is None:
            raise RuntimeError("agent_manager._llm not initialized; deep_research requires main agent to be initialized first")

        # 构造子 agent 工具集：只保留 read-only 工具，按 name 去重
        # 注意：get_tools_by_categories({'knowledge'}) 实现上会隐式包含 core，
        # 所以 core+knowledge 拼接会产生 read_file/terminal 重复，必须按 name 去重
        # 同时严格排除 write_file 和 deep_research 自身（防递归）
        _ALLOWED_SUB_TOOL_NAMES = ("read_file", "terminal", "fetch_url", "search_knowledge_base")
        all_candidates = get_tools_by_categories(base_dir, {"core"}) + \
                         get_tools_by_categories(base_dir, {"knowledge"})
        seen_names: set[str] = set()
        sub_tools = []
        for tt in all_candidates:
            if tt.name in _ALLOWED_SUB_TOOL_NAMES and tt.name not in seen_names:
                sub_tools.append(tt)
                seen_names.add(tt.name)

        # 子 agent 的 middleware：复用主 agent 的 compression 链
        # 不使用 ModelCallLimitMiddleware，因为：
        # 1. recursion_limit: 100 已提供足够保护
        # 2. system prompt 明确要求最多 10 次工具调用
        # 3. ModelCallLimitMiddleware 的错误信息会干扰正常输出
        compression_mws = build_compression_middlewares(llm, get_middleware_config())
        mws = list(compression_mws)

        logger.info("[deep_research] subagent starting with %d tools, recursion_limit=100, tool_call_limit=%d",
                    len(sub_tools), _SUBAGENT_TOOL_CALL_LIMIT)

        sub_agent = create_agent(
            model=llm,
            tools=sub_tools,
            system_prompt=RESEARCH_SUBAGENT_SYSTEM_PROMPT,
            middleware=mws,
        )

        # 构造子 agent 的输入消息
        prompt = f"研究 query: {query}\n\n研究 scope: {scope}\n\n请按 system prompt 要求收集证据并输出结构化摘要。"

        try:
            result = sub_agent.invoke(
                {"messages": [HumanMessage(content=prompt)]},
                config={"recursion_limit": 100}
            )

            logger.info("[deep_research] subagent completed, result keys: %s", list(result.keys()))
            final_messages = result.get("messages", [])
            logger.info("[deep_research] final_messages count: %d", len(final_messages))

            # 新增：记录所有消息的类型和内容摘要
            for i, msg in enumerate(final_messages):
                msg_type = getattr(msg, "type", "unknown")
                content_preview = str(msg.content)[:100] if hasattr(msg, "content") else "N/A"
                logger.info("[deep_research] msg[%d]: type=%s, content_preview=%s...", i, msg_type, content_preview)

            # 取最后一条 AIMessage 的 content 作为摘要
            for msg in reversed(final_messages):
                if hasattr(msg, "type") and msg.type == "ai" and msg.content:
                    content = msg.content
                    logger.info("[deep_research] found AIMessage, raw content type: %s, length: %d",
                                type(content).__name__, len(str(content)))

                    if isinstance(content, list):
                        content = "".join(
                            block.get("text", "") if isinstance(block, dict) else str(block)
                            for block in content
                        )

                    final_output = str(content).strip()
                    logger.info("[deep_research] final output length: %d chars", len(final_output))
                    return final_output

            return "(子 agent 未产生任何 AIMessage 输出)"

        except Exception as e:
            logger.error("[deep_research] subagent failed with exception: %s", e, exc_info=True)
            return f"(子 agent 执行失败: {e})"


def create_deep_research_tool(base_dir: Path) -> DeepResearchTool:
    """工厂函数：tools/__init__.py 自动发现要求的 create_* 入口。"""
    tool = DeepResearchTool()
    tool.base_dir = str(base_dir)
    return tool
