#!/usr/bin/env bash
# 在一次性副本里跑后端门禁（ruff + pytest）和契约自检，和 CI 的 Backend、A2A contract 两个 job 对应。
# SQL Agent 的测试在旁边的 run-sql-agent.sh 里。
#
# 为什么不在工作区里跑：app/api/main.py 的 load_dotenv() 会读到真实的 backend/.env，
# 测试就带上了真 Key、中继地址和生产库的 DSN。这里只拷 git 认识的文件，被 gitignore 的东西
# （.env、backend/data 里明文的模型设置、日志）都进不了副本，输入和 CI 的 checkout 一样，
# 只多了还没提交的改动。环境变量也从空开始，外面的 ANTHROPIC_BASE_URL 这类变量带不进来。
#
#   run.sh                        ruff + 全部用例；要 Postgres、Redis、Kafka 的用例跳过
#   run.sh tests/test_x.py -k y   参数原样交给 pytest
#   run.sh --full                 再起一次性的 pgvector、Redis、Kafka（要 Docker，沙箱外跑）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"

step() { printf '\n\033[1;34m── %s\033[0m\n' "$1"; }
fail() { printf '\n\033[1;31m✗ %s\033[0m\n' "$1"; exit 1; }

FULL=0
if [ "${1:-}" = "--full" ]; then
    FULL=1
    shift
fi

# worktree 里没有 venv，用主检出的（porcelain 输出的第一条就是主检出）
MAIN="$(git -C "$ROOT" worktree list --porcelain | sed -n '1s/^worktree //p')"
PY="$MAIN/backend/.venv/bin/python"
[ -x "$PY" ] || fail "找不到 $PY —— 先建后端 venv 并装 requirements.txt"

COPY="$(mktemp -d "${TMPDIR:-/tmp}/aihd-test.XXXXXX")"
SERVICES="aihd-test-$$"
STARTED=0

cleanup() {
    if [ "$STARTED" = 1 ]; then
        # 只删这次自己起的容器。几个会话可能在并行跑，别人的测试库不能动
        docker rm -f "$SERVICES-pg" "$SERVICES-redis" "$SERVICES-kafka" "$SERVICES-s3" >/dev/null 2>&1 || true
    fi
    rm -rf "$COPY"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

step "拷一次性副本（只含 git 认识的文件）"
# 工作区里删掉还没提交的文件 ls-files 仍会列出来，tar 碰到会报错，所以先滤掉
git -C "$ROOT" ls-files -co --exclude-standard -z |
    while IFS= read -r -d '' file; do
        if [ -e "$ROOT/$file" ] || [ -L "$ROOT/$file" ]; then
            printf '%s\0' "$file"
        fi
    done |
    tar -C "$ROOT" --null -T - -cf - | tar -xf - -C "$COPY"

# 占位 key 只是满足启动期配置校验，测试不发真实网络请求
RUN_ENV=(env -i "HOME=$HOME" "PATH=$PATH" "TMPDIR=${TMPDIR:-/tmp}" LANG=en_US.UTF-8
    ANTHROPIC_API_KEY=test-key)

# 契约就在仓库里，副本里也有一份，和要测的代码是同一个提交，不用再从别处导出。
# 设 A2A_CONTRACT_REQUIRED：契约用例和轨迹评测里的契约用例找不到契约时失败，不跳过
CONTRACT="$COPY/a2a-contract"
[ -f "$CONTRACT/fixtures/index.json" ] || fail "副本里没有 a2a-contract/fixtures/index.json"
RUN_ENV+=("A2A_CONTRACT_DIR=$CONTRACT" A2A_CONTRACT_REQUIRED=1)

free_ports() {
    # Hold all four binds until selection finishes so this invocation cannot
    # select the same port twice. Other processes can still race after release.
    "$PY" -c 'import socket
from contextlib import ExitStack
with ExitStack() as held:
    sockets = [held.enter_context(socket.socket()) for _ in range(4)]
    for sock in sockets:
        sock.bind(("127.0.0.1", 0))
    print(*(sock.getsockname()[1] for sock in sockets))'
}

wait_for() {
    local what="$1"
    shift
    for _ in $(seq 1 60); do
        if "$@" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    fail "$what 60 秒内没起来"
}

if [ "$FULL" = 1 ]; then
    step "起一次性的 pgvector、Redis、Kafka"
    STARTED=1
    SERVICE_PORTS="$(free_ports)"
    read -r PG_PORT REDIS_PORT KAFKA_PORT S3_PORT <<<"$SERVICE_PORTS"
    PG_PASSWORD="$(openssl rand -hex 12)"

    docker run -d --rm --name "$SERVICES-pg" \
        -e POSTGRES_USER=test -e POSTGRES_PASSWORD="$PG_PASSWORD" -e POSTGRES_DB=test \
        -p "127.0.0.1:$PG_PORT:5432" --tmpfs /var/lib/postgresql/data \
        pgvector/pgvector:pg16 >/dev/null
    docker run -d --rm --name "$SERVICES-redis" -p "127.0.0.1:$REDIS_PORT:6379" \
        redis:7-alpine >/dev/null
    docker run -d --rm --name "$SERVICES-s3" -p "127.0.0.1:$S3_PORT:8333" \
        -e AWS_ACCESS_KEY_ID=attachment-test -e AWS_SECRET_ACCESS_KEY=attachment-test-secret \
        chrislusf/seaweedfs:4.52 mini -dir=/data >/dev/null
    # 配置和 ci.yml 的 kafka 服务一样。容器内外用同一个端口号：broker 把这个地址告诉客户端，
    # 下面容器里的探活和宿主机上的测试都要连得上
    docker run -d --rm --name "$SERVICES-kafka" -p "127.0.0.1:$KAFKA_PORT:$KAFKA_PORT" \
        -e KAFKA_NODE_ID=1 \
        -e KAFKA_PROCESS_ROLES=broker,controller \
        -e KAFKA_LISTENERS="PLAINTEXT://:$KAFKA_PORT,CONTROLLER://:9093" \
        -e KAFKA_ADVERTISED_LISTENERS="PLAINTEXT://localhost:$KAFKA_PORT" \
        -e KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER \
        -e KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT \
        -e KAFKA_CONTROLLER_QUORUM_VOTERS=1@localhost:9093 \
        -e KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1 \
        -e KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR=1 \
        -e KAFKA_TRANSACTION_STATE_LOG_MIN_ISR=1 \
        -e KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS=0 \
        -e KAFKA_AUTO_CREATE_TOPICS_ENABLE=false \
        apache/kafka:4.3.1 >/dev/null

    # -h 127.0.0.1：Postgres 初始化时的临时实例只听 unix socket，不带 -h 会过早报就绪
    wait_for Postgres docker exec "$SERVICES-pg" pg_isready -h 127.0.0.1 -U test -d test
    wait_for Redis docker exec "$SERVICES-redis" redis-cli ping
    wait_for S3 curl -sS -o /dev/null "http://127.0.0.1:$S3_PORT/"
    wait_for Kafka docker exec "$SERVICES-kafka" /opt/kafka/bin/kafka-broker-api-versions.sh \
        --bootstrap-server "localhost:$KAFKA_PORT"

    RUN_ENV+=("TEST_POSTGRES_DSN=postgresql://test:$PG_PASSWORD@127.0.0.1:$PG_PORT/test"
        "TEST_REDIS_URL=redis://127.0.0.1:$REDIS_PORT/0"
        "TEST_KAFKA_BOOTSTRAP=localhost:$KAFKA_PORT"
        "TEST_S3_ENDPOINT=http://127.0.0.1:$S3_PORT")
fi

step "契约自检（a2a-contract/tools/check_contract.py）"
# 和 CI 的 A2A contract job 是同一条命令。要 jsonschema、referencing，后端 venv 里经 mcp 已经带着。
# 副本里没有 .git，版本段里"tag 打没打"那一行会跳过，不影响判定
"${RUN_ENV[@]}" "$PY" "$CONTRACT/tools/check_contract.py"

cd "$COPY/backend"

step "ruff"
"${RUN_ENV[@]}" "$PY" -m ruff check .

step "pytest"
# -rs：把跳过的用例和原因列出来，要服务的用例静默跳过时不至于误以为都测过了
"${RUN_ENV[@]}" "$PY" -m pytest -q -rs -p no:cacheprovider "$@"

printf '\n\033[1;32m✓ 后端门禁通过\033[0m\n'
