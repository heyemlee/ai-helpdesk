#!/bin/bash

# ai-helpdesk 智能客服系统 - Docker 部署脚本


set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 路径全部从脚本自身位置推导，因此在任何目录执行都成立。
# compose 文件在仓库根；应用的 .env / data / logs 在 backend/；
# Nginx / Prometheus / Grafana / Loki / Tempo / OTel 的配置在 deploy/（compose 只读挂载）。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
DEPLOY="$ROOT/deploy"

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
    mkdir -p "$DEPLOY/nginx/ssl"
    mkdir -p "$DEPLOY/grafana/provisioning"
    mkdir -p "$DEPLOY/grafana/dashboards"
    mkdir -p "$DEPLOY/prometheus/alerts"

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
    # 目录名只到秒。同一秒跑两次备份会撞车，而撞车不是"覆盖"——
    # 下面的 docker cp 会把第二份**嵌套**进第一份里（见那里的注释），
    # 恢复时读到的是第一份的陈旧数据。撞了就换个名字，别合并。
    if [ -e "$backup_dir" ]; then
        local suffix=1
        while [ -e "${backup_dir}_$suffix" ]; do suffix=$((suffix + 1)); done
        backup_dir="${backup_dir}_$suffix"
    fi

    print_info "备份数据到 $backup_dir..."

    mkdir -p "$backup_dir"

    # 备份 Redis 数据
    compose exec -T redis redis-cli --no-auth-warning -a "$REDIS_PASSWORD" SAVE
    # 拷**整个 /data**，不是 dump.rdb。
    #
    # 原先是 `docker cp …:/data/dump.rdb`。这个 Redis 跑的是
    # `--appendonly yes`（docker-compose.yml 的 command），**权威数据在 AOF 里**：
    # 2026-09-08 实测 /data/appendonlydir/appendonly.aof.1.incr.aof 是 9.7MB，
    # 而 dump.rdb 是 **89 字节**——空 RDB 的大小。
    #
    # 也就是说旧写法两个方向都是空操作：备份拷走 89 字节的空文件，
    # 恢复把它拷回去而 Redis 在 AOF 模式下**根本不读它**。
    # 而 `docker cp` 会成功，脚本会打印"备份完成"——**没有任何症状**。
    #
    # 不先做 BGREWRITEAOF 是有意的：AOF 默认 appendfsync everysec，
    # 直接拷最多丢 1 秒的写入，而那 1 秒就是这套备份的 RPO 下限；
    # 为省这 1 秒去调 redis-cli 会把备份和「拿得到密码」耦在一起。
    # 注意写法：`mkdir` + 源路径带 `/.`。
    #
    # `docker cp src dest` 的语义**取决于 dest 存不存在**：不存在就把 src 拷成
    # dest，存在就把 src 拷**进** dest。写成 `cp …:/data "$dir/redis-data"` 时，
    # 只要 redis-data 已经存在（同秒撞车、重跑、手工试过一次），就会变成
    # redis-data/data/…——多一层，恢复时按老路径读到的是陈旧数据，
    # **而且两次 cp 都成功，没有任何报错**。2026-09-08 的演练踩到过一次。
    #
    # 带 `/.` 是"拷贝内容"，配合先 mkdir，两种情况下结果一样。
    mkdir -p "$backup_dir/redis-data"
    docker cp ai-helpdesk-redis:/data/. "$backup_dir/redis-data"

    # 备份 Postgres 数据
    # `--clean --if-exists` 不是可选项。
    #
    # 原先是裸 `pg_dump`：产出的 SQL 只有 CREATE，没有 DROP。往**非空**库里灌
    # 会在第一张表就撞上 `relation "conversation_log" already exists`，
    # 配合 ON_ERROR_STOP=1 直接中止——2026-09-08 的恢复演练实测到这一条，
    # 也就是说这条恢复路径**只在空库上成立**，而真实恢复场景恰恰是库非空。
    docker exec ai-helpdesk-postgres pg_dump --clean --if-exists \
        -U "${POSTGRES_USER:-aihelpdesk}" "${POSTGRES_DB:-aihelpdesk}" \
        > "$backup_dir/postgres.sql"

    # 备份配置：应用 .env + deploy/ 下整套基础设施配置
    cp "$ENV_FILE" "$backup_dir/"
    cp -r "$DEPLOY" "$backup_dir/deploy"

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
        # 顺序很重要，原先是错的：先 `compose stop` 停掉**全部**服务，
        # 再往**已经停掉**的 Postgres 容器里 `docker exec psql`——exec 要求容器
        # 在跑，所以那一步必然失败。
        #
        # 后果不是"静默成功"（`set -e` 会在那里中止，退出码非零，看得见），
        # 而是**恢复停在半路**：Redis 的 dump.rdb 已经换掉了，Postgres 一行没进，
        # 配置没恢复，所有服务停着。两个存储从此对不上，而现场是"命令报错了"
        # ——最容易被当成一次环境问题重试，重试一次 Redis 又被换一遍。
        #
        # 正确的顺序分三段：
        #   1. 只停会写数据的应用，让数据库继续跑；
        #   2. Postgres 在**运行中**恢复；Redis 必须**停下来**换 dump.rdb
        #      （跑着的 Redis 不会去读磁盘上的 rdb，而且退出时会用内存里的数据
        #      把它覆盖掉——照原样 cp 进去等于什么都没做）；
        #   3. 最后再把应用拉起来。
        print_info "1/3 停止应用（数据库保持运行）..."
        compose stop ai-helpdesk frontend nginx

        print_info "2/3 恢复 Postgres..."
        compose up -d postgres
        # 等它真的能接连接再灌，刚 up 的容器可能还在初始化
        local ready=0
        for _ in $(seq 1 30); do
            if compose exec -T postgres pg_isready -U "${POSTGRES_USER:-aihelpdesk}" \
                   -d "${POSTGRES_DB:-aihelpdesk}" >/dev/null 2>&1; then
                ready=1
                break
            fi
            sleep 1
        done
        if [ "$ready" -ne 1 ]; then
            print_error "Postgres 未能就绪，恢复中止（数据未改动）"
            compose start ai-helpdesk frontend nginx
            return 1
        fi
        if ! compose exec -T postgres psql -U "${POSTGRES_USER:-aihelpdesk}" \
                 -d "${POSTGRES_DB:-aihelpdesk}" -v ON_ERROR_STOP=1 \
                 < "$backup_dir/postgres.sql"; then
            print_error "Postgres 恢复失败，未继续（请检查 $backup_dir/postgres.sql）"
            # 把应用拉回来，并**明说**当前是什么状态。
            #
            # 原先这条路径直接 return，应用留在 stopped——2026-09-08 演练实测：
            # 恢复因为一个技术原因失败，服务就那么停着，而屏幕上只有一句
            # "请检查 postgres.sql"。真实事故里这是最坏的形态：**你以为你在恢复，
            # 实际上你刚把服务停了，而且没人告诉你**。
            print_warn "数据未改动。正在把应用拉回原状态..."
            compose start ai-helpdesk frontend nginx
            print_error "恢复未完成，服务已按原样拉起。旧格式备份（无 --clean 的 "
            print_error "pg_dump）无法恢复到非空库，请用新备份或先清空目标库。"
            return 1
        fi

        print_info "3/3 恢复 Redis..."
        compose stop redis
        if [ -d "$backup_dir/redis-data" ]; then
            # 先清空再灌：AOF 是**目录**（appendonlydir/ 里有 manifest + base + incr），
            # 只往里拷不删旧的，会留下一份和 manifest 对不上的残留分片，
            # Redis 起来时按 manifest 读，等于恢复没生效——又是一个没有症状的形态。
            #
            # 清空必须用**挂同一个卷的临时容器**，不能 `docker exec` 到 redis 上：
            # 此刻 redis 已经 stop 了，exec 要求容器在跑。
            # （2026-09-08 演练里这一版就是这么写错的，和它上面那段注释在骂的
            # 原始缺陷是同一类错误——所以留在这里当个记号。）
            local redis_volume
            redis_volume="$(docker inspect ai-helpdesk-redis \
                --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' 2>/dev/null)"
            if [ -z "$redis_volume" ]; then
                print_error "找不到 Redis 的 /data 卷，恢复中止"
                compose start redis ai-helpdesk frontend nginx
                return 1
            fi
            if ! docker run --rm -v "$redis_volume:/data" alpine:3.20 \
                    sh -c 'rm -rf /data/appendonlydir /data/dump.rdb'; then
                print_error "清空 Redis 数据目录失败"
                compose start redis ai-helpdesk frontend nginx
                return 1
            fi
            if ! docker cp "$backup_dir/redis-data/." ai-helpdesk-redis:/data; then
                print_error "Redis 数据目录拷入失败"
                compose start redis ai-helpdesk frontend nginx
                return 1
            fi
        elif [ -f "$backup_dir/dump.rdb" ]; then
            # 兼容 2026-09-08 之前的旧备份：它们只有 dump.rdb。
            # **明确警告**——AOF 模式下 Redis 不读这个文件，恢复不会生效。
            print_warn "这是旧格式备份（只有 dump.rdb）。当前 Redis 以 appendonly 模式运行，"
            print_warn "不会加载 dump.rdb —— Redis 的数据**不会**被恢复，只有 Postgres 会。"
            docker cp "$backup_dir/dump.rdb" ai-helpdesk-redis:/data/dump.rdb || true
        else
            print_error "备份里既没有 redis-data/ 也没有 dump.rdb"
            compose start redis
            return 1
        fi
        compose start redis

        # 恢复配置
        cp "$backup_dir/.env" "$ENV_FILE"
        if [ -d "$backup_dir/deploy" ]; then
            rm -rf "$DEPLOY"
            cp -r "$backup_dir/deploy" "$DEPLOY"
        elif [ -d "$backup_dir/config" ]; then
            # 2026-09-09 之前的备份：那时这些配置放在 backend/config/，目录是扁平的
            # （prometheus.yml、alerts/、grafana/ 并列），和现在 deploy/ 按服务分目录的布局
            # 对不上，直接覆盖会把 deploy/ 弄坏。配置本身在版本库里，这里只恢复 .env，
            # 配置有本地改动的话按下面的对应关系手动搬。
            print_warn "旧格式备份（config/ 而非 deploy/）：基础设施配置未自动恢复，只恢复了 backend/.env。"
            print_warn "对应关系：config/prometheus.yml → deploy/prometheus/  config/alerts → deploy/prometheus/alerts"
            print_warn "          config/grafana → deploy/grafana  config/nginx → deploy/nginx  其余 *.yml → deploy/<服务名>/"
        else
            print_warn "备份里没有配置目录，只恢复了 backend/.env"
        fi

        # 启动服务
        compose start ai-helpdesk frontend nginx

        print_info "恢复完成"
    else
        print_info "恢复已取消"
    fi
}

# 函数：显示帮助信息
show_help() {
    cat << EOF
ai-helpdesk 智能客服系统 - Docker 部署脚本

用法: scripts/docker-deploy.sh [命令]（在任何目录执行都成立）

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
    scripts/docker-deploy.sh install
    scripts/docker-deploy.sh start
    scripts/docker-deploy.sh logs ai-helpdesk
    scripts/docker-deploy.sh backup
    scripts/docker-deploy.sh restore backups/20231201_120000

环境变量:
    应用配置在 backend/.env；compose 变量替换在仓库根 .env

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
            print_info "安装完成！运行 'scripts/docker-deploy.sh start' 启动服务"
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