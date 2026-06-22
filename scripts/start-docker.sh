#!/bin/bash

# ============================================
#   PuddingClaw - Docker 一键启动脚本
# ============================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

BACKEND_PORT="${BACKEND_PORT:-6666}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}"
FRONTEND_URL="http://127.0.0.1:${FRONTEND_PORT}"

echo ""
echo "============================================"
echo "  PuddingClaw - Docker 启动脚本"
echo "============================================"
echo ""

if ! command -v docker >/dev/null 2>&1; then
    echo -e "${RED}[错误] 未找到 Docker，请先安装 Docker${NC}"
    exit 1
fi

if ! command -v docker-compose >/dev/null 2>&1 && ! docker compose version >/dev/null 2>&1; then
    echo -e "${RED}[错误] 未找到 Docker Compose，请先安装${NC}"
    exit 1
fi

# 检查 .env 是否存在
if [ ! -f "backend/.env" ]; then
    if [ -f "backend/.env.example" ]; then
        echo -e "${YELLOW}[警告] 未找到 backend/.env，已从 .env.example 复制模板${NC}"
        cp backend/.env.example backend/.env
        echo -e "${YELLOW}[提示] 请编辑 backend/.env 填写 API Key 后重新启动${NC}"
    else
        echo -e "${YELLOW}[警告] 未找到 backend/.env，请手动创建并配置 API Key${NC}"
    fi
fi

COMPOSE_CMD="docker compose"
if ! docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
fi

echo -e "${BLUE}[信息] 构建并启动服务...${NC}"
$COMPOSE_CMD up --build -d

echo ""
echo -e "${GREEN}[成功] PuddingClaw 已启动${NC}"
echo ""
echo "  前端界面: ${FRONTEND_URL}"
echo "  后端 API: ${BACKEND_URL}"
echo "  API 文档: ${BACKEND_URL}/docs"
echo ""
echo "  常用命令："
echo "    查看日志: $COMPOSE_CMD logs -f backend"
echo "    停止服务: $COMPOSE_CMD down"
echo ""
