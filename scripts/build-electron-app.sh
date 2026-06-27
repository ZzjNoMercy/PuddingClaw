#!/bin/bash
# 构建 PuddingClaw Electron 桌面应用（macOS .app）

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "============================================"
echo "  PuddingClaw Electron App Builder"
echo "============================================"
echo ""

# 1. 检查前置依赖
if ! command -v node >/dev/null 2>&1; then
    echo "[错误] 未找到 Node.js"
    exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "[错误] 未找到 uv。打包后的 app 首次启动时需要 uv 创建 .venv"
    exit 1
fi

# 2. 安装 electron 依赖
echo "[1/4] 安装 electron 依赖..."
cd electron
if [ ! -d "node_modules" ]; then
    npm install
fi
cd ..

# 3. 构建 frontend production standalone
echo "[2/4] 构建 frontend production build..."
cd frontend
npm install
NEXT_DIST_DIR=.next-build npm run build
cd ..

# 4. 确保 standalone 包含 static
echo "[3/4] 复制 static 资源到 standalone..."
if [ -d "frontend/.next-build/static" ]; then
    cp -r frontend/.next-build/static frontend/.next-build/standalone/.next-build/static
fi
if [ -d "frontend/public" ]; then
    cp -r frontend/public frontend/.next-build/standalone/public
fi

# 5. 打包 Electron
echo "[4/4] 打包 Electron app..."
cd electron
npm run build:dir

cd ..
echo ""
echo "============================================"
echo "  构建完成"
echo "============================================"
echo ""
echo "输出目录: $REPO_ROOT/dist-electron/mac-arm64/PuddingClaw.app"
echo ""
echo "运行方式:"
echo "  open $REPO_ROOT/dist-electron/mac-arm64/PuddingClaw.app"
echo ""
echo "注意:"
echo "  1. 首次启动 app 会自动执行 uv sync 创建 backend/.venv，可能需要几分钟"
echo "  2. 需要 Docker Desktop 已安装并运行"
echo "  3. 当前为开发验证版本，未签名，可能需要在 系统设置 -> 隐私与安全性 中允许"
echo ""
