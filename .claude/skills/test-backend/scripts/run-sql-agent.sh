#!/usr/bin/env bash
# 在一次性副本里跑 SQL Agent 的测试，和 CI 的 SQL Agent job 的测试几步对应：
# unittest 全量（含契约用例）、tests/test_simple_erp.py（pytest），再确认契约用例一条都没跳过。
# CI 那个 job 后面的 Demo、评测冒烟、门禁冒烟、只读账号工具这里不跑。
#
# 为什么不在 ai-sql-agent/ 里直接跑：unittest 不加载 conftest.py，挡不住本机配置。
# pydantic-settings 会读 ai-sql-agent/.env，设置页存的 runtime_data/model_settings.json 优先级更高，
# 测试就带上了本机的模型连接和凭据。这里只拷 git 认识的文件，被 gitignore 的东西进不了副本，
# 环境变量也从空开始。
#
#   run-sql-agent.sh          离线：模型走 Mock，要 pgvector 的用例跳过
#   run-sql-agent.sh --full   再起一次性的 pgvector（要 Docker，沙箱外跑）
#
# 有些用例要在本机端口上起假的模型服务，有些要 node，沙箱里跑会失败，放到沙箱外跑。
#
# Python：SQL_AGENT_PY，没设就用主检出 ai-sql-agent/.venv 的（scripts/dev-all.sh 用的也是它），
# 要装好 ai-sql-agent 的 .[dev]。CI 用 Python 3.11。副本里的代码经 PYTHONPATH 盖过 venv 里装的那份。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"

step() { printf '\n\033[1;34m── %s\033[0m\n' "$1"; }
fail() { printf '\n\033[1;31m✗ %s\033[0m\n' "$1"; exit 1; }

FULL=0
for arg in "$@"; do
    case "$arg" in
        --full) FULL=1 ;;
        # 打错参数要报错：悄悄忽略的话，跑的是缩水的门禁，结果还是绿的
        *) fail "不认识的参数：$arg（可用 --full）" ;;
    esac
done

MAIN="$(git -C "$ROOT" worktree list --porcelain | sed -n '1s/^worktree //p')"
PY="${SQL_AGENT_PY:-$MAIN/ai-sql-agent/.venv/bin/python}"
[ -x "$PY" ] || fail "找不到 $PY —— 先建 SQL Agent 的 venv：cd ai-sql-agent && uv venv --python 3.11 && uv pip install -e '.[dev]'，或者用 SQL_AGENT_PY 指定"
if ! "$PY" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 11))'; then
    printf '\033[1;33m注意：%s 不是 Python 3.11，CI 用的是 3.11\033[0m\n' "$PY"
fi

COPY="$(mktemp -d "${TMPDIR:-/tmp}/aisql-test.XXXXXX")"
PG_NAME="aisql-test-$$-pg"
STARTED=0

cleanup() {
    if [ "$STARTED" = 1 ]; then
        # 只删这次自己起的容器
        docker rm -f "$PG_NAME" >/dev/null 2>&1 || true
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

APP="$COPY/ai-sql-agent"
[ -f "$COPY/a2a-contract/fixtures/index.json" ] || fail "副本里没有 a2a-contract/fixtures/index.json"

# 模型走 Mock，不给任何凭据。契约从副本里读，找不到就失败
RUN_ENV=(env -i "HOME=$HOME" "PATH=$PATH" "TMPDIR=${TMPDIR:-/tmp}" LANG=en_US.UTF-8
    PYTHONDONTWRITEBYTECODE=1 "PYTHONPATH=$APP/src:$APP"
    AI_SQL_LOG_LEVEL=ERROR
    "A2A_CONTRACT_DIR=$COPY/a2a-contract" A2A_CONTRACT_REQUIRED=1)

if [ "$FULL" = 1 ]; then
    step "起一次性的 pgvector"
    STARTED=1
    PG_PORT="$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
    PG_PASSWORD="$(openssl rand -hex 12)"
    docker run -d --rm --name "$PG_NAME" \
        -e POSTGRES_USER=ai_sql -e POSTGRES_PASSWORD="$PG_PASSWORD" -e POSTGRES_DB=ai_sql \
        -p "127.0.0.1:$PG_PORT:5432" --tmpfs /var/lib/postgresql/data \
        pgvector/pgvector:pg16 >/dev/null
    # -h 127.0.0.1：Postgres 初始化时的临时实例只听 unix socket，不带 -h 会过早报就绪
    for _ in $(seq 1 60); do
        docker exec "$PG_NAME" pg_isready -h 127.0.0.1 -U ai_sql -d ai_sql >/dev/null 2>&1 && break
        sleep 1
    done
    docker exec "$PG_NAME" pg_isready -h 127.0.0.1 -U ai_sql -d ai_sql >/dev/null 2>&1 ||
        fail "pgvector 60 秒内没起来"
    RUN_ENV+=("AI_SQL_PGVECTOR_DSN=postgresql://ai_sql:$PG_PASSWORD@127.0.0.1:$PG_PORT/ai_sql")
fi

cd "$APP"

# 测的必须是副本里的代码，不是 venv 里装的那份。比较解析后的路径：
# TMPDIR 带不带结尾的 /、/var 和 /private/var 这类差别不算
"${RUN_ENV[@]}" "$PY" - "$APP" <<'PY' || fail "测的不是副本里的代码"
import pathlib
import sys

import ai_sql_agent

app = pathlib.Path(sys.argv[1]).resolve()
where = pathlib.Path(ai_sql_agent.__file__).resolve()
if app not in where.parents:
    sys.exit(f"ai_sql_agent 是从 {where} 导入的，不在副本 {app} 里")
PY

step "unittest（单元测试 + 契约用例；--full 时含 pgvector 集成测试）"
"${RUN_ENV[@]}" "$PY" -m unittest discover -s tests

step "pytest tests/test_simple_erp.py"
"${RUN_ENV[@]}" "$PY" -m pytest tests/test_simple_erp.py -q -p no:cacheprovider

step "契约用例确实跑到了（不是整组 skip）"
"${RUN_ENV[@]}" "$PY" - <<'PY'
import sys
import unittest

sys.path.insert(0, "tests")
suite = unittest.defaultTestLoader.loadTestsFromNames(["test_contract_fixtures", "test_business_outcome"])
result = unittest.TextTestRunner(verbosity=0).run(suite)
if not result.testsRun or result.skipped:
    print(f"契约用例被跳过了 {len(result.skipped)} 条：", file=sys.stderr)
    for case, reason in result.skipped:
        print(f"  {case}: {reason}", file=sys.stderr)
    sys.exit(1)
sys.exit(0 if result.wasSuccessful() else 1)
PY

printf '\n\033[1;32m✓ SQL Agent 测试通过\033[0m\n'
