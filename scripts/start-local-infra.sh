#!/bin/bash

# ============================================
#   PuddingClaw - 本地基础设施启动脚本
#   frontend/backend/MinerU 在本机运行；
#   Docker 只启动 Higress + Milvus。
# ============================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "============================================"
echo "  PuddingClaw - 本地基础设施"
echo "============================================"
echo ""

if ! command -v docker >/dev/null 2>&1; then
    echo -e "${RED}[错误] 未找到 Docker。Higress/Milvus infra 需要 Docker。${NC}"
    exit 1
fi

echo -e "${YELLOW}[步骤 1/2] 启动 Higress + Milvus...${NC}"
if ! docker compose -f docker-compose.infra.yml up -d; then
    echo ""
    echo -e "${RED}[错误] Docker 基础设施启动失败。${NC}"
    echo ""
    echo -e "${YELLOW}[排查建议]${NC}"
    echo "1. 如果错误包含 mirror.aliyuncs.com 403，通常是 Docker Desktop registry mirror 拦截了 Docker Hub 拉取。"
    echo "   可在 Docker Desktop -> Settings -> Docker Engine 中移除失效 registry-mirrors 后重启 Docker。"
    echo ""
    echo "2. 也可以临时覆盖镜像地址后重试，例如："
    echo "   MILVUS_IMAGE=<可用镜像源>/milvusdb/milvus:v2.5.4 \\"
    echo "   MINIO_IMAGE=<可用镜像源>/minio/minio:RELEASE.2025-04-22T22-12-26Z \\"
    echo "   ./scripts/start-local-infra.sh"
    echo ""
    echo "3. 如果只需要 Higress，可先继续使用已有 puddingclaw-higress 容器；Milvus 可稍后再启动。"
    exit 1
fi

echo ""
echo -e "${YELLOW}[步骤 2/2] 本机服务地址${NC}"
echo ""
echo "  Higress Gateway: http://localhost:8080"
echo "  Higress Console: http://localhost:8001"
echo "  Milvus:          http://localhost:19530"
echo "  MinerU API:      http://localhost:8002  (本机运行，不由本脚本启动)"
echo ""
echo -e "${BLUE}[提示] 如需启动本机 MinerU：${NC}"
echo "  python scripts/setup-mineru.py --foreground"
echo "  # 或使用已有 conda 环境：MINERU_PORT=8002 scripts/start-mineru-host.sh"
echo ""
echo -e "${BLUE}[提示] 本机 backend 推荐环境变量：${NC}"
echo "  AI_GATEWAY_URL=http://localhost:8080/v1"
echo "  MINERU_URL=http://localhost:8002"
echo ""
echo -e "${GREEN}[完成] 基础设施启动命令已执行。${NC}"
