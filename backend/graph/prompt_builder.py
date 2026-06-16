"""Prompt Builder — Assemble system prompt from 6 Markdown files.

读写解耦设计：
- 读取：rag_mode=False 时全文注入，rag_mode=True 时由调用方向量检索注入
- 写入：无论 rag_mode 如何，始终注入「记忆写入指南」（含 MEMORY.md 当前结构摘要）
"""

from pathlib import Path
from datetime import datetime
import platform

# 单个组件文件的最大字符数，超出部分会被截断，防止 system prompt 过长导致 LLM context 溢出
MAX_COMPONENT_LENGTH = 20000

# (memory_path_str, mtime) → structure_snapshot
_MEMORY_STRUCTURE_CACHE: dict[tuple[str, float], str] = {}


def _read_component(path: Path) -> str:
    """读取指定路径的文件内容，超长时截断。

    依次尝试 UTF-8 → GBK → latin-1 编码，兼容 Agent 可能写入的混合编码文件。
    若三种编码均失败，则用 UTF-8 替换模式（errors='replace'）兜底。
    """
    if not path.exists():
        return ""
    raw = path.read_bytes()
    # 三级编码回退：优先 UTF-8，兼容中文环境的 GBK，最后 latin-1 作为保底
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            content = raw.decode(enc)
            break
        except (UnicodeDecodeError, ValueError):
            continue
    else:
        # 所有编码均失败时，UTF-8 替换模式确保不崩溃
        content = raw.decode("utf-8", errors="replace")
    # 超出长度限制时截断并附加提示，避免 system prompt 过长
    if len(content) > MAX_COMPONENT_LENGTH:
        content = content[:MAX_COMPONENT_LENGTH] + "\n...[truncated]"
    return content


def _strip_memory_protocol(agents_content: str) -> str:
    """从 AGENTS.md 内容中移除「记忆协议」段落，由调用方按 memory_backend 注入对应版本。

    查找 '## 记忆协议' 标题行，移除该行及其下属内容直到下一个 '## ' 二级标题。
    """
    lines = agents_content.splitlines(keepends=True)
    result = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## 记忆协议"):
            skipping = True
            continue
        if skipping and stripped.startswith("## "):
            skipping = False
        if not skipping:
            result.append(line)
    return "".join(result)


# mem0 模式下注入的类型说明，帮助 LLM 理解记忆的分类体系
# 对应 Claude Code 的 user/feedback/project/reference 四类型
MEM0_MEMORY_GUIDE = """### 记忆类型说明（Claude Code 分类体系）
以下记忆按类型分组，帮助你理解用户背景并按需使用：
- **用户偏好 (user)**：用户的角色、习惯、工作方式、喜好与背景
- **行为规则 (feedback)**：用户对 AI 协作方式的明确要求，需遵循的操作规则与边界
- **项目上下文 (project)**：当前进行中的工作、功能需求、技术决策、进度信息
- **参考信息 (reference)**：文档路径、外部链接、API 地址、配置文件位置等"""


# markdown 模式的记忆协议（与 AGENTS.md 原文一致）
MEMORY_PROTOCOL_MARKDOWN = """## 记忆协议 (MEMORY PROTOCOL)

### 长期记忆写入（强制）
- 文件位置：`memory/MEMORY.md`
- 写入工具：**必须使用 `write_file`**（禁止用 terminal 拼命令）
- 写入流程：`read_file("memory/MEMORY.md")` → 在对应章节追加 → `write_file("memory/MEMORY.md", 完整内容)`
- 触发条件和格式详见 system prompt 中的「记忆写入指南」章节

### 会话日志
- 文件位置：`memory/logs/YYYY-MM-DD.md`
- 每日自动归档的对话摘要

### 记忆读取
- 在回答问题前，检查上下文中是否有相关的历史记忆信息
- 优先使用已记录的用户偏好"""

# mem0 模式的记忆协议（替代 markdown 版本）
MEMORY_PROTOCOL_MEM0 = """## 记忆协议 (MEMORY PROTOCOL)

### 长期记忆写入
- 写入方式：根据信息类型选择对应工具主动保存重要信息：
  - `save_user_memory`：保存用户画像（姓名、角色、技术背景、偏好）
  - `save_feedback_memory`：保存行为规则与用户纠正（如"不要在末尾总结"）
  - `save_project_memory`：保存项目上下文、决策、进行中的工作
  - `save_reference_memory`：保存参考指针（文档 URL、文件路径、API 端点）
- 系统会自动从对话中提取关键记忆（SmartExtractor），无需手动写文件
- **禁止**使用 `write_file` 写入 `memory/MEMORY.md`，本系统使用向量数据库管理记忆

### 会话日志
- 文件位置：`memory/logs/YYYY-MM-DD.md`
- 每日自动归档的对话摘要

### 记忆读取
- 系统已根据当前对话自动检索相关历史记忆并注入上下文
- 优先使用已记录的用户偏好"""


# RAG 模式下注入 system prompt 的说明文字（仅控制读取行为）。
RAG_READ_GUIDANCE = """注意：长期记忆(MEMORY.md)已切换为RAG检索模式。
系统会根据用户的问题自动检索相关记忆片段并注入上下文。
如果检索到了相关记忆，它们会以"[记忆检索结果]"标记呈现在你的上下文中。"""


# 静态部分：写入规则 + 触发条件 + 铁律 + 格式示例。字节永不变，合入静态前缀扩展 cache 锚点。
# 实际的 MEMORY.md 章节结构会作为动态内容拼接到长期记忆区块末尾（见 _build_memory_structure_snapshot）。
MEMORY_WRITE_PROTOCOL_STATIC = """## 记忆写入协议（始终生效）

### 写入规则
- 文件路径：`memory/MEMORY.md`
- 写入工具：使用 `write_file` 工具（不要用 terminal）
- 写入方式：先用 `read_file` 读取当前内容，在对应章节下追加新条目，然后用 `write_file` 写回完整内容
- 禁止覆盖已有内容，只做追加
- MEMORY.md 当前章节结构见下方「长期记忆」区块末尾的快照，请据此判断写入位置

### 必须写入的触发条件
当对话中出现以下任一情况时，你**必须**在回复用户后立即写入 MEMORY.md：
1. **用户偏好**：用户表达了喜好、习惯、工作方式（写入「用户偏好」或「用户信息」章节）
2. **重要决策**：做出了技术选型、方案确认、配置变更（写入「重要事项」章节）
3. **新建/修改技能**：创建或优化了技能（写入「重要事项」章节，记录技能名称、路径、时间）
4. **关键事实**：用户提供了项目信息、团队信息、环境配置等（写入对应章节）
5. **用户明确要求记住**：用户说"记住这个"、"下次还要用"等（写入对应章节）

### ⚠️ 严禁口头写入（铁律）
你必须通过**实际调用 write_file 工具**来写入记忆。以下行为严格禁止：
- ❌ 在回复文本中写"让我使用 write_file 工具..."但不发起实际 tool call
- ❌ 在回复文本中展示 write_file 的调用结果但实际未调用
- ❌ 声称"已保存到 MEMORY.md"但没有 tool call 记录
- ✅ 正确做法：先调用 read_file 读取，再调用 write_file 写入，两次都必须是真实的工具调用

### 写入格式示例
每条新记录格式：`- 内容描述（补充说明）`，在对应 `###` 章节下追加。"""


def _build_memory_structure_snapshot(memory_content: str, memory_path=None) -> str:
    """提取 MEMORY.md 章节骨架，作为动态内容附加到长期记忆区块末尾。

    与 MEMORY_WRITE_PROTOCOL_STATIC 解耦：静态规则进入 cache 锚点，动态章节
    结构与 memory_content 一起变化（反正 memory_content 本身就跟 MEMORY.md mtime 变）。

    Args:
        memory_content: 已读取的 MEMORY.md 全文（由调用方传入，避免重复 IO）
        memory_path: 可选，MEMORY.md 的 Path 对象，用于 mtime 缓存
    """
    # 尝试查缓存
    cache_key = None
    if memory_path is not None and memory_path.exists():
        try:
            cache_key = (str(memory_path), memory_path.stat().st_mtime)
            if cache_key in _MEMORY_STRUCTURE_CACHE:
                return _format_snapshot_block(_MEMORY_STRUCTURE_CACHE[cache_key])
        except OSError:
            cache_key = None

    # 缓存未命中：扫描章节骨架
    structure_lines: list[str] = []
    if memory_content:
        for line in memory_content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# ") or stripped.startswith("## ") or stripped.startswith("### "):
                structure_lines.append(stripped)

    if structure_lines:
        structure_snapshot = "\n".join(structure_lines)
    else:
        structure_snapshot = "（文件为空或不存在，请按下方格式创建初始结构）"

    # 写缓存
    if cache_key is not None:
        _MEMORY_STRUCTURE_CACHE[cache_key] = structure_snapshot
        # 淘汰老条目，防止无限增长
        if len(_MEMORY_STRUCTURE_CACHE) > 16:
            oldest = next(iter(_MEMORY_STRUCTURE_CACHE))
            del _MEMORY_STRUCTURE_CACHE[oldest]

    return _format_snapshot_block(structure_snapshot)


def _format_snapshot_block(structure_snapshot: str) -> str:
    """把章节骨架包装成 system prompt 里的独立区块。"""
    return f"""### MEMORY.md 当前章节结构
```
{structure_snapshot}
```"""


# 工具提醒段落，追加到 system prompt 末尾（用于长对话时强化工具调用意识）
TOOL_REMINDER_SECTION = (
    "\n\n## 工具调用提醒\n"
    "[系统提醒] 请记住：你必须使用工具来完成任务。"
    "需要读取文件时调用 read_file，需要执行命令时调用 terminal，"
    "需要写入文件时调用 write_file。"
    "禁止在文本中描述操作而不实际调用工具。"
)


# ========== 新增：环境/日期提示 ==========
def _build_environment_hint() -> str:
    now = datetime.now()
    weekday_names = ["一", "二", "三", "四", "五", "六", "日"]
    is_windows = platform.system() == "Windows"
    shell_hints = (
        "用 dir 替代 ls，用 type 替代 cat，用 mkdir 替代 mkdir -p，路径分隔符可使用 \\ 或 /"
        if is_windows
        else "使用标准 Unix 命令（ls/cat/mkdir 等），路径分隔符为 /"
    )
    return (
        f"【当前时间】今天是 {now.year}年{now.month}月{now.day}日 "
        f"星期{weekday_names[now.weekday()]}。"
        f"涉及日期、时间、本周/本月/今年的查询或计算，请基于此进行判断。\n\n"
        f"【运行环境】当前系统为 {platform.system()}。"
        f"使用 terminal 工具时请注意：{shell_hints}。"
    )


# ========== 新增：工具使用指南 ==========
TOOL_USAGE_GUIDE = """## 工具使用指南

### 各技能/工具的适用场景
- **execute_skill**：执行已注册的 Skill。workflow 类型技能需要先调用 execute_skill 探测类型和获取指引；script 类型技能可直接执行。
  - 如果当前会话中已经调用过 execute_skill 并获取了技能指引（如 SKILL.md 内容已在上下文中），请直接基于已有指引继续执行后续步骤，**不需要重复调用** execute_skill 探测。
- **read_file**：读取 workspace/ 目录下的文件内容。
- **write_file**：向 workspace/ 目录写入文件。禁止仅在回复文本中声称已保存而不实际调用。
- **terminal**：在 workspace/ 目录下执行 shell 命令。

### 工作流执行原则
- 首次使用某 workflow 技能时，按步骤执行：execute_skill 探测 → 阅读 SKILL.md → 阅读 references → 执行 scripts 查询。
- **同一话题的后续对话中**，如果之前已经探测过该技能并阅读了文档，请直接继续执行后续查询步骤，**不要从头重新探测**。
- 遇到查询失败时，优先分析失败原因并修复后重试，而不是重新开始整个 workflow。"""


def build_system_prompt(
    base_dir: Path,
    rag_mode: bool = False,
    memory_backend: str = "markdown",
    mem0_context: str = "",
    rag_context: str = "",
    tool_reminder: bool = False,
) -> str:
    """按固定顺序拼接多个 Markdown 文件，构建完整的 system prompt。

    拼接顺序（Ch5 cache-aware 重构后）：
    【静态前缀 — cache 锚点区】
    1. SKILLS_SNAPSHOT.md  — 当前可用技能快照
    2. workspace/SOUL.md   — Agent 人格、语气、行为边界
    3. workspace/IDENTITY.md — Agent 名称、风格、emoji 设定
    4. workspace/USER.md   — 用户画像与偏好
    5. workspace/AGENTS.md — 操作指令、记忆与技能协议
    6. MEMORY_WRITE_PROTOCOL_STATIC — markdown 模式的记忆写入协议（静态部分）

    【动态区块 — 随记忆/检索结果变化】
    7. 长期记忆（三种模式）：
       - markdown + Direct：MEMORY.md 全文 + 章节结构快照
       - markdown + RAG：RAG 检索提示 + 检索结果 + 章节结构快照
       - mem0：mem0 检索结果
    8. Tool Reminder — 条件注入，追加于末尾（tool_reminder=True 时生效）

    Prefix Caching 优化（Ch5 关键重构）：
    - 组件 1-6 全部静态，跨请求字节内容一致，DeepSeek 自动 Prefix Cache 锚点
    - 旧版 Memory Write Guide 因含 structure_snapshot（跟 MEMORY.md mtime 变化）
      被放在"静态末尾"，一旦 MEMORY.md 更新就破坏缓存；现已拆为静态规则（第 6 层）
      + 动态 snapshot（合并到第 7 层末尾）
    - 组件 7-8 为动态内容，刻意置于 prompt 末尾，最大化 Prefix Cache 命中率
    - 禁止调整以上组件顺序，否则会破坏静态前缀连续性，导致缓存失效
    """
    # 可选 system.md：如果存在则插入在 IDENTITY 之后
    system_md_path = base_dir / "workspace" / "system.md"
    has_system_md = system_md_path.exists()

    # ⚠️ 组件顺序不可调整（1-5 为 Prefix Caching 静态前缀），详见上方 docstring
    components = [
        ("Skills Snapshot", base_dir / "SKILLS_SNAPSHOT.md"),
        ("Soul", base_dir / "workspace" / "SOUL.md"),
        ("Identity", base_dir / "workspace" / "IDENTITY.md"),
    ]
    if has_system_md:
        components.append(("System Rules", system_md_path))
    components.extend([
        ("User Profile", base_dir / "workspace" / "USER.md"),
        ("Agents Guide", base_dir / "workspace" / "AGENTS.md"),
    ])

    parts: list[str] = []
    for label, path in components:
        content = _read_component(path)
        if not content:
            continue

        # mem0 模式下替换 SOUL.md 中的"文件即记忆"原则
        # 注意：此替换是确定性的（memory_backend 不变则输出不变），不影响 Prefix Caching
        if label == "Soul" and memory_backend == "mem0":
            content = content.replace(
                "**文件即记忆**：你的记忆以 Markdown 文件的形式存在，任何人都可以直接阅读和编辑。这不是限制，而是你的独特优势。",
                "**向量即记忆**：你的记忆存储在向量数据库中，系统会自动检索相关记忆注入上下文。你可以通过 `save_memory` 工具主动保存重要信息。",
            )

        # 移除 AGENTS.md 中的记忆协议段落，按 memory_backend 注入对应版本
        if label == "Agents Guide":
            content = _strip_memory_protocol(content)
            protocol = MEMORY_PROTOCOL_MEM0 if memory_backend == "mem0" else MEMORY_PROTOCOL_MARKDOWN
            content = content.rstrip("\n") + "\n\n" + protocol

        parts.append(f"<!-- {label} -->\n{content}")

    # 第 6 层：markdown 模式的记忆写入协议（静态）
    # 合入静态前缀扩展 DeepSeek prefix cache 锚点；mem0 模式不需要（mem0 自动管理写入）
    if memory_backend != "mem0":
        parts.append(f"<!-- Memory Write Protocol (static) -->\n{MEMORY_WRITE_PROTOCOL_STATIC}")

    # 第 7 层：长期记忆注入（动态区块，随 mtime / 检索结果变化）
    if memory_backend == "mem0":
        # mem0 模式：注入类型说明 + 结构化检索结果（由 agent.py 的 _format_mem0_context() 生成）
        # 类型体系来自 Claude Code：user / feedback / project / reference
        if mem0_context:
            parts.append(
                f"<!-- Long-term Memory (mem0) -->\n## 用户长期记忆\n\n"
                f"{MEM0_MEMORY_GUIDE}\n\n{mem0_context}"
            )
        else:
            parts.append(
                f"<!-- Long-term Memory (mem0) -->\n## 用户长期记忆\n\n"
                f"{MEM0_MEMORY_GUIDE}\n\n（当前未检索到相关记忆，随着对话积累会自动丰富）"
            )
    else:
        # markdown 模式：长期记忆内容 + 章节结构快照合并为单个 dynamic 区块
        memory_content = _read_component(base_dir / "memory" / "MEMORY.md")
        memory_path = base_dir / "memory" / "MEMORY.md"

        dynamic_blocks: list[str] = []
        if not rag_mode:
            if memory_content:
                dynamic_blocks.append(memory_content)
        else:
            # RAG 模式：注入 RAG guidance + 实际检索结果（如有）
            rag_block = RAG_READ_GUIDANCE
            if rag_context:
                rag_block += f"\n\n{rag_context}"
            dynamic_blocks.append(rag_block)

        # 章节结构快照：作为动态内容附加到长期记忆区块末尾
        # 与 memory_content 的 mtime 一起变化，不再破坏静态前缀
        dynamic_blocks.append(_build_memory_structure_snapshot(memory_content, memory_path=memory_path))

        if dynamic_blocks:
            parts.append("<!-- Long-term Memory -->\n" + "\n\n".join(dynamic_blocks))

    # 各组件之间用空行分隔，保持 prompt 可读性
    result = "\n\n".join(parts)

    # 长对话时在 system prompt 末尾追加工具提醒，避免污染对话历史
    if tool_reminder:
        result += TOOL_REMINDER_SECTION

    # ========== 新增：魔镜Claw 提示词优化 ==========
    result += "\n\n" + _build_environment_hint()
    result += "\n\n" + TOOL_USAGE_GUIDE

    return result
