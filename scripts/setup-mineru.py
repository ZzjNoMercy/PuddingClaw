#!/usr/bin/env python3
"""MinerU 一键部署脚本（uv 版本）。

MinerU 是 PuddingClaw 的可选解析服务，通过 backend/pyproject.toml 中的
`[project.optional-dependencies.mineru]` 管理（默认使用 base ``mineru``，
不包含 vllm/lmdeploy 等 GPU 后端，避免与 backend 核心依赖冲突）。本脚本负责：

1. 检测操作系统、GPU、Docker 可用性
2. 推荐并执行合适的部署方式
3. 用 uv 安装/同步依赖（含 mineru optional）
4. 启动 mineru-api 服务
5. 将 MINERU_URL 写回 backend/.env

用法：
    python scripts/setup-mineru.py [--mode native|docker] [--port 8002] [--dry-run]

环境要求：
- Python 3.10 ~ 3.13
- uv (https://docs.astral.sh/uv/)

参考：
- https://github.com/opendatalab/MinerU
- https://opendatalab.github.io/MinerU/zh/quick_start/docker_deployment/
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
ENV_FILE = BACKEND_DIR / ".env"

DEFAULT_PORT = 8002

DRY_RUN: bool = False
AUTO_YES: bool = False
SKIP_MODEL_DOWNLOAD: bool = False
FOREGROUND: bool = False


def log(msg: str, level: str = "info") -> None:
    prefix = {"info": "[INFO]", "warn": "[WARN]", "error": "[ERROR]", "step": "[STEP]"}.get(level, "[INFO]")
    print(f"{prefix} {msg}")


def run(cmd: list[str], *, check: bool = True, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """运行命令，dry-run 模式下只打印。

    如果 env 未传入，会自动过滤掉外部激活的 VIRTUAL_ENV，避免 uv 发出
    ``VIRTUAL_ENV does not match the project environment path`` 警告。
    """
    log(f"$ {' '.join(cmd)}", level="step")
    if DRY_RUN:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    if env is None:
        env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    return subprocess.run(cmd, check=check, cwd=cwd, env=env)


def ask_yes_no(question: str, default: bool = True) -> bool:
    if AUTO_YES:
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{question} {suffix}: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


# ==================== 环境探测 ====================

def detect_os() -> tuple[Literal["macos", "linux", "windows", "unknown"], bool]:
    system = platform.system().lower()
    if system == "darwin":
        return "macos", False
    if system == "linux":
        is_wsl = Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists() or "WSL" in platform.release()
        return "linux", is_wsl
    if system == "windows":
        return "windows", False
    return "unknown", False


def detect_python() -> tuple[int, int]:
    return sys.version_info[:2]


def check_python_ok() -> bool:
    major, minor = detect_python()
    return (3, 10) <= (major, minor) <= (3, 13)


def detect_gpu() -> tuple[Literal["nvidia", "mps", "none"], str]:
    if shutil.which("nvidia-smi"):
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                name = result.stdout.strip().splitlines()[0]
                return "nvidia", name
        except Exception:
            pass

    if platform.system() == "Darwin" and platform.machine() in ("arm64", "Apple Silicon"):
        return "mps", "Apple Silicon"

    return "none", ""


def detect_docker() -> bool:
    return shutil.which("docker") is not None


def detect_wsl() -> bool:
    """检测 Windows 上是否安装了 WSL（仅在 host 为 Windows 时调用）。"""
    if platform.system().lower() != "windows":
        return False
    return shutil.which("wsl") is not None


def ensure_uv() -> str:
    """确保 uv 可用，返回 uv 命令路径。"""
    uv = shutil.which("uv")
    if uv:
        return uv
    log("未检测到 uv，尝试自动安装...", level="warn")
    try:
        # 使用官方安装脚本
        subprocess.run(
            [sys.executable, "-c", "import urllib.request; urllib.request.urlretrieve('https://astral.sh/uv/install.sh', '/tmp/uv-install.sh')"],
            check=True,
        )
        subprocess.run(["sh", "/tmp/uv-install.sh"], check=True)
        uv = shutil.which("uv")
        if uv:
            return uv
    except Exception as e:
        log(f"自动安装 uv 失败: {e}", level="error")
    log("请手动安装 uv: https://docs.astral.sh/uv/getting-started/installation/", level="error")
    raise SystemExit(1)


# ==================== 部署模式决策 ====================

def recommend_mode(
    os_type: str,
    is_wsl: bool,
    gpu: str,
    docker_ok: bool,
    wsl_ok: bool,
) -> Literal["native", "docker"]:
    if os_type == "macos":
        log("macOS 官方不建议使用 Docker 部署 MinerU，推荐原生安装。")
        return "native"

    if os_type == "linux":
        if is_wsl:
            log("检测到 WSL2 环境，建议使用 Docker 部署。")
        else:
            log("检测到 Linux 环境，Docker 部署兼容性最好。")
        if docker_ok:
            return "docker"
        log("Docker 不可用，切换到原生安装。", level="warn")
        return "native"

    if os_type == "windows":
        if docker_ok:
            log("Windows 环境检测到 Docker，使用 Docker 部署。")
            return "docker"
        if wsl_ok:
            log(
                "Windows 检测到 WSL2，但未检测到 Docker。"
                "请在 WSL2 内安装 Docker 后，直接在 WSL2 中运行本脚本；"
                "或在 Windows 上安装 Docker Desktop 并启用 WSL2 后端。",
                level="error",
            )
        else:
            log(
                "Windows 原生环境不支持 MinerU 原生部署。"
                "请先安装 WSL2 + Docker（推荐），或安装 Docker Desktop，然后重新运行。",
                level="error",
            )
        raise SystemExit(1)

    log("无法识别操作系统，尝试原生安装。", level="warn")
    return "native"


# ==================== 模型下载 ====================

VALID_MODEL_SOURCES = ("auto", "huggingface", "modelscope")


def get_model_source() -> str:
    """返回有效的模型下载源，非法值会回退到 modelscope 并告警。"""
    source = os.environ.get("MINERU_MODEL_SOURCE", "modelscope")
    if source not in VALID_MODEL_SOURCES:
        log(
            f"MINERU_MODEL_SOURCE='{source}' 不是有效下载源（{', '.join(VALID_MODEL_SOURCES)}），"
            "回退到 modelscope",
            level="warn",
        )
        return "modelscope"
    return source


def _models_already_present() -> bool:
    """检查 ~/.mineru/models 是否已有模型文件。"""
    model_dir = Path.home() / ".mineru" / "models"
    if not model_dir.exists():
        return False
    # 只要目录非空且不只包含空子目录，就认为已有模型
    return any(model_dir.iterdir())


def download_mineru_models(uv: str, gpu: str, skip_download: bool = False) -> None:
    """预下载 MinerU 模型到 ~/.mineru/models。

    macOS/CPU 环境使用 base mineru，只需 pipeline 模型；
    GPU 加速需要 vllm/lmdeploy（当前不在 backend extra 里），
    若未来在 Docker/独立环境中启用，可扩展为下载 vlm 模型。
    """
    if skip_download:
        log("跳过模型预下载（--skip-model-download）")
        if not _models_already_present():
            log("警告：~/.mineru/models 为空，首次 API 调用仍会触发下载。", level="warn")
        return

    source = get_model_source()
    if source == "local":
        log("MINERU_MODEL_SOURCE=local，跳过模型下载，使用本地已有模型。")
        if not _models_already_present():
            log("警告：~/.mineru/models 为空，请确认本地模型已放置正确。", level="warn")
        return

    # 原生部署使用 base mineru，默认下载 pipeline 模型即可
    model_type = "pipeline"

    if _models_already_present():
        log("检测到 ~/.mineru/models 已存在模型文件，跳过预下载。")
        log("如需重新下载，请清空该目录后重新运行脚本。")
        return

    log(f"预下载 MinerU {model_type} 模型（来源: {source}，约 10GB+）...")
    log("下载耗时取决于网络，请耐心等待。")

    cmd = [
        uv, "run", "--extra", "mineru",
        "python", "-m", "mineru.cli.models_download",
        "-s", source,
        "-m", model_type,
    ]
    run(cmd, cwd=BACKEND_DIR)
    log("MinerU 模型下载完成。")


# ==================== 原生部署（uv） ====================

def setup_mineru_native(port: int, gpu: str, foreground: bool = False) -> None:
    """用 uv 安装并启动 MinerU。

    Args:
        port: API 端口。
        gpu: GPU 类型（nvidia/mps/none）。
        foreground: 为 True 时前台阻塞运行并实时输出日志；False 时后台启动并返回。
    """
    uv = ensure_uv()

    if not check_python_ok():
        log(f"当前 Python {detect_python()} 不在 MinerU 支持范围 3.10~3.13", level="error")
        raise SystemExit(1)

    # 设置/校验模型下载源
    source = get_model_source()
    if os.environ.get("MINERU_MODEL_SOURCE") != source:
        os.environ["MINERU_MODEL_SOURCE"] = source
        log(f"已设置 MINERU_MODEL_SOURCE={source}")

    # 同步依赖（安装 mineru optional，base 包不含 GPU 后端）
    log("使用 uv 安装后端依赖（含 mineru optional）...")
    run([uv, "sync", "--extra", "mineru"], cwd=BACKEND_DIR)

    # 预下载模型（避免首次 API 调用时等待）
    download_mineru_models(uv, gpu, skip_download=SKIP_MODEL_DOWNLOAD)

    # 启动 mineru-api
    log(f"启动 MinerU API 服务（端口 {port}）...")

    cmd = [
        uv, "run", "--extra", "mineru",
        "mineru-api", "--host", "0.0.0.0", "--port", str(port),
    ]

    log(f"启动命令: {' '.join(cmd)}")
    if DRY_RUN:
        log("[DRY-RUN] 跳过启动服务", level="info")
        return

    update_env_file(f"http://localhost:{port}")

    clean_env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}

    if foreground:
        log("前台模式：按 Ctrl+C 停止服务。")
        try:
            subprocess.run(cmd, cwd=BACKEND_DIR, env=clean_env, check=True)
        except KeyboardInterrupt:
            log("收到中断信号，MinerU API 已停止。", level="warn")
            raise SystemExit(0)
        except subprocess.CalledProcessError as e:
            log(f"MinerU API 异常退出，返回码: {e.returncode}", level="error")
            raise SystemExit(1)
        return

    process = subprocess.Popen(cmd, cwd=BACKEND_DIR, env=clean_env)
    time.sleep(3)

    if process.poll() is not None:
        log("MinerU API 启动失败，请检查上面的错误日志。", level="error")
        raise SystemExit(1)

    log(f"MinerU API 已启动，PID={process.pid}")


# ==================== Docker 部署 ====================

def start_mineru_docker(port: int, gpu: str) -> None:
    """使用 MinerU 官方 Docker 镜像启动。"""
    if not detect_docker():
        log("Docker 不可用，无法使用 Docker 模式。", level="error")
        raise SystemExit(1)

    # MinerU 官方镜像（建议钉版本）
    image = "opendatalab/mineru:v3.4.0"
    container_name = "puddingclaw-mineru"

    # 先停止同名容器
    try:
        subprocess.run(["docker", "rm", "-f", container_name], check=False, capture_output=True)
    except Exception:
        pass

    mineru_home = Path.home() / ".mineru"
    mineru_home.mkdir(parents=True, exist_ok=True)

    cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        "-p", f"{port}:8002",
        "-v", f"{mineru_home / 'models'}:/root/.mineru/models",
        "-v", f"{mineru_home / 'mineru.json'}:/root/.mineru/mineru.json",
        "-e", "MINERU_MODEL_SOURCE=modelscope",
    ]

    if gpu == "nvidia":
        cmd.extend(["--gpus", "all"])
        log("启用 NVIDIA GPU 支持。")
    else:
        log("使用 CPU 模式运行。")

    cmd.extend([
        image,
        "mineru-api", "--host", "0.0.0.0", "--port", "8002",
    ])

    run(cmd)
    log(f"MinerU Docker 容器已启动: {container_name}")
    log("首次调用解析时会自动下载模型（约 10GB+），请耐心等待。")
    time.sleep(5)

    update_env_file(f"http://localhost:{port}")


# ==================== 项目配置 ====================

def update_env_file(mineru_url: str) -> None:
    """将 MINERU_URL 写回 backend/.env。"""
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

    found = False
    for i, line in enumerate(lines):
        if line.startswith("MINERU_URL="):
            lines[i] = f"MINERU_URL={mineru_url}"
            found = True
            break

    if not found:
        lines.append(f"MINERU_URL={mineru_url}")

    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"已更新 {ENV_FILE}: MINERU_URL={mineru_url}")


# ==================== 主流程 ====================

def main() -> None:
    parser = argparse.ArgumentParser(description="MinerU 一键部署脚本（uv 版本）")
    parser.add_argument("--mode", choices=["native", "docker", "auto"], default="auto",
                        help="部署模式：native 使用 uv，docker 使用官方镜像")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="MinerU API 端口")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要执行的命令")
    parser.add_argument("--yes", "-y", action="store_true", help="自动确认所有提示")
    parser.add_argument("--skip-model-download", action="store_true",
                        help="跳过模型预下载（已有本地模型或希望首次调用时懒加载）")
    parser.add_argument("--foreground", "-f", action="store_true",
                        help="前台运行 mineru-api，实时输出日志（按 Ctrl+C 停止）")
    args = parser.parse_args()

    global DRY_RUN, AUTO_YES, SKIP_MODEL_DOWNLOAD, FOREGROUND
    DRY_RUN = args.dry_run
    AUTO_YES = args.yes
    SKIP_MODEL_DOWNLOAD = args.skip_model_download
    FOREGROUND = args.foreground

    log("=" * 60)
    log("开始部署 MinerU")
    log("=" * 60)

    os_type, is_wsl = detect_os()
    gpu, gpu_name = detect_gpu()
    docker_ok = detect_docker()
    wsl_ok = detect_wsl()

    log(f"操作系统: {platform.platform()} ({os_type}, WSL2={is_wsl})")
    log(f"Python 版本: {detect_python()}")
    log(f"GPU: {gpu} ({gpu_name or '无'})")
    log(f"Docker 可用: {docker_ok}")
    if os_type == "windows":
        log(f"WSL 可用: {wsl_ok}")

    mode: Literal["native", "docker"] = args.mode
    if mode == "auto":
        mode = recommend_mode(os_type, is_wsl, gpu, docker_ok, wsl_ok)

    if mode == "native" and os_type == "windows":
        log("Windows 不支持 MinerU 原生部署。请使用 --mode docker，或先在 WSL2 中运行本脚本。", level="error")
        raise SystemExit(1)

    if mode == "docker" and os_type == "macos":
        log("macOS 不建议 Docker 部署 MinerU。", level="warn")
        if not ask_yes_no("是否强制使用 Docker 模式？", default=False):
            mode = "native"

    if mode == "docker" and not docker_ok:
        if os_type == "windows":
            log("Windows 上 Docker 不可用，无法使用 Docker 模式。请安装 Docker Desktop 或 WSL2 + Docker。", level="error")
            raise SystemExit(1)
        log("Docker 不可用，切换到原生安装。", level="warn")
        mode = "native"

    log(f"最终部署模式: {mode}")

    if mode == "native":
        setup_mineru_native(args.port, gpu, foreground=FOREGROUND)
    else:
        if FOREGROUND:
            log("Docker 模式不支持 --foreground，将以后台容器方式运行。", level="warn")
        start_mineru_docker(args.port, gpu)

    log("=" * 60)
    log("MinerU 部署完成")
    log(f"API 地址: http://localhost:{args.port}")
    log(f"项目 env 已更新: {ENV_FILE}")
    log("=" * 60)


if __name__ == "__main__":
    main()
