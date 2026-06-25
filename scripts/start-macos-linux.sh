#!/bin/bash

# ============================================
#   PuddingClaw - 一键启动脚本
#   适用于 macOS 和 Linux
# ============================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

BACKEND_HOST="${BACKEND_HOST:-0.0.0.0}"
BACKEND_PORT="${BACKEND_PORT:-8888}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}"
FRONTEND_URL="http://127.0.0.1:${FRONTEND_PORT}"

# 本地运行 frontend/backend 时，基础设施若由 docker-compose.infra.yml 提供，
# backend 应通过宿主机端口访问，而不是 Docker service name。
export AI_GATEWAY_URL="${AI_GATEWAY_URL:-http://localhost:8080/v1}"
export MINERU_URL="${MINERU_URL:-http://localhost:8002}"

echo ""
echo "============================================"
echo "  PuddingClaw - 一键启动脚本"
echo "============================================"
echo ""

if ! command -v python3 >/dev/null 2>&1; then
    echo -e "${RED}[错误] 未找到 Python3，请先安装 Python 3.10+${NC}"
    exit 1
fi

if ! command -v node >/dev/null 2>&1; then
    echo -e "${RED}[错误] 未找到 Node.js，请先安装 Node.js 18+${NC}"
    exit 1
fi

echo -e "${BLUE}[信息] Python 版本:${NC} $(python3 --version)"
echo -e "${BLUE}[信息] Node.js 版本:${NC} $(node --version)"
echo ""

echo -e "${YELLOW}[步骤 1/5] 检查 Python 虚拟环境...${NC}"
if [ ! -d "backend/.venv" ]; then
    echo -e "${BLUE}[信息] 创建虚拟环境 backend/.venv ...${NC}"
    python3 -m venv backend/.venv
    echo -e "${GREEN}[成功] 虚拟环境创建完成${NC}"
else
    echo -e "${BLUE}[信息] 虚拟环境已存在${NC}"
fi

echo ""
echo -e "${YELLOW}[步骤 2/5] 安装后端依赖...${NC}"
source backend/.venv/bin/activate
pip install -r backend/requirements.txt
echo -e "${GREEN}[成功] 后端依赖安装完成${NC}"

echo ""
echo -e "${YELLOW}[步骤 3/5] 检查环境变量配置...${NC}"
if [ ! -f "backend/.env" ]; then
    if [ -f "backend/.env.example" ]; then
        echo -e "${YELLOW}[警告] 未找到 backend/.env，已从 .env.example 复制模板${NC}"
        cp backend/.env.example backend/.env
        echo -e "${YELLOW}[提示] 请编辑 backend/.env 填写 API Key 后重新启动${NC}"
    else
        echo -e "${YELLOW}[警告] 未找到 backend/.env，请手动创建并配置 API Key${NC}"
    fi
else
    echo -e "${GREEN}[成功] 环境变量配置文件已存在${NC}"
fi

echo ""
echo -e "${YELLOW}[步骤 4/5] 安装前端依赖...${NC}"
cd frontend
if [ ! -d "node_modules" ]; then
    echo -e "${BLUE}[信息] 首次安装，运行 npm install...${NC}"
    npm install
else
    echo -e "${BLUE}[信息] node_modules 已存在，跳过安装${NC}"
fi
cd ..
echo -e "${GREEN}[成功] 前端依赖检查完成${NC}"
echo ""

echo -e "${YELLOW}[步骤 5/5] 启动服务...${NC}"
echo ""
echo "  后端 API:  ${BACKEND_URL}"
echo "  前端界面:  ${FRONTEND_URL}"
echo "  API 文档:  ${BACKEND_URL}/docs"
echo "  Higress:   ${AI_GATEWAY_URL}"
echo "  MinerU:    ${MINERU_URL}"
echo ""
echo "============================================"
echo "  按 Ctrl+C 停止所有服务"
echo "============================================"
echo ""

BACKEND_PID=""

cleanup() {
    echo ""
    echo -e "${YELLOW}[信息] 正在停止服务...${NC}"
    if [ ! -z "$BACKEND_PID" ]; then
        kill "$BACKEND_PID" 2>/dev/null || true
    fi
    # 清理可能残留的进程
    lsof -ti:$BACKEND_PORT | xargs kill -9 2>/dev/null || true
    lsof -ti:$FRONTEND_PORT | xargs kill -9 2>/dev/null || true
    echo -e "${GREEN}[成功] 服务已停止${NC}"
    exit 0
}

trap cleanup SIGINT SIGTERM

# 清理端口残留进程
if lsof -ti:$BACKEND_PORT >/dev/null 2>&1; then
    echo -e "${YELLOW}[信息] 端口 $BACKEND_PORT 被占用，正在清理...${NC}"
    lsof -ti:$BACKEND_PORT | xargs kill 2>/dev/null || true
    sleep 1
fi
if lsof -ti:$FRONTEND_PORT >/dev/null 2>&1; then
    echo -e "${YELLOW}[信息] 端口 $FRONTEND_PORT 被占用，正在清理...${NC}"
    lsof -ti:$FRONTEND_PORT | xargs kill 2>/dev/null || true
    sleep 1
fi

echo -e "${BLUE}[信息] 启动后端服务...${NC}"
cd backend
source .venv/bin/activate
python -m uvicorn app:app --host "$BACKEND_HOST" --port "$BACKEND_PORT" --reload \
    --reload-dir api \
    --reload-dir graph \
    --reload-dir tools \
    --reload-include "*.py" \
    --log-level info \
    --log-config ../logging.yaml &
BACKEND_PID=$!
cd ..

echo -e "${BLUE}[信息] 等待后端启动...${NC}"
sleep 3

echo -e "${BLUE}[信息] 启动前端服务...${NC}"
cd frontend
npx next dev -H 0.0.0.0 --port "$FRONTEND_PORT"

cleanup
