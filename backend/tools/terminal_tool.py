"""SafeTerminalTool — sandboxed shell execution with command blacklist."""

import re
import shlex
import subprocess
from pathlib import Path
from typing import Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field


BLACKLISTED_COMMANDS = [
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "dd if=",
    ":(){:|:&};:",
    "chmod -R 777 /",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "format c:",
    "del /f /s /q c:",
]


class TerminalInput(BaseModel):
    command: str = Field(description="The shell command to execute")


class SafeTerminalTool(BaseTool):
    name: str = "terminal"
    description: str = (
        "Execute shell commands in a sandboxed environment. "
        "The working directory is restricted to the project root. "
        "Use this for file operations, installing packages, running scripts, etc."
    )
    args_schema: Type[BaseModel] = TerminalInput
    risk_level: str = "dangerous"
    root_dir: str = ""
    path_aliases: dict[str, str] = Field(default_factory=dict)

    def _is_safe(self, command: str) -> bool:
        cmd_lower = command.lower().strip()
        for blocked in BLACKLISTED_COMMANDS:
            if blocked in cmd_lower:
                return False
        return True

    def _apply_path_aliases(self, command: str) -> str:
        """Map DeepAgents virtual paths to host paths before shell execution."""

        rewritten = command
        for alias, target in sorted(self.path_aliases.items(), key=lambda item: len(item[0]), reverse=True):
            if not alias or not target:
                continue
            normalized_alias = alias.rstrip("/")
            normalized_target = str(Path(target).expanduser())
            quoted_target = shlex.quote(normalized_target)

            # Replace `/skills/foo.py` as `'<real skills dir>'/foo.py`.
            rewritten = rewritten.replace(
                f"{normalized_alias}/",
                f"{quoted_target}/",
            )
            # Replace an exact `/skills` token without touching `/skills-old`.
            rewritten = re.sub(
                rf"(?<!\S){re.escape(normalized_alias)}(?![\w./-])",
                quoted_target,
                rewritten,
            )
        return rewritten

    def _run(self, command: str) -> str:
        if not self._is_safe(command):
            return f"❌ Command blocked for safety: {command}"
        command = self._apply_path_aliases(command)
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=self.root_dir,
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
                output = "(command completed with no output)"
            # Truncate very long output, but preserve PuddingClaw structured tool-result
            # envelopes so that citations/sources are not lost.
            MAX_PLAIN_OUTPUT = 5000
            if len(output) > MAX_PLAIN_OUTPUT and "puddingclaw_tool_result" not in output:
                output = output[:MAX_PLAIN_OUTPUT] + "\n...[truncated]"
            return output
        except subprocess.TimeoutExpired:
            return "❌ Command timed out (30s limit)"
        except Exception as e:
            return f"❌ Error: {str(e)}"


def create_terminal_tool(base_dir: Path) -> SafeTerminalTool:
    return SafeTerminalTool(root_dir=str(base_dir))
