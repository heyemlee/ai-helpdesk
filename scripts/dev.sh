#!/usr/bin/env bash
# 本地开发：一条命令同时起后端（uvicorn，端口见 API_PORT）和前端（vite :5173）。
# Ctrl-C 会把两个进程一起收掉。
#
# 前端通过 vite.config.ts 的 proxy 把 /api 转发到后端端口（见 API_PORT），
# 浏览器只需要访问 http://localhost:5173。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"

REQUIRED_MODULES="anthropic openai redis fastapi uvicorn prometheus_client dotenv httpx"

# 找一个真的能 import 全部依赖的解释器：优先项目 .venv，其次 $PYTHON_BIN，最后 python3
pick_python() {
  for candidate in "$BACKEND/.venv/bin/python" "${PYTHON_BIN:-}" python3; do
    [ -n "$candidate" ] || continue
    command -v "$candidate" >/dev/null 2>&1 || [ -x "$candidate" ] || continue
    if "$candidate" -c "import $(echo "$REQUIRED_MODULES" | tr ' ' ',')" >/dev/null 2>&1; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

if ! PY="$(pick_python)"; then
  cat >&2 <<'MSG'
找不到装齐依赖的 Python 解释器。先建一个虚拟环境：

    cd backend
    python3 -m venv .venv
    ./.venv/bin/pip install -r requirements.txt

装好后重新运行 scripts/dev.sh。
（不想在本机装依赖的话，用 Docker：docker compose up -d --build）
MSG
  exit 1
fi

if [ ! -f "$BACKEND/.env" ]; then
  echo "缺少 $BACKEND/.env，请先从 .env.example 复制一份并填好 ANTHROPIC_API_KEY" >&2
  exit 1
fi

cleanup() {
  echo ""
  echo "正在停止前后端..."
  # 杀掉整个进程组，避免 uvicorn --reload 的子进程变孤儿
  kill 0 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# 后端端口：环境变量 > backend/.env 的 API_PORT > 8000。
# 和 app/api/main.py 的 load_dotenv() 一致——已存在的环境变量优先，.env 不覆盖。
env_api_port="$(sed -n 's/^[[:space:]]*API_PORT[[:space:]]*=[[:space:]]*\([^#[:space:]]*\).*/\1/p' "$BACKEND/.env" | tail -1)"
API_PORT="${API_PORT:-${env_api_port:-8000}}"

# 环境里已有的 ANTHROPIC_* 会盖过 backend/.env（load_dotenv 不覆盖已存在的变量）。
# Claude Code、部分 CI runner 会导出 ANTHROPIC_BASE_URL=https://api.anthropic.com，
# 于是 .env 里的第三方网关被静默顶掉，拿网关的 key 去打官方端点——唯一线索是
# 启动日志里那行 base_url。这里在起进程之前就喊出来。语义不改（环境变量仍然优先），
# 想用 .env 的值：env -u ANTHROPIC_BASE_URL scripts/dev.sh
for var in ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ANTHROPIC_MODEL OPENAI_BASE_URL OPENAI_API_KEY; do
  env_val="${!var:-}"
  [ -n "$env_val" ] || continue
  file_val="$(sed -n "s/^[[:space:]]*$var[[:space:]]*=[[:space:]]*\([^#[:space:]]*\).*/\1/p" "$BACKEND/.env" | tail -1)"
  [ -n "$file_val" ] && [ "$env_val" != "$file_val" ] || continue
  case "$var" in *KEY*) shown_env="${env_val:0:6}…"; shown_file="${file_val:0:6}…" ;; *) shown_env="$env_val"; shown_file="$file_val" ;; esac
  echo "⚠️  环境变量 $var=$shown_env 会盖过 backend/.env 里的 $shown_file" >&2
  echo "   要用 .env 的值：env -u $var scripts/dev.sh" >&2
done

echo "解释器: $PY"
echo "▶ 后端  http://localhost:$API_PORT  (API 文档 /docs)"
(
  cd "$BACKEND"
  exec "$PY" -m uvicorn app.api.main:app --host 0.0.0.0 --port "$API_PORT"
) &

if [ ! -d "$FRONTEND/node_modules" ]; then
  echo "首次运行，安装前端依赖..."
  (cd "$FRONTEND" && npm install)
fi

echo "▶ 前端  http://localhost:5173"
(
  cd "$FRONTEND"
  # 让 vite.config.ts 的 /api 代理跟着后端端口走。写 127.0.0.1 而不是 localhost：
  # localhost 在本机同时解析到 ::1 和 127.0.0.1，别的进程占了另一族时会代理到错的服务。
  export VITE_API_URL="${VITE_API_URL:-http://127.0.0.1:$API_PORT}"
  exec npm run dev
) &

wait
