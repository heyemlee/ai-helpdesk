#!/usr/bin/env bash
# 本机同时启动 ai-sql-agent 和 ai-helpdesk（后端 + 前端），按 a2a-contract 的本机接法连起来：
# 先起 ai-sql-agent 并等它就绪，再带上 SQL_AGENT_URL 运行 scripts/dev.sh。
# Ctrl-C 一次停掉全部；两边的启动脚本都会先停掉自己上一轮的进程，重复运行不会撞端口。
#
# ai-sql-agent 的位置：SQL_AGENT_DIR > 同级目录 ../ai-sql-agent（和测试找 ../a2a-contract 的规则一样）。
# 端口：ai-sql-agent 读它自己 .env 的 AI_SQL_PORT（默认 8000），ai-helpdesk 读 backend/.env 的 API_PORT。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SQL_AGENT_DIR="${SQL_AGENT_DIR:-$ROOT/../ai-sql-agent}"
if [ ! -x "$SQL_AGENT_DIR/scripts/run_api.sh" ]; then
  echo "找不到 ai-sql-agent（$SQL_AGENT_DIR/scripts/run_api.sh）。放到同级目录，或者设置 SQL_AGENT_DIR。" >&2
  exit 1
fi
SQL_AGENT_DIR="$(cd "$SQL_AGENT_DIR" && pwd)"

# 读 .env 里的 KEY=VALUE（取最后一个，忽略行尾注释）；文件不存在时输出空
env_value() { sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\([^#[:space:]]*\).*/\1/p" "$2" 2>/dev/null | tail -1 || true; }

# 端口规则和两边各自的启动脚本一致：环境变量 > .env > 8000
SQL_AGENT_PORT="${AI_SQL_PORT:-$(env_value AI_SQL_PORT "$SQL_AGENT_DIR/.env")}"
SQL_AGENT_PORT="${SQL_AGENT_PORT:-8000}"
HELPDESK_PORT="${API_PORT:-$(env_value API_PORT "$ROOT/backend/.env")}"
HELPDESK_PORT="${HELPDESK_PORT:-8000}"
if [ "$SQL_AGENT_PORT" = "$HELPDESK_PORT" ]; then
  echo "ai-sql-agent（AI_SQL_PORT）和 ai-helpdesk（API_PORT）都是 $SQL_AGENT_PORT，两个不能共用端口。" >&2
  echo "按 a2a-contract 的本机接法：ai-sql-agent 用 8000，在 backend/.env 里把 API_PORT 设成 8080。" >&2
  exit 1
fi
SQL_AGENT_BASE="http://127.0.0.1:$SQL_AGENT_PORT"

# 任何一边退出（Ctrl-C、dev.sh 启动失败、ai-sql-agent 没起来）都把整组进程一起收掉，不留孤儿
trap 'trap - EXIT; trap "" INT TERM; kill 0 2>/dev/null || true' EXIT INT TERM

echo "▶ ai-sql-agent  $SQL_AGENT_BASE  （日志行带 [sql-agent] 前缀）"
(
  cd "$SQL_AGENT_DIR"
  # 用它自己的 .venv；只监听本机——本地没开鉴权，不暴露到局域网
  PATH="$SQL_AGENT_DIR/.venv/bin:$PATH" PYTHONUNBUFFERED=1 exec scripts/run_api.sh --host 127.0.0.1
) 2>&1 | sed -u 's/^/[sql-agent] /' &
sql_agent_pid=$!

# 等 /health 和 Agent Card 都能访问再起 ai-helpdesk：a2a 模式下调用方开流前要先读 Card
ready=""
for _ in $(seq 60); do  # 最多等 30 秒
  if curl -fs -o /dev/null "$SQL_AGENT_BASE/health" && curl -fs -o /dev/null "$SQL_AGENT_BASE/.well-known/agent.json"; then
    ready=1
    break
  fi
  kill -0 "$sql_agent_pid" 2>/dev/null || break  # 已经退出（比如端口被别的程序占着），不用再等
  sleep 0.5
done
if [ -z "$ready" ]; then
  echo "ai-sql-agent 没有就绪，看上面 [sql-agent] 开头的输出。" >&2
  exit 1
fi

# 本机直连 127.0.0.1。backend/.env 里的 SQL_AGENT_URL 留给 Docker 模式：
# load_dotenv 不覆盖已有的环境变量，所以这里导出的值优先。
export SQL_AGENT_URL="$SQL_AGENT_BASE"
"$ROOT/scripts/dev.sh"
