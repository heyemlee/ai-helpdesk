#!/bin/bash

# ai-helpdesk 智能客服系统 - Docker 部署脚本


set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 路径全部从脚本自身位置推导，因此在任何目录执行都成立。
# compose 文件在仓库根，.env / data / logs / config 在 backend/。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"

PROJECT_NAME="ai-helpdesk"
COMPOSE_FILE="$ROOT/docker-compose.yml"
ENV_FILE="$BACKEND/.env"
# Compose 只读 compose 文件同目录的 .env 做 ${VAR} 替换，和上面那份用途不同。
ROOT_ENV_FILE="$ROOT/.env"

# Redis 开了 requirepass。compose 里写的是 ${REDIS_PASSWORD:-aihelpdesk123}，
# 这里用同一套取值规则，否则 redis-cli 会拿到 NOAUTH。
REDIS_PASSWORD="${REDIS_PASSWORD:-aihelpdesk123}"

# Compose v2 是 docker 的子命令，v1 是独立可执行文件，两者都接受。
if docker compose version &> /dev/null; then
    COMPOSE_BIN=(docker compose)
elif command -v docker-compose &> /dev/null; then
    COMPOSE_BIN=(docker-compose)
else
    COMPOSE_BIN=()
fi

# 所有 compose 调用都显式带上文件和项目名，不依赖工作目录，
# 也不受仓库目录改名影响。
compose() {
    if [ ${#COMPOSE_BIN[@]} -eq 0 ]; then
        print_error "Docker Compose 未安装，请先安装 Docker Compose"
        exit 1
    fi
    "${COMPOSE_BIN[@]}" -f "$COMPOSE_FILE" -p "$PROJECT_NAME" "$@"
}

# 函数：打印信息
print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 函数：检查依赖
check_dependencies() {
    print_info "检查依赖..."

    if ! command -v docker &> /dev/null; then
        print_error "Docker 未安装，请先安装 Docker"
        exit 1
    fi

    if [ ${#COMPOSE_BIN[@]} -eq 0 ]; then
        print_error "Docker Compose 未安装（docker compose 和 docker-compose 都不可用）"
        exit 1
    fi

    print_info "依赖检查完成"
}

# 函数：创建必要的目录
create_directories() {
    print_info "创建必要的目录..."

    mkdir -p "$BACKEND/data"
    mkdir -p "$BACKEND/logs"
    mkdir -p "$BACKEND/config/nginx/ssl"
    mkdir -p "$BACKEND/config/grafana/provisioning"
    mkdir -p "$BACKEND/config/grafana/dashboards"
    mkdir -p "$BACKEND/config/alerts"

    print_info "目录创建完成"
}

# 函数：检查环境变量
check_env_file() {
    print_info "检查环境变量配置..."

    # 仓库根的 .env：只供 compose 做 ${VAR} 替换。缺失时 compose 会静默回退到
    # 内置默认值，不算致命，但会让这里配的密码看起来"不生效"，所以一并补上。
    if [ ! -f "$ROOT_ENV_FILE" ]; then
        if [ -f "$ROOT/.env.example" ]; then
            cp "$ROOT/.env.example" "$ROOT_ENV_FILE"
            print_info "已创建 $ROOT_ENV_FILE（compose 变量替换）"
        else
            print_warn "仓库根缺少 .env 和 .env.example，compose 将使用内置默认值"
        fi
    fi

    if [ ! -f "$ENV_FILE" ]; then
        print_warn "backend/.env 不存在，从 backend/.env.example 创建..."

        if [ -f "$BACKEND/.env.example" ]; then
            cp "$BACKEND/.env.example" "$ENV_FILE"
            print_info "已创建 $ENV_FILE，请编辑配置"
            print_warn "特别注意：请设置 ANTHROPIC_API_KEY"
        else
            print_error "backend/.env.example 不存在"
            exit 1
        fi
    else
        print_info "环境变量配置文件已存在"
    fi
}

# 函数：构建镜像
build_images() {
    print_info "构建 Docker 镜像..."

    compose build --no-cache

    print_info "镜像构建完成"
}

# 函数：启动服务
start_services() {
    print_info "启动服务..."

    compose up -d

    print_info "服务启动完成"
}

# 函数：停止服务
stop_services() {
    print_info "停止服务..."

    compose down

    print_info "服务已停止"
}

# 函数：重启服务
restart_services() {
    print_info "重启服务..."

    compose restart

    print_info "服务已重启"
}

# 函数：查看服务状态
status_services() {
    print_info "服务状态:"

    compose ps
}

# 函数：查看日志
view_logs() {
    local service=$1

    if [ -z "$service" ]; then
        print_info "查看所有服务日志..."
        compose logs -f
    else
        print_info "查看 $service 服务日志..."
        compose logs -f "$service"
    fi
}

# 函数：健康检查
health_check() {
    print_info "执行健康检查..."

    # 等待服务启动
    sleep 10

    # 检查主应用
    if curl -sf http://localhost:8000/health > /dev/null; then
        print_info "✓ 主应用健康"
    else
        print_error "✗ 主应用不健康"
    fi

    # 检查 Redis
    if compose exec -T redis redis-cli --no-auth-warning -a "$REDIS_PASSWORD" ping | grep -q PONG; then
        print_info "✓ Redis 健康"
    else
        print_error "✗ Redis 不健康"
    fi

    # 检查 Postgres
    if curl -sf http://localhost:8001/api/v1/heartbeat > /dev/null; then
        print_info "✓ Postgres 健康"
    else
        print_error "✗ Postgres 不健康"
    fi

    # 检查 Prometheus
    if curl -sf http://localhost:9090/-/healthy > /dev/null; then
        print_info "✓ Prometheus 健康"
    else
        print_error "✗ Prometheus 不健康"
    fi
}

# 函数：清理资源
cleanup() {
    print_warn "清理所有资源（包括数据卷）..."

    read -p "确认清理？这将删除所有数据 (y/N): " -n 1 -r
    echo

    if [[ $REPLY =~ ^[Yy]$ ]]; then
        compose down -v
        print_info "清理完成"
    else
        print_info "清理已取消"
    fi
}

# 函数：备份数据
backup_data() {
    local backup_dir="$ROOT/backups/$(date +%Y%m%d_%H%M%S)"

    print_info "备份数据到 $backup_dir..."

    mkdir -p "$backup_dir"

    # 备份 Redis 数据
    compose exec -T redis redis-cli --no-auth-warning -a "$REDIS_PASSWORD" SAVE
    docker cp ai-helpdesk-redis:/data/dump.rdb "$backup_dir/"

    # 备份 Postgres 数据
    docker exec ai-helpdesk-postgres pg_dump -U "${POSTGRES_USER:-aihelpdesk}" "${POSTGRES_DB:-aihelpdesk}" > "$backup_dir/postgres.sql"

    # 备份配置
    cp "$ENV_FILE" "$backup_dir/"
    cp -r "$BACKEND/config" "$backup_dir/"

    print_info "备份完成: $backup_dir"
}

# 函数：恢复数据
restore_data() {
    local backup_dir=$1

    if [ -z "$backup_dir" ]; then
        print_error "请指定备份目录"
        exit 1
    fi

    # 相对路径按仓库根解析，backups/ 就建在那里
    [[ "$backup_dir" != /* ]] && backup_dir="$ROOT/$backup_dir"

    if [ ! -d "$backup_dir" ]; then
        print_error "备份目录不存在: $backup_dir"
        exit 1
    fi

    print_warn "从 $backup_dir 恢复数据..."
    read -p "确认恢复？这将覆盖现有数据 (y/N): " -n 1 -r
    echo

    if [[ $REPLY =~ ^[Yy]$ ]]; then
        # 停止服务
        compose stop

        # 恢复 Redis 数据
        docker cp "$backup_dir/dump.rdb" ai-helpdesk-redis:/data/

        # 恢复 Postgres 数据
        docker exec -i ai-helpdesk-postgres psql -U "${POSTGRES_USER:-aihelpdesk}" "${POSTGRES_DB:-aihelpdesk}" < "$backup_dir/postgres.sql"

        # 恢复配置
        cp "$backup_dir/.env" "$ENV_FILE"
        rm -rf "$BACKEND/config"
        cp -r "$backup_dir/config" "$BACKEND/config"

        # 启动服务
        compose start

        print_info "恢复完成"
    else
        print_info "恢复已取消"
    fi
}

# 函数：显示帮助信息
show_help() {
    cat << EOF
ai-helpdesk 智能客服系统 - Docker 部署脚本

用法: ./docker-deploy.sh [命令]

命令:
    install     初始化安装（检查依赖、创建目录、构建镜像）
    start       启动所有服务
    stop        停止所有服务
    restart     重启所有服务
    status      查看服务状态
    logs        查看服务日志（可选指定服务名）
    health      执行健康检查
    build       重新构建镜像
    cleanup     清理所有资源（包括数据）
    backup      备份数据
    restore     恢复数据（需指定备份目录）
    help        显示此帮助信息

示例:
    ./docker-deploy.sh install
    ./docker-deploy.sh start
    ./docker-deploy.sh logs ai-helpdesk
    ./docker-deploy.sh backup
    ./docker-deploy.sh restore backups/20231201_120000

环境变量:
    在 .env 文件中配置相关参数

EOF
}

# 主函数
main() {
    case "${1:-help}" in
        install)
            check_dependencies
            check_env_file
            create_directories
            build_images
            print_info "安装完成！运行 './docker-deploy.sh start' 启动服务"
            ;;
        start)
            check_env_file
            start_services
            health_check
            ;;
        stop)
            stop_services
            ;;
        restart)
            restart_services
            ;;
        status)
            status_services
            ;;
        logs)
            view_logs "$2"
            ;;
        health)
            health_check
            ;;
        build)
            build_images
            ;;
        cleanup)
            cleanup
            ;;
        backup)
            backup_data
            ;;
        restore)
            restore_data "$2"
            ;;
        help|--help|-h)
            show_help
            ;;
        *)
            print_error "未知命令: $1"
            show_help
            exit 1
            ;;
    esac
}

# 执行主函数
main "$@"