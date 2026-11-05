#!/usr/bin/env bash
# 启动 AI SQL Agent FastAPI 服务。
# 端口：环境变量 AI_SQL_PORT > .env 里的 AI_SQL_PORT > 8000（docker compose 映射宿主机端口读的也是它）。
# 启动前先停掉本仓库上一轮还在跑的 API；端口被别的程序占着时只报出来，不替它结束。
set -euo pipefail
cd "$(dirname "$0")/.."
root="$(pwd -P)"

env_port=""
if [ -f .env ]; then
  env_port="$(sed -n 's/^[[:space:]]*AI_SQL_PORT[[:space:]]*=[[:space:]]*\([^#[:space:]]*\).*/\1/p' .env | tail -1)"
fi
port="${AI_SQL_PORT:-${env_port:-8000}}"

# 本仓库的 API 进程：命令行对得上、工作目录是仓库根目录，再加上它们的直接子进程
# （uvicorn --reload 时真正占着端口的是子进程）。
old_pids=""
for pid in $(pgrep -f 'ai_sql_agent\.api\.app:app' 2>/dev/null || true); do
  [ "$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')" = "$root" ] || continue
  old_pids="$old_pids $pid $(pgrep -P "$pid" 2>/dev/null | tr '\n' ' ' || true)"
done

for pid in $(lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true); do
  case " $old_pids " in *" $pid "*) continue ;; esac
  echo "端口 $port 被别的程序占着，它不是 ai-sql-agent 的进程，不会替它结束：" >&2
  ps -o pid=,command= -p "$pid" >&2 || true
  echo "先停掉它，或者在 .env 里给 AI_SQL_PORT 换一个端口。" >&2
  exit 1
done

if [ -n "${old_pids// /}" ]; then
  echo "停止上一轮还在跑的 API 进程：$old_pids"
  kill $old_pids 2>/dev/null || true
  for _ in $(seq 50); do  # 最多等 10 秒，还没退出的再 kill -9
    alive=""
    for pid in $old_pids; do
      if kill -0 "$pid" 2>/dev/null; then alive="$alive $pid"; fi
    done
    [ -z "$alive" ] && break
    sleep 0.2
  done
  if [ -n "$alive" ]; then kill -9 $alive 2>/dev/null || true; fi
fi

exec uvicorn ai_sql_agent.api.app:app --host 0.0.0.0 --port "$port" "$@"
