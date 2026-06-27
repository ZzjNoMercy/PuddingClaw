#!/bin/bash
# 使用 uv 在宿主机启动 MinerU 服务。
# 这是 setup-mineru.py 的简单包装，确保和项目其他脚本一样使用 uv 管理依赖。
#
# 用法：
#   ./scripts/start-mineru-host.sh
#   MINERU_PORT=8002 ./scripts/start-mineru-host.sh
#   ./scripts/start-mineru-host.sh --skip-model-download

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

PORT="${MINERU_PORT:-8002}"
UV_CACHE_DIR="${UV_CACHE_DIR:-/private/tmp/puddingclaw-uv-cache}"

echo "============================================"
echo "  PuddingClaw - 启动 MinerU（uv 模式）"
echo "============================================"
echo ""

if ! command -v uv >/dev/null 2>&1; then
    echo "[错误] 未找到 uv。请先安装 uv:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# 过滤掉外部可能激活的错误 VIRTUAL_ENV，确保使用 PuddingClaw 自己的 .venv
unset VIRTUAL_ENV
unset VIRTUAL_ENV_PROMPT
hash -r

echo "[信息] 启动 MinerU API（端口 ${PORT}）..."
echo ""
echo "  API: http://localhost:${PORT}"
echo ""
echo "============================================"
echo "  按 Ctrl+C 停止服务"
echo "============================================"
echo ""

# 调用 setup-mineru.py 完成依赖同步、模型下载、.env 更新并前台启动服务
exec python scripts/setup-mineru.py --mode native --port "$PORT" --foreground "$@"
