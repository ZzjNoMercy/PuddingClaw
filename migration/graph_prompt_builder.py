"""Prompt Builder — Assemble system prompt from Markdown files.

基于 V5 的 Prefix Caching 架构，融合魔镜Claw 的提示词工程优化：
- 自动注入当前日期/星期/运行环境
- 追加 Tool 使用指南（减少重复探测、规范 workflow）
- 可选 system.md 组件

注意：保留 V5 Ch5 的 Prefix Caching 优化（静态/动态分离），这是比魔镜Claw 更先进的设计。
"""

from pathlib import Path
from datetime import datetime
import platform

MAX_COMPONENT_LENGTH = 20000

# (memory_path_str, mtime) → structure_snapshot
_MEMORY_STRUCTURE_CACHE: dict[tuple[str, float], str] = {}


def _read_component(path: Path) -> str:
    """读取指定路径的文件内容，超长时截断。"""
    if not path.exists():
        return ""
    raw = path.read_bytes()
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            content = raw.decode(enc)
            break
        except (UnicodeDecodeError, ValueError):
            continue
    else:
        content = raw.decode("utf-8", errors="replace")
    if len(content) > MAX_COMPONENT_LENGTH:
        content = content[:MAX_COMPONENT_LENGTH] + "\n...[truncated]"
    return content


def _strip_memory_protocol(agents_content: str) -> str:
    """从 AGENTS.md 内容中移除「记忆协议」段落。"""
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


# mem0 模式下注入的类型说明
MEM0_MEMORY_GUIDE = """### 记忆类型说明（Claude Code 分类体系）
以下记忆按类型分组，帮助你理解用户背景并按需使用：
- **用户偏好 (user)**：用户的角色、习惯、工作方式、喜好与背景
- **行为规则 (feedback)**：用户对 AI 协作方式的明确要求，需遵循的操作规则与边界
- **项目上下文 (project)**：当前进行中的工作、功能需求、技术决策、进度信息
- **参考信息 (reference)**：文档路径、外部链接、API 地址、配置文件位置等"""


# markdown 模式的记忆协议
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

# mem0 模式的记忆协议
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


RAG_READ_GUIDANCE = """注意：长期记忆(MEMORY.md)已切换为RAG检索模式。
系统会根据用户的问题自动检索相关记忆片段并注入上下文。
如果检索到了相关记忆，它们会以"[记忆检索结果]"标记呈现在你的上下文中。"""


# 静态部分：写入规则 + 触发条件 + 铁律 + 格式示例。字节永不变，合入静态前缀扩展 cache 锚点。
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
    """提取 MEMORY.md 章节骨架，作为动态内容附加到长期记忆区块末尾。"""
    cache_key = None
    if memory_path is not None and memory_path.exists():
        try:
            cache_key = (str(memory_path), memory_path.stat().st_mtime)
            if cache_key in _MEMORY_STRUCTURE_CACHE:
                return _format_snapshot_block(_MEMORY_STRUCTURE_CACHE[cache_key])
        except OSError:
            cache_key = None

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

    if cache_key is not None:
        _MEMORY_STRUCTURE_CACHE[cache_key] = structure_snapshot
        if len(_MEMORY_STRUCTURE_CACHE) > 16:
            oldest = next(iter(_MEMORY_STRUCTURE_CACHE))
            del _MEMORY_STRUCTURE_CACHE[oldest]

    return _format_snapshot_block(structure_snapshot)


def _format_snapshot_block(structure_snapshot: str) -> str:
    return f"""### MEMORY.md 当前章节结构
```
{structure_snapshot}
```"""


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

    拼接顺序（保留 V5 Ch5 Prefix Caching 优化）：
    【静态前缀 — cache 锚点区】
    1. SKILLS_SNAPSHOT.md
    2. workspace/SOUL.md
    3. workspace/IDENTITY.md
    4. workspace/USER.md
    5. workspace/AGENTS.md（移除记忆协议段落）
    6. MEMORY_WRITE_PROTOCOL_STATIC（静态部分）

    【动态区块】
    7. 长期记忆（三种模式）
    8. Tool Reminder（条件注入）

    【新增：魔镜Claw 优化】
    9. 日期/环境提示
    10. Tool 使用指南
    """
    # 可选 system.md：如果存在则插入在 IDENTITY 之后
    system_md_path = base_dir / "workspace" / "system.md"
    has_system_md = system_md_path.exists()

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

        if label == "Soul" and memory_backend == "mem0":
            content = content.replace(
                "**文件即记忆**：你的记忆以 Markdown 文件的形式存在，任何人都可以直接阅读和编辑。这不是限制，而是你的独特优势。",
                "**向量即记忆**：你的记忆存储在向量数据库中，系统会自动检索相关记忆注入上下文。你可以通过 `save_memory` 工具主动保存重要信息。",
            )

        if label == "Agents Guide":
            content = _strip_memory_protocol(content)
            protocol = MEMORY_PROTOCOL_MEM0 if memory_backend == "mem0" else MEMORY_PROTOCOL_MARKDOWN
            content = content.rstrip("\n") + "\n\n" + protocol

        parts.append(f"<!-- {label} -->\n{content}")

    if memory_backend != "mem0":
        parts.append(f"<!-- Memory Write Protocol (static) -->\n{MEMORY_WRITE_PROTOCOL_STATIC}")

    # 第 7 层：长期记忆注入（动态区块）
    if memory_backend == "mem0":
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
        memory_content = _read_component(base_dir / "memory" / "MEMORY.md")
        memory_path = base_dir / "memory" / "MEMORY.md"

        dynamic_blocks: list[str] = []
        if not rag_mode:
            if memory_content:
                dynamic_blocks.append(memory_content)
        else:
            rag_block = RAG_READ_GUIDANCE
            if rag_context:
                rag_block += f"\n\n{rag_context}"
            dynamic_blocks.append(rag_block)

        dynamic_blocks.append(_build_memory_structure_snapshot(memory_content, memory_path=memory_path))

        if dynamic_blocks:
            parts.append("<!-- Long-term Memory -->\n" + "\n\n".join(dynamic_blocks))

    result = "\n\n".join(parts)

    if tool_reminder:
        result += TOOL_REMINDER_SECTION

    # ========== 新增：魔镜Claw 提示词优化 ==========
    result += "\n\n" + _build_environment_hint()
    result += "\n\n" + TOOL_USAGE_GUIDE

    return result
