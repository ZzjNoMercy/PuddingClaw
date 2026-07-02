#!/bin/bash

# ============================================
#   PuddingClaw - 本地基础设施启动脚本
#   frontend/backend/MinerU 在本机运行；
#   Docker 启动 Higress + Milvus，并可选启动 PuddingClaw bundled PostgreSQL。
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

export POSTGRES_PORT="${POSTGRES_PORT:-5432}"
export POSTGRES_DB="${POSTGRES_DB:-puddingclaw}"
export POSTGRES_USER="${POSTGRES_USER:-puddingclaw}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-puddingclaw}"
LOCAL_POSTGRES_USER="${LOCAL_POSTGRES_USER:-$(whoami)}"
LOCAL_POSTGRES_DB="${LOCAL_POSTGRES_DB:-puddingclaw}"
LOCAL_POSTGRES_PASSWORD="${LOCAL_POSTGRES_PASSWORD:-}"

# PostgreSQL 模式：
#   detect   默认。若 5432 已被非 Docker 本机服务占用，则写入“本机 PostgreSQL”配置；
#            否则启动 PuddingClaw 内置 Docker PostgreSQL 并写入内置配置。
#   bundled  强制启动 docker-compose.infra.yml 里的 puddingclaw-postgres。
#   external 明确使用用户已有 PostgreSQL，本脚本不启动 postgres service。
POSTGRES_MODE="${PUDDINGCLAW_POSTGRES_MODE:-detect}"

postgres_listeners() {
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"${POSTGRES_PORT}" -sTCP:LISTEN 2>/dev/null || true
    fi
}

postgres_port_used_by_non_docker() {
    local listeners
    listeners="$(postgres_listeners)"
    if [ -z "$listeners" ]; then
        return 1
    fi

    # macOS Docker Desktop 通常显示为 com.docke/com.docker/vpnkit；
    # 这些是 Docker 端口代理，不视为用户本机已有 PostgreSQL。
    if echo "$listeners" | tail -n +2 | grep -E -vq 'com\.docke|com\.docker|Docker|vpnkit'; then
        return 0
    fi
    return 1
}

START_BUNDLED_POSTGRES="true"
case "$POSTGRES_MODE" in
    detect)
        if postgres_port_used_by_non_docker; then
            START_BUNDLED_POSTGRES="false"
            echo -e "${YELLOW}[提示] 检测到本机 ${POSTGRES_PORT} 端口已有非 Docker 服务监听。${NC}"
            echo "       将保留用户已有 PostgreSQL，不启动 PuddingClaw bundled PostgreSQL。"
            echo "       若你确认要强制使用 bundled，请设置：PUDDINGCLAW_POSTGRES_MODE=bundled"
        fi
        ;;
    bundled)
        START_BUNDLED_POSTGRES="true"
        ;;
    external)
        START_BUNDLED_POSTGRES="false"
        echo -e "${BLUE}[信息] PUDDINGCLAW_POSTGRES_MODE=external，本脚本不会启动 PostgreSQL。${NC}"
        ;;
    *)
        echo -e "${RED}[错误] PUDDINGCLAW_POSTGRES_MODE 只能是 detect / bundled / external，当前为：${POSTGRES_MODE}${NC}"
        exit 1
        ;;
esac

update_database_config() {
    local mode="$1"
    local host="$2"
    local port="$3"
    local database="$4"
    local username="$5"
    local password="$6"
    python3 - "$mode" "$host" "$port" "$database" "$username" "$password" <<'PY'
import json
import sys
from pathlib import Path

mode, host, port, database, username, password = sys.argv[1:7]
path = Path("backend/config.json")
if path.exists():
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
else:
    data = {}
data.setdefault("database", {})
data["database"].update({
    "mode": mode,
    "host": host,
    "port": int(port),
    "database": database,
    "username": username,
    "password": password,
    "url": "",
})
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
PY
}

if ! command -v docker >/dev/null 2>&1; then
    echo -e "${RED}[错误] 未找到 Docker。Higress/Milvus infra 需要 Docker。${NC}"
    exit 1
fi

if [ "$START_BUNDLED_POSTGRES" = "true" ]; then
    echo -e "${YELLOW}[步骤 1/2] 启动 bundled PostgreSQL + Higress + Milvus...${NC}"
    COMPOSE_SERVICES=()
    DB_CONFIG_MODE="bundled"
    DB_CONFIG_DB="${POSTGRES_DB}"
    DB_CONFIG_USER="${POSTGRES_USER}"
    DB_CONFIG_PASSWORD="${POSTGRES_PASSWORD}"
else
    echo -e "${YELLOW}[步骤 1/2] 启动 Higress + Milvus（跳过 bundled PostgreSQL）...${NC}"
    COMPOSE_SERVICES=(higress milvus)
    DB_CONFIG_MODE="external"
    DB_CONFIG_DB="${LOCAL_POSTGRES_DB}"
    DB_CONFIG_USER="${LOCAL_POSTGRES_USER}"
    DB_CONFIG_PASSWORD="${LOCAL_POSTGRES_PASSWORD}"
fi

update_database_config "$DB_CONFIG_MODE" "127.0.0.1" "$POSTGRES_PORT" "$DB_CONFIG_DB" "$DB_CONFIG_USER" "$DB_CONFIG_PASSWORD"
echo -e "${GREEN}[完成] 已根据 PostgreSQL 检测结果更新 backend/config.json：${DB_CONFIG_MODE}${NC}"

if ! docker compose -f docker-compose.infra.yml up -d "${COMPOSE_SERVICES[@]}"; then
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
    echo "3. 如果只需要 PuddingClaw bundled PostgreSQL，可先执行："
    echo "   docker compose -f docker-compose.infra.yml up -d postgres"
    echo ""
    echo "4. 如果只需要 Higress，可先继续使用已有 puddingclaw-higress 容器；Milvus 可稍后再启动。"
    echo ""
    echo "5. 如果本机已有 PostgreSQL，请设置自己的连接串，例如："
    echo "   PUDDINGCLAW_POSTGRES_MODE=external \\"
    echo "   DATABASE_URL=postgresql+asyncpg://<user>:<password>@127.0.0.1:<port>/<database> \\"
    echo "   ./scripts/start-local-infra.sh"
    exit 1
fi

echo ""
echo -e "${YELLOW}[步骤 2/2] 本机服务地址${NC}"
echo ""
if [ "$START_BUNDLED_POSTGRES" = "true" ]; then
    echo "  PostgreSQL:      postgresql://127.0.0.1:${POSTGRES_PORT}"
    echo "    database:      ${POSTGRES_DB}"
    echo "    username:      ${POSTGRES_USER}"
    echo "    password:      ${POSTGRES_PASSWORD}"
else
    echo "  PostgreSQL:      local/user-managed"
    echo "    本脚本没有启动或修改你的本机 PostgreSQL。"
    echo "    database:      ${DB_CONFIG_DB}"
    echo "    username:      ${DB_CONFIG_USER}"
    echo "    password:      ${DB_CONFIG_PASSWORD:-<empty>}"
fi
echo "  Higress Gateway: http://localhost:8080"
echo "  Higress Console: http://localhost:8001"
echo "  Milvus:          grpc://localhost:19530"
echo "  MinerU API:      http://localhost:8002  (本机运行，不由本脚本启动)"
echo ""
echo -e "${BLUE}[提示] 如需启动本机 MinerU：${NC}"
echo "  python scripts/setup-mineru.py --foreground"
echo "  # 或使用已有 conda 环境：MINERU_PORT=8002 scripts/start-mineru-host.sh"
echo ""
echo -e "${BLUE}[提示] 本机 backend 推荐环境变量：${NC}"
if [ "$START_BUNDLED_POSTGRES" = "true" ]; then
    echo "  DATABASE_URL=postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_PORT}/${POSTGRES_DB}"
else
    echo "  已写入 backend/config.json；如需覆盖，可在 Settings -> 知识库 -> Catalog Database 中调整。"
fi
echo "  AI_GATEWAY_URL=http://localhost:8080/v1"
echo "  MINERU_URL=http://localhost:8002"
echo ""
echo -e "${GREEN}[完成] 基础设施启动命令已执行。${NC}"
