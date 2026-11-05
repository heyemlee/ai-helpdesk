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

# compose 给 ${VAR} 取值的顺序是：shell 环境变量 > 仓库根 .env > compose 文件里写的默认值。
# 脚本里的 "${VAR:-默认值}" 只管得到第一层和第三层，这个函数补中间那层：
# shell 里没有的变量，从仓库根 .env 读进来。少了它，按 .env.example 的写法在 .env 里
# 改了端口，compose 跟着变，脚本却还在探默认端口。
#
# 只取点名的变量，不 source 整个文件——.env 不是 shell 脚本。认 KEY=VALUE 和
# KEY: VALUE（可带 export、可加引号、未加引号时 " #" 之后算注释），这几种写法的取值
# 拿 `docker compose config` 对照过，是一致的。不做值里的 ${VAR} 嵌套展开和引号内的
# 转义，端口这类简单值用不到。
load_compose_env() {
    local name line value dq='"' sq="'"

    [ -f "$ROOT_ENV_FILE" ] || return 0

    for name in "$@"; do
        # shell 里设置过（哪怕是空值）就以 shell 为准，compose 也是这个优先级
        if [ -n "${!name+set}" ]; then
            continue
        fi

        # 同一个变量写了多次时，compose 以最后一次为准
        line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${name}[[:space:]]*[=:]" "$ROOT_ENV_FILE" | tail -n 1)"
        if [ -z "$line" ]; then
            continue
        fi

        value="${line#*[=:]}"
        value="${value#"${value%%[![:space:]]*}"}"
        case "$value" in
            "$dq"*) value="${value#?}"; value="${value%%"$dq"*}" ;;
            "$sq"*) value="${value#?}"; value="${value%%"$sq"*}" ;;
            *)      value="${value%% #*}"; value="${value%"${value##*[![:space:]]}"}" ;;
        esac

        printf -v "$name" '%s' "$value"
    done

    return 0
}

# 两个应用实例发布到宿主机的端口，健康检查探的就是它们。
# 变量名和默认值与 docker-compose.yml 的 ports 一字不差，改那边必须改这边
# （backend/tests/test_restore_ordering.py 会拿两个文件对照）。
# 容器内固定是 8000；宿主机的 8000 是同机 ai-sql-agent 的，不是这个项目的。
load_compose_env AI_HELPDESK_PORT AI_HELPDESK_CUSTOMER_PORT
AI_HELPDESK_PORT="${AI_HELPDESK_PORT:-8080}"
AI_HELPDESK_CUSTOMER_PORT="${AI_HELPDESK_CUSTOMER_PORT:-8081}"

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

    # SQL Agent（docker-compose.yml 的 sql-agent）自己的 env 文件和 Key 文件。缺了不影响别的服务，
    # 只是它起不来（生产就绪检查不过）。Key 不在这里替你生成：员工实例那一把要同时写进 backend/.env
    if [ ! -f "$DEPLOY/sql-agent/sql-agent.env" ] && [ -f "$DEPLOY/sql-agent/sql-agent.env.example" ]; then
        cp "$DEPLOY/sql-agent/sql-agent.env.example" "$DEPLOY/sql-agent/sql-agent.env"
        print_warn "已创建 deploy/sql-agent/sql-agent.env，请填 SQL Agent 的模型凭据"
    fi
    if [ ! -f "$DEPLOY/secrets/sql_agent_keys.json" ]; then
        print_warn "缺 deploy/secrets/sql_agent_keys.json（SQL Agent 的 Key），模板见 deploy/sql-agent/sql_agent_keys.example.json"
    fi
    if [ ! -d "$DEPLOY/sql-agent/data/semantic" ] || [ ! -d "$DEPLOY/sql-agent/data/simple" ]; then
        print_warn "缺 deploy/sql-agent/data/semantic 或 simple（SQL Agent 的语义层和业务库，生成方法见 ai-sql-agent/tools/simple_erp/README.md）"
    fi
}

# 函数：构建镜像
build_images() {
    print_info "构建 Docker 镜像..."

    # 镜像里记下这是哪一版：事件日志的 turn_started 和评测报告都记它（docker-compose.yml
    # 把它作为构建参数传进去）。外面设了就用设的；有没提交的改动时带 -dirty
    if [ -z "${AI_HELPDESK_RELEASE:-}" ]; then
        AI_HELPDESK_RELEASE="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo dev)"
        if [ "$AI_HELPDESK_RELEASE" != dev ] && [ -n "$(git -C "$ROOT" status --porcelain 2>/dev/null)" ]; then
            AI_HELPDESK_RELEASE="$AI_HELPDESK_RELEASE-dirty"
        fi
    fi
    export AI_HELPDESK_RELEASE
    print_info "镜像版本: $AI_HELPDESK_RELEASE"

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

# 容器在不在（不管跑没跑）。SQL Agent 是后加的服务：没建过它的环境里 `compose start` 会报错
has_container() {
    docker inspect "$1" > /dev/null 2>&1
}

# SQL Agent 不发布宿主机端口（发布了的话，任何容器都能经 host.docker.internal 连到它），
# 只能在它自己的容器里探。/readyz 查的是它的依赖：记忆库、审计库、模型凭据
sql_agent_ready() {
    compose exec -T sql-agent python -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=5)" \
        > /dev/null 2>&1
}

# 函数：健康检查
health_check() {
    print_info "执行健康检查..."

    # 等待服务启动
    sleep 10

    # 检查主应用（员工实例 ai-helpdesk）
    #
    # 探的必须是 compose 发布到宿主机的那个端口。原先写死 8000：后来宿主机端口
    # 挪到 8080 之后，8000 只剩容器**内**还在用，宿主机的 8000 是同机的 ai-sql-agent——
    # 它活着这里就报"主应用健康"，和 ai-helpdesk 自己是死是活无关。
    if curl -sf "http://localhost:${AI_HELPDESK_PORT}/health" > /dev/null; then
        print_info "✓ 主应用健康"
    else
        print_error "✗ 主应用不健康"
    fi

    # 检查客户实例（ai-helpdesk-customer，compose 只把它绑在 127.0.0.1 上）
    if curl -sf "http://127.0.0.1:${AI_HELPDESK_CUSTOMER_PORT}/health" > /dev/null; then
        print_info "✓ 客户实例健康"
    else
        print_error "✗ 客户实例不健康"
    fi

    # 检查 Nginx 入口：客户真正走的路是 80 → nginx → 客户实例。
    # 上一条直连通了不代表这条通——nginx 挂了，客户照样进不来。
    if curl -sf http://localhost:80/health > /dev/null; then
        print_info "✓ Nginx 入口健康"
    else
        print_error "✗ Nginx 入口不健康"
    fi

    # 检查 Redis
    if compose exec -T redis redis-cli --no-auth-warning -a "$REDIS_PASSWORD" ping | grep -q PONG; then
        print_info "✓ Redis 健康"
    else
        print_error "✗ Redis 不健康"
    fi

    # 检查 Postgres：在容器里跑 pg_isready，不去猜宿主机端口。
    #
    # 原先是 `curl localhost:8001/api/v1/heartbeat`——那是 ChromaDB 的 HTTP 心跳。
    # 存储收敛到 Postgres 时只把提示文字从 ChromaDB 改成了 Postgres，
    # 探测没动：Postgres 不讲 HTTP，compose 里也没有任何服务发布 8001，
    # 所以这一条从那天起**永远**报"不健康"。
    if compose exec -T postgres pg_isready -U "${POSTGRES_USER:-aihelpdesk}" \
           -d "${POSTGRES_DB:-aihelpdesk}" > /dev/null 2>&1; then
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

    # 检查 SQL Agent
    if sql_agent_ready; then
        print_info "✓ SQL Agent 就绪"
    else
        print_error "✗ SQL Agent 不就绪（docker logs ai-helpdesk-sql-agent 里找 production_readiness）"
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

# SQL Agent 的状态：审计和反馈（audit.db）、运行记录（runs.db）、对话记忆（memory.db），外加页面上保存的
# 模型连接（model_settings.json，明文 Key）。三个库都是 WAL 模式、进程一直在写，**不能直接拷文件**——
# 拷到的可能缺着还在 -wal 里的那部分。用 SQLite 的在线备份接口（和 sqlite3 命令行的 .backup 是同一个；
# 镜像里没有 sqlite3 命令行，用 Python 的），在容器里备好再拷出来。
#
# 语义层、业务库和 Key 文件在 deploy/ 下，随整个 deploy/ 一起备份：业务库只读挂载、没有进程在写，拷文件就是一致的
backup_sql_agent_state() {
    local dest="$1/sql-agent-state"

    if ! has_container ai-helpdesk-sql-agent; then
        print_warn "没有 SQL Agent 的容器（还没部署过），它的状态没有备份"
        return 0
    fi

    # 在跑（不是在反复重启的间隙）就在容器里做在线备份。exec 不进去也不能让整个备份停在这里
    # （set -e），退到下面直接拷卷
    if [ "$(docker inspect --format '{{.State.Running}} {{.State.Restarting}}' ai-helpdesk-sql-agent 2>/dev/null)" = "true false" ] &&
        docker exec -i ai-helpdesk-sql-agent python - <<'PY'
import os
import shutil
import sqlite3

state, out = "/app/state", "/tmp/sql-agent-backup"
shutil.rmtree(out, ignore_errors=True)
os.makedirs(out, mode=0o700)
for name in ("audit.db", "runs.db", "memory.db"):
    path = os.path.join(state, name)
    if not os.path.exists(path):
        continue
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    target = sqlite3.connect(os.path.join(out, name))
    source.backup(target)
    target.close()
    source.close()
settings = os.path.join(state, "model_settings.json")
if os.path.exists(settings):
    shutil.copy2(settings, out)
PY
    then
        # dest 先建好、源路径带 /.：和 Redis 那里一样，docker cp 的语义取决于 dest 存不存在
        mkdir -p "$dest"
        docker cp ai-helpdesk-sql-agent:/tmp/sql-agent-backup/. "$dest"
        docker exec ai-helpdesk-sql-agent rm -rf /tmp/sql-agent-backup
        return 0
    fi

    # 停着、在反复重启（比如 sql-agent.env 改坏了、就绪检查不过），或者 exec 不进去：进程没在正常写，
    # 直接拷卷里的文件，连同可能剩下的 -wal 一起拷，下次打开时 SQLite 会把它补进去
    print_warn "SQL Agent 没在正常运行，直接从它的卷里拷状态文件"
    mkdir -p "$dest"
    docker cp ai-helpdesk-sql-agent:/app/state/. "$dest"
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
    # 恢复演练实测 /data/appendonlydir/appendonly.aof.1.incr.aof 是 9.7MB，
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
    # **而且两次 cp 都成功，没有任何报错**。恢复演练踩到过一次。
    #
    # 带 `/.` 是"拷贝内容"，配合先 mkdir，两种情况下结果一样。
    mkdir -p "$backup_dir/redis-data"
    docker cp ai-helpdesk-redis:/data/. "$backup_dir/redis-data"

    # 备份 Postgres 数据
    # `--clean --if-exists` 不是可选项。
    #
    # 原先是裸 `pg_dump`：产出的 SQL 只有 CREATE，没有 DROP。往**非空**库里灌
    # 会在第一张表就撞上 `relation "conversation_log" already exists`，
    # 配合 ON_ERROR_STOP=1 直接中止——恢复演练实测到这一条，
    # 也就是说这条恢复路径**只在空库上成立**，而真实恢复场景恰恰是库非空。
    #
    # 事件日志（turn_events）只备表结构、不备数据：它带着客户原话和回答的原文，保留任务
    # 清得到库里的、清不到备份里的。恢复后日志是空的，丢的是工程数据，合规留档在
    # conversation_log 里，照常备份。
    docker exec ai-helpdesk-postgres pg_dump --clean --if-exists \
        --exclude-table-data=turn_events \
        -U "${POSTGRES_USER:-aihelpdesk}" "${POSTGRES_DB:-aihelpdesk}" \
        > "$backup_dir/postgres.sql"

    # 备份配置：应用 .env + deploy/ 下整套基础设施配置
    cp "$ENV_FILE" "$backup_dir/"
    # 面向客户实例的专用密钥（AI_HELPDESK_API_KEY）。compose 要求它存在，
    # 漏备份的话，换机器恢复后客户实例起不来。
    if [ -f "$BACKEND/.env.customer" ]; then
        cp "$BACKEND/.env.customer" "$backup_dir/"
    fi
    cp -r "$DEPLOY" "$backup_dir/deploy"

    backup_sql_agent_state "$backup_dir"

    print_info "备份完成: $backup_dir"
}

# SQL Agent 的状态换成备份里的那一份（backup_sql_agent_state 的反过程）。调用时 SQL Agent 已经停了，
# 所以和 Redis 一样用挂同一个卷的临时容器：先删旧库（连同 -wal、-shm，不删的话旧的 WAL 会被
# 回放到新库上），再拷进去，属主改回镜像里的 ai_sql（uid 10001），模型连接文件改回 0600。
# 备份里没有这一份（旧备份、当时 SQL Agent 没在跑）就不动它
restore_sql_agent_state() {
    local backup_dir="$1" volume

    if [ ! -d "$backup_dir/sql-agent-state" ]; then
        print_warn "备份里没有 SQL Agent 的状态，它的状态没有改动"
        return 0
    fi
    volume="$(docker inspect ai-helpdesk-sql-agent \
        --format '{{range .Mounts}}{{if eq .Destination "/app/state"}}{{.Name}}{{end}}{{end}}' 2>/dev/null)"
    if [ -z "$volume" ]; then
        # 备份里有这一份却放不回去：算没恢复完（调用方会明说），不能报"恢复完成"
        print_error "找不到 SQL Agent 的状态卷（容器还没建过），它的状态没有恢复：先起一次 sql-agent 再恢复"
        return 1
    fi
    if ! docker run --rm -v "$volume:/state" -v "$backup_dir/sql-agent-state:/backup:ro" alpine:3.20 \
            sh -c 'rm -f /state/*.db /state/*.db-wal /state/*.db-shm /state/model_settings.json && cp -a /backup/. /state/ && chown -R 10001:10001 /state && { [ ! -f /state/model_settings.json ] || chmod 600 /state/model_settings.json; }'; then
        print_error "SQL Agent 的状态没能恢复（卷 ${volume}）"
        return 1
    fi
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
        #
        # SQL Agent 也一起停：它的状态要整个换掉（第 4 步），而且下面整个替换 deploy/ 时，
        # 它挂着的语义层、业务库和 Key 文件会失效。没建过它的环境里不碰它（compose start 会报错）。
        local apps=(ai-helpdesk ai-helpdesk-customer frontend nginx)
        if has_container ai-helpdesk-sql-agent; then
            apps+=(sql-agent)
        fi

        print_info "1/4 停止应用（数据库保持运行）..."
        # 两个应用实例（员工 ai-helpdesk、客户 ai-helpdesk-customer）都写同一个库，
        # 恢复期间都得停；只停一个，另一个会在恢复途中继续写。
        compose stop "${apps[@]}"

        print_info "2/4 恢复 Postgres..."
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
            compose start "${apps[@]}"
            return 1
        fi
        if ! compose exec -T postgres psql -U "${POSTGRES_USER:-aihelpdesk}" \
                 -d "${POSTGRES_DB:-aihelpdesk}" -v ON_ERROR_STOP=1 \
                 < "$backup_dir/postgres.sql"; then
            print_error "Postgres 恢复失败，未继续（请检查 $backup_dir/postgres.sql）"
            # 把应用拉回来，并**明说**当前是什么状态。
            #
            # 原先这条路径直接 return，应用留在 stopped——恢复演练实测：
            # 恢复因为一个技术原因失败，服务就那么停着，而屏幕上只有一句
            # "请检查 postgres.sql"。真实事故里这是最坏的形态：**你以为你在恢复，
            # 实际上你刚把服务停了，而且没人告诉你**。
            print_warn "数据未改动。正在把应用拉回原状态..."
            compose start "${apps[@]}"
            print_error "恢复未完成，服务已按原样拉起。旧格式备份（无 --clean 的 "
            print_error "pg_dump）无法恢复到非空库，请用新备份或先清空目标库。"
            return 1
        fi

        print_info "3/4 恢复 Redis..."
        compose stop redis
        if [ -d "$backup_dir/redis-data" ]; then
            # 先清空再灌：AOF 是**目录**（appendonlydir/ 里有 manifest + base + incr），
            # 只往里拷不删旧的，会留下一份和 manifest 对不上的残留分片，
            # Redis 起来时按 manifest 读，等于恢复没生效——又是一个没有症状的形态。
            #
            # 清空必须用**挂同一个卷的临时容器**，不能 `docker exec` 到 redis 上：
            # 此刻 redis 已经 stop 了，exec 要求容器在跑。
            # （恢复演练里这一版就是这么写错的，和它上面那段注释在骂的
            # 原始缺陷是同一类错误——所以留在这里当个记号。）
            local redis_volume
            redis_volume="$(docker inspect ai-helpdesk-redis \
                --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' 2>/dev/null)"
            if [ -z "$redis_volume" ]; then
                print_error "找不到 Redis 的 /data 卷，恢复中止"
                compose start redis "${apps[@]}"
                return 1
            fi
            if ! docker run --rm -v "$redis_volume:/data" alpine:3.20 \
                    sh -c 'rm -rf /data/appendonlydir /data/dump.rdb'; then
                print_error "清空 Redis 数据目录失败"
                compose start redis "${apps[@]}"
                return 1
            fi
            if ! docker cp "$backup_dir/redis-data/." ai-helpdesk-redis:/data; then
                print_error "Redis 数据目录拷入失败"
                compose start redis "${apps[@]}"
                return 1
            fi
        elif [ -f "$backup_dir/dump.rdb" ]; then
            # 兼容改成整目录备份之前的旧备份：它们只有 dump.rdb。
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

        # 这一步失败也接着恢复配置、拉起服务（Postgres 和 Redis 已经换过了，停在这里更糟），最后明说
        local partial=0
        print_info "4/4 恢复 SQL Agent 的状态..."
        restore_sql_agent_state "$backup_dir" || partial=1

        # 恢复配置
        cp "$backup_dir/.env" "$ENV_FILE"
        if [ -f "$backup_dir/.env.customer" ]; then
            cp "$backup_dir/.env.customer" "$BACKEND/.env.customer"
        fi
        if [ -d "$backup_dir/deploy" ]; then
            # SQL Agent 的 Key、env 文件、语义层和业务库都在 deploy/ 下、都不进 git：备份里没有的
            # （比如 SQL Agent 进 compose 之前的备份）先挪开、换完再放回来，不跟着整个 deploy/ 删掉
            local keep="$ROOT/.restore-keep.$$" path
            for path in secrets sql-agent/sql-agent.env sql-agent/data; do
                if [ -e "$DEPLOY/$path" ] && [ ! -e "$backup_dir/deploy/$path" ]; then
                    mkdir -p "$keep/$(dirname "$path")"
                    mv "$DEPLOY/$path" "$keep/$path"
                fi
            done
            rm -rf "$DEPLOY"
            cp -r "$backup_dir/deploy" "$DEPLOY"
            for path in secrets sql-agent/sql-agent.env sql-agent/data; do
                if [ -e "$keep/$path" ]; then
                    mkdir -p "$DEPLOY/$(dirname "$path")"
                    mv "$keep/$path" "$DEPLOY/$path"
                    print_warn "备份里没有 deploy/${path}，保留了现在这一份"
                fi
            done
            rm -rf "$keep"
            if [ -f "$DEPLOY/secrets/sql_agent_keys.json" ] && ! grep -q '^SQL_AGENT_API_KEY=.' "$ENV_FILE"; then
                print_warn "恢复出来的 backend/.env 里没有 SQL_AGENT_API_KEY：员工实例调 SQL Agent 会被拒（401），"
                print_warn "从 deploy/secrets/sql_agent_keys.json 里 caller=ai-helpdesk 那一把补上"
            fi
        elif [ -d "$backup_dir/config" ]; then
            # 目录重整之前的备份：那时这些配置放在 backend/config/，目录是扁平的
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
        compose start "${apps[@]}"

        if [ "$partial" -eq 1 ]; then
            print_error "恢复没有全部完成：Postgres、Redis 和配置已恢复，SQL Agent 的状态还是恢复之前的那一份"
            return 1
        fi
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