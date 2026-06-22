"""ExecuteSkillTool — parse SKILL.md and execute its scripts in order."""

import re
import subprocess
import sys
from pathlib import Path
from typing import Type

import yaml
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field


class ExecuteSkillInput(BaseModel):
    skill_name: str = Field(description="要执行的技能名称")
    user_query: str = Field(default="", description="用户的原始问题，用于上下文")


class ExecuteSkillTool(BaseTool):
    name: str = "execute_skill"
    description: str = (
        "执行一个已注册的 Skill。传入 skill_name（技能名称），工具会读取 SKILL.md，"
        "自动执行其中声明的 Python 脚本并返回结果。"
        "适用于需要主动调用某项技能（如获取日期、查询天气等）的场景。"
    )
    args_schema: Type[BaseModel] = ExecuteSkillInput
    risk_level: str = "moderate"
    skills_dir: str = ""

    def _parse_frontmatter(self, content: str) -> dict:
        """从 SKILL.md 解析 YAML frontmatter（--- 之间的内容）。"""
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not match:
            return {}
        try:
            return yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            return {}

    def _extract_scripts(self, content: str) -> list[str]:
        """从 ## Resources 段落提取 scripts/*.py 文件列表。"""
        resources_match = re.search(
            r"##\s+Resources\s*\n(.*?)(?=\n##|\Z)", content, re.DOTALL
        )
        if not resources_match:
            return []

        scripts = []
        for line in resources_match.group(1).splitlines():
            # 匹配形如 `scripts/xxx.py` 的路径
            py_match = re.search(r"`(scripts/[^`]+\.py)`", line)
            if py_match:
                scripts.append(py_match.group(1))
        return scripts

    def _resolve_skill_dir(self, skills_dir: Path, skill_name: str) -> Path | None:
        """智能匹配技能目录：兼容 -/_ 差异和大小写差异。"""
        # 直接候选
        candidates = [
            skills_dir / skill_name,
            skills_dir / skill_name.replace("-", "_"),
            skills_dir / skill_name.replace("_", "-"),
        ]
        # 遍历已存在的目录做大小写和规范化匹配
        norm_target = skill_name.lower().replace("-", "_")
        for subdir in skills_dir.iterdir():
            if subdir.is_dir():
                if subdir.name.lower().replace("-", "_") == norm_target:
                    candidates.append(subdir)
        for cand in candidates:
            if cand.is_dir():
                return cand
        return None

    def _run(self, skill_name: str, user_query: str = "") -> str:
        from graph.citations import encode_tool_result, parse_tool_result

        skills_dir = Path(self.skills_dir)
        skill_dir = self._resolve_skill_dir(skills_dir, skill_name)

        # 1. 验证 skill 目录存在
        if skill_dir is None:
            return f"错误：技能目录不存在：{skills_dir / skill_name}"

        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            return f"错误：SKILL.md 不存在：{skill_md}"

        # 2. 读取并解析 SKILL.md
        content = skill_md.read_text(encoding="utf-8")
        frontmatter = self._parse_frontmatter(content)
        description = frontmatter.get("description", f"技能：{skill_name}")

        # 3. 提取 scripts/*.py
        scripts = self._extract_scripts(content)
        if not scripts:
            return f"[{skill_name}] {description}\n\n（该技能没有声明可执行脚本）"

        # 4. 依次执行每个脚本
        results = []
        collected_sources = []
        for script_path in scripts:
            # 安全约束：只执行 .py 文件
            if not script_path.endswith(".py"):
                continue

            abs_script = skill_dir / script_path
            if not abs_script.is_file():
                results.append(f"[跳过] {script_path}（文件不存在）")
                continue

            try:
                result = subprocess.run(
                    [sys.executable, str(abs_script)],
                    cwd=str(skill_dir),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    encoding="utf-8",
                    errors="replace",
                )
                output = result.stdout
                if result.stderr:
                    output += f"\n[stderr]: {result.stderr}"
                if not output.strip():
                    output = "(脚本执行完成，无输出)"
                # 截断超长输出
                if len(output) > 5000:
                    output = output[:5000] + "\n...[已截断]"
                answer_context, sources = parse_tool_result(output)
                for source in sources:
                    if source.get("source_type") == "knowledge_base":
                        source["source_type"] = "skill"
                    source.setdefault("metadata", {})["skill_name"] = skill_name
                collected_sources.extend(sources)
                results.append(f"[{script_path}]\n{answer_context}")
            except subprocess.TimeoutExpired:
                results.append(f"[{script_path}] 错误：执行超时（30秒限制）")
            except Exception as e:
                results.append(f"[{script_path}] 错误：{str(e)}")

        output_text = "\n\n".join(results)
        answer = f"技能：{skill_name}\n描述：{description}\n\n执行结果：\n{output_text}"
        return encode_tool_result(answer, collected_sources) if collected_sources else answer


def create_execute_skill_tool(base_dir: Path) -> ExecuteSkillTool:
    return ExecuteSkillTool(skills_dir=str(base_dir / "skills"))
