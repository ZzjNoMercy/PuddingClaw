#!/bin/bash

# ============================================
#   PuddingClaw - 本地基础设施启动脚本
#   frontend/backend/MinerU 在本机运行；
#   Docker 启动 Milvus，并可选启动 PuddingClaw bundled PostgreSQL。
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
PGVECTOR_DATABASE="${PUDDINGCLAW_PGVECTOR_DATABASE:-$LOCAL_POSTGRES_DB}"
PGVECTOR_VERSION="${PUDDINGCLAW_PGVECTOR_VERSION:-0.8.6}"
PGVECTOR_DEFAULT_VERSION="0.8.6"
PGVECTOR_DEFAULT_SHA256="10bf9938906e5d643bbc4a7eea104b6f57ba4898e5b76b20e60484ea1d5a7f8f"
PGVECTOR_SHA256="${PUDDINGCLAW_PGVECTOR_SHA256:-}"

if ! echo "$PGVECTOR_VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo -e "${RED}[错误] PUDDINGCLAW_PGVECTOR_VERSION 必须是 x.y.z 格式，当前为：${PGVECTOR_VERSION}${NC}"
    exit 1
fi

if [ -z "$PGVECTOR_SHA256" ] && [ "$PGVECTOR_VERSION" = "$PGVECTOR_DEFAULT_VERSION" ]; then
    PGVECTOR_SHA256="$PGVECTOR_DEFAULT_SHA256"
fi

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

print_pgvector_install_guide() {
    local server_major="${1:-16}"
    echo -e "${YELLOW}[依赖] 当前 PostgreSQL 缺少必备 pgvector 扩展。${NC}"
    case "$(uname -s)" in
        Darwin)
            echo "       本脚本可为 Homebrew PostgreSQL ${server_major} 自动编译 pgvector ${PGVECTOR_VERSION}。"
            echo "       请确认已安装 Xcode Command Line Tools，且 postgresql@${server_major} 由 Homebrew 管理。"
            ;;
        Linux)
            echo "       安装：sudo apt install postgresql-${server_major}-pgvector"
            echo "       然后重启 PostgreSQL 服务。"
            ;;
        *)
            echo "       安装说明：https://github.com/pgvector/pgvector"
            ;;
    esac
    echo "       安装后重新运行本脚本；脚本会在目标数据库自动启用 vector 扩展。"
}

external_psql() {
    PGPASSWORD="${LOCAL_POSTGRES_PASSWORD}" psql \
        -X \
        -v ON_ERROR_STOP=1 \
        -h 127.0.0.1 \
        -p "${POSTGRES_PORT}" \
        -U "${LOCAL_POSTGRES_USER}" \
        "$@"
}

resolve_pg_config() {
    local server_major="$1"
    local candidate=""
    local candidate_major=""

    if [ "$(uname -s)" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
        candidate="$(brew --prefix "postgresql@${server_major}" 2>/dev/null || true)/bin/pg_config"
        if [ -x "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    fi

    candidate="/usr/lib/postgresql/${server_major}/bin/pg_config"
    if [ -x "$candidate" ]; then
        echo "$candidate"
        return 0
    fi

    if command -v pg_config >/dev/null 2>&1; then
        candidate="$(command -v pg_config)"
        candidate_major="$("$candidate" --version | awk '{split($2, version, "."); print version[1]}')"
        if [ "$candidate_major" = "$server_major" ]; then
            echo "$candidate"
            return 0
        fi
    fi

    return 1
}

install_pgvector_for_homebrew_postgres() {
    local server_major="$1"
    local pg_config_path=""
    local build_dir=""
    local archive_path=""
    local source_dir=""
    local actual_sha256=""

    if [ "$(uname -s)" != "Darwin" ]; then
        print_pgvector_install_guide "$server_major"
        return 1
    fi

    for command_name in curl make shasum tar; do
        if ! command -v "$command_name" >/dev/null 2>&1; then
            echo -e "${RED}[错误] 自动安装 pgvector 需要命令：${command_name}${NC}"
            return 1
        fi
    done

    if [ -z "$PGVECTOR_SHA256" ]; then
        echo -e "${RED}[错误] 非默认 pgvector 版本必须同时设置 PUDDINGCLAW_PGVECTOR_SHA256。${NC}"
        return 1
    fi

    pg_config_path="$(resolve_pg_config "$server_major" || true)"
    if [ -z "$pg_config_path" ]; then
        echo -e "${RED}[错误] 找不到与 PostgreSQL ${server_major} 匹配的 pg_config。${NC}"
        echo "       Homebrew 安装：brew install postgresql@${server_major}"
        return 1
    fi

    build_dir="$(mktemp -d "/private/tmp/puddingclaw-pgvector-${PGVECTOR_VERSION}.XXXXXX")"
    archive_path="${build_dir}/pgvector-${PGVECTOR_VERSION}.tar.gz"
    source_dir="${build_dir}/source"
    mkdir -p "$source_dir"

    echo -e "${YELLOW}[依赖] 正在下载 pgvector ${PGVECTOR_VERSION} 源码...${NC}"
    if ! curl --fail --location --retry 3 \
        "https://github.com/pgvector/pgvector/archive/refs/tags/v${PGVECTOR_VERSION}.tar.gz" \
        --output "$archive_path"; then
        echo -e "${RED}[错误] pgvector ${PGVECTOR_VERSION} 源码下载失败。${NC}"
        return 1
    fi

    actual_sha256="$(shasum -a 256 "$archive_path" | awk '{print $1}')"
    if [ "$actual_sha256" != "$PGVECTOR_SHA256" ]; then
        echo -e "${RED}[错误] pgvector 源码 SHA256 校验失败，已停止安装。${NC}"
        echo "       expected: ${PGVECTOR_SHA256}"
        echo "       actual:   ${actual_sha256}"
        return 1
    fi

    tar -xzf "$archive_path" -C "$source_dir" --strip-components=1
    echo -e "${YELLOW}[依赖] 正在为 PostgreSQL ${server_major} 编译 pgvector ${PGVECTOR_VERSION}...${NC}"
    make -s -C "$source_dir" PG_CONFIG="$pg_config_path"
    make -s -C "$source_dir" PG_CONFIG="$pg_config_path" install
    echo -e "${GREEN}[完成] pgvector ${PGVECTOR_VERSION} 已安装到 PostgreSQL ${server_major}。${NC}"
}

ensure_external_pgvector() {
    if ! command -v psql >/dev/null 2>&1; then
        echo -e "${RED}[错误] 未找到 psql，无法配置本机 PostgreSQL。${NC}"
        print_pgvector_install_guide "16"
        return 1
    fi

    local server_major
    local available_version
    local installed_version

    server_major="$(external_psql -d postgres -Atqc "show server_version_num" 2>/dev/null | awk '{print int($1 / 10000)}' || true)"
    if [ -z "$server_major" ]; then
        echo -e "${RED}[错误] 无法连接本机 PostgreSQL，请检查端口、用户和密码。${NC}"
        return 1
    fi

    available_version="$(external_psql -d postgres -Atqc "select default_version from pg_available_extensions where name = 'vector'" 2>/dev/null || true)"
    if [ "$available_version" != "$PGVECTOR_VERSION" ]; then
        if [ -n "$available_version" ]; then
            echo -e "${YELLOW}[依赖] PostgreSQL ${server_major} 当前可用 pgvector ${available_version}，项目锁定 ${PGVECTOR_VERSION}。${NC}"
        fi
        install_pgvector_for_homebrew_postgres "$server_major"
        available_version="$(external_psql -d postgres -Atqc "select default_version from pg_available_extensions where name = 'vector'" 2>/dev/null || true)"
    fi

    if [ "$available_version" != "$PGVECTOR_VERSION" ]; then
        echo -e "${RED}[错误] PostgreSQL ${server_major} 仍未发现 pgvector ${PGVECTOR_VERSION}。${NC}"
        print_pgvector_install_guide "$server_major"
        return 1
    fi

    if ! external_psql -d "$PGVECTOR_DATABASE" -Atqc "select 1" >/dev/null 2>&1; then
        echo -e "${RED}[错误] 无法连接 pgvector 目标数据库 ${PGVECTOR_DATABASE}，未启用扩展。${NC}"
        return 1
    fi

    installed_version="$(external_psql -d "$PGVECTOR_DATABASE" -Atqc "select extversion from pg_extension where extname = 'vector'" 2>/dev/null || true)"
    if [ -z "$installed_version" ]; then
        echo -e "${YELLOW}[依赖] 正在为数据库 ${PGVECTOR_DATABASE} 启用 pgvector ${PGVECTOR_VERSION}...${NC}"
        external_psql -d "$PGVECTOR_DATABASE" -qc "create extension vector version '${PGVECTOR_VERSION}'"
    elif [ "$installed_version" != "$PGVECTOR_VERSION" ]; then
        echo -e "${YELLOW}[依赖] 正在将数据库 ${PGVECTOR_DATABASE} 的 pgvector 从 ${installed_version} 升级到 ${PGVECTOR_VERSION}...${NC}"
        external_psql -d "$PGVECTOR_DATABASE" -qc "alter extension vector update to '${PGVECTOR_VERSION}'"
    fi

    installed_version="$(external_psql -d "$PGVECTOR_DATABASE" -Atqc "select extversion from pg_extension where extname = 'vector'")"
    echo -e "${GREEN}[完成] 数据库 ${PGVECTOR_DATABASE} 已启用 pgvector ${installed_version}。${NC}"
}

ensure_bundled_pgvector() {
    local attempts=30
    local installed_version=""

    while ! docker compose -f docker-compose.infra.yml exec -T postgres \
        pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; do
        attempts=$((attempts - 1))
        if [ "$attempts" -le 0 ]; then
            echo -e "${RED}[错误] Bundled PostgreSQL 未在预期时间内就绪。${NC}"
            return 1
        fi
        sleep 1
    done

    docker compose -f docker-compose.infra.yml exec -T postgres \
        psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        -qc "create extension if not exists vector"
    installed_version="$(docker compose -f docker-compose.infra.yml exec -T postgres \
        psql -X -Atqc "select extversion from pg_extension where extname = 'vector'" \
        -U "$POSTGRES_USER" -d "$POSTGRES_DB")"
    echo -e "${GREEN}[完成] Bundled 数据库 ${POSTGRES_DB} 已启用 pgvector ${installed_version}。${NC}"
}

if ! command -v docker >/dev/null 2>&1; then
    echo -e "${RED}[错误] 未找到 Docker。Milvus / bundled PostgreSQL 需要 Docker。${NC}"
    exit 1
fi

if [ "$START_BUNDLED_POSTGRES" = "true" ]; then
    echo -e "${YELLOW}[步骤 1/2] 启动 bundled PostgreSQL + Milvus...${NC}"
    COMPOSE_SERVICES=()
    DB_CONFIG_MODE="bundled"
    DB_CONFIG_DB="${POSTGRES_DB}"
    DB_CONFIG_USER="${POSTGRES_USER}"
    DB_CONFIG_PASSWORD="${POSTGRES_PASSWORD}"
else
    echo -e "${YELLOW}[步骤 1/2] 启动 Milvus（跳过 bundled PostgreSQL）...${NC}"
    COMPOSE_SERVICES=(milvus)
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
    echo "4. Provider 请求已直连；Milvus 可稍后再启动。"
    echo ""
    echo "5. 如果本机已有 PostgreSQL，请在 Settings -> 知识库中配置端口、库名、账号和密码。"
    echo "   如需命令行强制覆盖，可设置专用变量，例如："
    echo "   PUDDINGCLAW_POSTGRES_MODE=external \\"
    echo "   PUDDINGCLAW_DATABASE_URL=postgresql+asyncpg://<user>:<password>@127.0.0.1:<port>/<database> \\"
    echo "   ./scripts/start-local-infra.sh"
    exit 1
fi

if [ "$START_BUNDLED_POSTGRES" = "true" ]; then
    ensure_bundled_pgvector
else
    ensure_external_pgvector
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
    echo "    本脚本不会启动或替换你的本机 PostgreSQL，只维护目标库的 pgvector 扩展。"
    echo "    database:      ${DB_CONFIG_DB}"
    echo "    username:      ${DB_CONFIG_USER}"
    echo "    password:      ${DB_CONFIG_PASSWORD:-<empty>}"
    if [ "$PGVECTOR_DATABASE" != "$DB_CONFIG_DB" ]; then
        echo "    pgvector db:   ${PGVECTOR_DATABASE}"
    fi
fi
echo "  Milvus:          grpc://localhost:19530"
echo "  MinerU API:      http://localhost:8002  (本机运行，不由本脚本启动)"
echo ""
echo -e "${BLUE}[提示] 如需启动本机 MinerU：${NC}"
echo "  python scripts/setup-mineru.py --foreground"
echo "  # 或使用已有 conda 环境：MINERU_PORT=8002 scripts/start-mineru-host.sh"
echo "  # MinerU 地址会写入 backend/config.json 的 knowledge.mineru.base_url"
echo ""
echo -e "${BLUE}[提示] 本机 backend 推荐环境变量：${NC}"
if [ "$START_BUNDLED_POSTGRES" = "true" ]; then
    echo "  已写入 backend/config.json；backend 会使用 Settings 中的数据库配置。"
    echo "  如需命令行强制覆盖，可设置："
    echo "  PUDDINGCLAW_DATABASE_URL=postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_PORT}/${POSTGRES_DB}"
else
    echo "  已写入 backend/config.json；如需覆盖，可在 Settings -> 知识库 -> Catalog Database 中调整。"
fi
echo ""
echo -e "${GREEN}[完成] 基础设施启动命令已执行。${NC}"
