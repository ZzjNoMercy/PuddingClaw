#!/bin/bash
# 开发模式：先启动 frontend dev server，再启动 Electron

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "[1/3] 安装 electron 依赖..."
cd electron
if [ ! -d "node_modules" ]; then
    npm install
fi
cd ..

echo "[2/3] 启动 frontend dev server..."
# 清理可能残留的 3000 端口，确保 frontend 使用固定端口
lsof -ti:3000 | xargs kill 2>/dev/null || true
sleep 1
cd frontend
npm run dev &
FRONTEND_PID=$!
cd ..

# 等待 localhost:3000 就绪
echo "[3/3] 等待 frontend 就绪并启动 Electron..."
for i in $(seq 1 30); do
    if curl -s http://localhost:3000 >/dev/null 2>&1; then
        echo "frontend ready"
        break
    fi
    echo "waiting... $i/30"
    sleep 1
done

cd electron
npm start

# Electron 退出后清理 frontend dev server
echo "停止 frontend dev server..."
kill $FRONTEND_PID 2>/dev/null || true
