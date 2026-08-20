"""Explicit background installation for locked parser optional extras."""

from __future__ import annotations

import asyncio
import importlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from knowledge.parsers.contracts import ParserError

_INSTALL_TASKS: dict[str, asyncio.Task[None]] = {}
_INSTALL_STATE: dict[str, dict[str, Any]] = {}
_EXTRAS = {"unstructured_local": "unstructured"}


def install_status(parser_id: str) -> dict[str, Any]:
    return dict(_INSTALL_STATE.get(parser_id) or {"status": "idle", "message": ""})


async def start_optional_dependency_install(parser_id: str) -> dict[str, Any]:
    extra = _EXTRAS.get(parser_id)
    if not extra:
        raise ParserError(f"解析器没有可安装的依赖组：{parser_id}")
    active = _INSTALL_TASKS.get(parser_id)
    if active is not None and not active.done():
        return install_status(parser_id)
    _INSTALL_STATE[parser_id] = {
        "status": "installing",
        "message": f"正在安装锁定依赖组 {extra}",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    _INSTALL_TASKS[parser_id] = asyncio.create_task(
        _run_install(parser_id, extra), name=f"parser-dependency-install-{parser_id}"
    )
    return install_status(parser_id)


async def _run_install(parser_id: str, extra: str) -> None:
    backend_dir = Path(__file__).resolve().parents[2]
    uv = shutil.which("uv")
    if not uv:
        _INSTALL_STATE[parser_id] = {"status": "failed", "message": "找不到 uv，无法安装可选依赖"}
        return
    if not (backend_dir / "pyproject.toml").is_file() or not (backend_dir / "uv.lock").is_file():
        _INSTALL_STATE[parser_id] = {
            "status": "failed",
            "message": "当前安装不包含 pyproject.toml/uv.lock，请通过发布包安装对应 extra",
        }
        return
    try:
        process = await asyncio.create_subprocess_exec(
            uv,
            "sync",
            "--project",
            str(backend_dir),
            "--extra",
            extra,
            "--locked",
            "--inexact",
            cwd=str(backend_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        _INSTALL_STATE[parser_id] = {"status": "failed", "message": f"无法启动 uv：{exc}"}
        return
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=1800)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        _INSTALL_STATE[parser_id] = {"status": "failed", "message": "依赖安装超过 30 分钟，已停止"}
        return
    text = output.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        _INSTALL_STATE[parser_id] = {
            "status": "failed",
            "message": (text[-1200:] or f"uv 退出码 {process.returncode}"),
        }
        return
    importlib.invalidate_caches()
    # Clear the registry health cache after site-packages changes. Import here
    # to avoid a registry -> installer -> registry module cycle.
    from knowledge.parsers.registry import get_document_parser_registry

    get_document_parser_registry().invalidate_health(parser_id)
    _INSTALL_STATE[parser_id] = {
        "status": "succeeded",
        "message": "依赖安装完成，已自动重新探测",
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
