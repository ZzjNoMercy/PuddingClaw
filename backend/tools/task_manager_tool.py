"""Task Manager Tool — 任务清单管理工具。

提供任务状态更新、清理已完成任务、查看任务列表等功能。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field


class TaskManagerInput(BaseModel):
    action: str = Field(
        description="操作类型：'list'（列出所有任务）、'complete'（标记任务完成）、'clean'（清理已完成任务）、'archive'（归档旧任务）"
    )
    task_number: int | None = Field(
        default=None,
        description="任务编号（从 1 开始），用于 'complete' 操作"
    )
    days: int | None = Field(
        default=7,
        description="归档天数阈值（默认 7 天），用于 'archive' 操作"
    )


class TaskManagerTool(BaseTool):
    """任务清单管理工具。

    支持的操作：
    - list: 列出所有任务（显示编号、状态、描述）
    - complete: 标记指定任务为已完成
    - clean: 清理所有已完成的任务
    - archive: 归档超过 N 天的已完成任务
    """

    name: str = "task_manager"
    description: str = (
        "Manage TODO.md task list. Use this tool (NOT read_file) when user asks to: "
        "'查看任务', '查看待办', '查看TODO', 'list tasks', 'show tasks', "
        "'标记完成', 'mark done', 'complete task', "
        "'清理任务', 'clean tasks', 'remove completed'. "
        "Actions: 'list' (show all tasks), 'complete' (mark task as done by number), "
        "'clean' (remove all completed tasks), 'archive' (archive old completed tasks)."
    )
    args_schema: Type[BaseModel] = TaskManagerInput
    risk_level: str = "safe"
    todo_path: Path | None = None

    def _run(self, action: str, task_number: int | None = None, days: int | None = 7) -> str:
        """执行任务管理操作。"""
        if self.todo_path is None or not self.todo_path.exists():
            return "TODO.md 文件不存在，暂无任务。"

        if action == "list":
            return self._list_tasks()
        elif action == "complete":
            if task_number is None:
                return "错误：'complete' 操作需要指定 task_number。"
            return self._complete_task(task_number)
        elif action == "clean":
            return self._clean_completed_tasks()
        elif action == "archive":
            return self._archive_old_tasks(days or 7)
        else:
            return f"错误：不支持的操作 '{action}'。支持的操作：list, complete, clean, archive。"

    def _list_tasks(self) -> str:
        """列出所有任务。"""
        lines = self.todo_path.read_text(encoding="utf-8").splitlines()
        tasks = []
        for i, line in enumerate(lines, start=1):
            if line.strip().startswith("- ["):
                status = "✓" if "[x]" in line or "[X]" in line else " "
                # 提取任务描述（去掉 checkbox 和时间戳）
                desc = re.sub(r"^- \[.\] ", "", line)
                desc = re.sub(r" \(created:.*\)$", "", desc)
                tasks.append(f"{i}. [{status}] {desc}")

        if not tasks:
            return "TODO.md 中暂无任务。"

        return "当前任务列表：\n" + "\n".join(tasks)

    def _complete_task(self, task_number: int) -> str:
        """标记指定任务为已完成。"""
        lines = self.todo_path.read_text(encoding="utf-8").splitlines()
        task_lines = [i for i, line in enumerate(lines) if line.strip().startswith("- [")]

        if task_number < 1 or task_number > len(task_lines):
            return f"错误：任务编号 {task_number} 超出范围（共 {len(task_lines)} 个任务）。"

        target_idx = task_lines[task_number - 1]
        line = lines[target_idx]

        # 检查是否已完成
        if "[x]" in line or "[X]" in line:
            return f"任务 {task_number} 已经是完成状态。"

        # 标记为已完成
        lines[target_idx] = line.replace("- [ ]", "- [x]", 1)

        self.todo_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return f"✓ 任务 {task_number} 已标记为完成。"

    def _clean_completed_tasks(self) -> str:
        """清理所有已完成的任务。"""
        lines = self.todo_path.read_text(encoding="utf-8").splitlines()
        original_count = sum(1 for line in lines if line.strip().startswith("- ["))

        # 保留未完成的任务和标题行
        new_lines = []
        for line in lines:
            if line.strip().startswith("- ["):
                if "- [x]" not in line and "- [X]" not in line:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        new_count = sum(1 for line in new_lines if line.strip().startswith("- ["))
        removed_count = original_count - new_count

        if removed_count == 0:
            return "没有已完成的任务需要清理。"

        self.todo_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        return f"✓ 已清理 {removed_count} 个已完成的任务，剩余 {new_count} 个待办任务。"

    def _archive_old_tasks(self, days: int) -> str:
        """归档超过 N 天的已完成任务。"""
        lines = self.todo_path.read_text(encoding="utf-8").splitlines()
        cutoff_date = datetime.now() - timedelta(days=days)

        archived_lines = []
        new_lines = []

        for line in lines:
            if line.strip().startswith("- ["):
                # 提取创建时间
                match = re.search(r"created: ([\d-T:]+)", line)
                if match and ("[x]" in line or "[X]" in line):
                    created_str = match.group(1)
                    try:
                        created_date = datetime.fromisoformat(created_str)
                        if created_date < cutoff_date:
                            archived_lines.append(line)
                            continue
                    except ValueError:
                        pass
                new_lines.append(line)
            else:
                new_lines.append(line)

        if not archived_lines:
            return f"没有超过 {days} 天的已完成任务需要归档。"

        # 写入归档文件
        archive_path = self.todo_path.parent / "TODO_ARCHIVE.md"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        archive_content = f"\n\n# 归档时间：{timestamp}\n" + "\n".join(archived_lines)

        if archive_path.exists():
            archive_path.write_text(
                archive_path.read_text(encoding="utf-8") + archive_content,
                encoding="utf-8"
            )
        else:
            archive_path.write_text(
                f"# TODO 归档\n{archive_content}",
                encoding="utf-8"
            )

        # 更新 TODO.md
        self.todo_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

        return f"✓ 已归档 {len(archived_lines)} 个超过 {days} 天的已完成任务到 TODO_ARCHIVE.md。"


def create_task_manager_tool(base_dir: Path) -> TaskManagerTool:
    """工厂函数：tools/__init__.py 自动发现要求的 create_* 入口。"""
    tool = TaskManagerTool()
    tool.todo_path = base_dir / "workspace" / "TODO.md"
    return tool
