#!/bin/bash
# 构建 PuddingClaw Electron 桌面应用（macOS arm64 DMG）

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
echo "[1/5] 安装 electron 依赖..."
cd electron
if [ ! -d "node_modules" ]; then
    npm install
fi
cd ..

# 3. 从当前 Backend 和 Web 源码重建 CLI 嵌入式 Runtime
echo "[2/5] 构建 CLI 嵌入式 Runtime..."
cd packages/puddingclaw-deploy-cli
npm run build:runtime
cd ../..

# 4. 构建 Electron 使用的 frontend production standalone
echo "[3/5] 构建 frontend production build..."
cd frontend
npm install
NEXT_DIST_DIR=.next-build npm run build
cd ..

# 5. 确保 standalone 包含 static
echo "[4/5] 复制 static 资源到 standalone..."
if [ -d "frontend/.next-build/static" ]; then
    cp -r frontend/.next-build/static frontend/.next-build/standalone/.next-build/static
fi
if [ -d "frontend/public" ]; then
    cp -r frontend/public frontend/.next-build/standalone/public
fi

# 6. 打包、签名并生成 DMG；提供 Apple 公证凭据时会自动公证
echo "[5/5] 打包并签名 Electron DMG..."
cd electron
npm run build:dmg

cd ..
echo ""
echo "============================================"
echo "  构建完成"
echo "============================================"
echo ""
echo "输出目录: $REPO_ROOT/dist-electron/PuddingClaw-0.1.0-arm64.dmg"
echo ""
echo "注意:"
echo "  1. DMG 使用登录钥匙串中的 Developer ID Application 证书签名"
echo "  2. 设置 Apple 公证环境变量后，electron-builder 会自动公证并装订票据"
echo "  3. 首次启动由 CLI 按所选模式准备用户目录 Runtime，可能需要几分钟"
echo ""
