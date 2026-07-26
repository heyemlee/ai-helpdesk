#!/usr/bin/env bash
# 本地开发：一条命令同时起后端（uvicorn :8000）和前端（vite :5173）。
# Ctrl-C 会把两个进程一起收掉。
#
# 前端通过 vite.config.js 的 proxy 把 /api 转发到 :8000，
# 浏览器只需要访问 http://localhost:5173。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"

REQUIRED_MODULES="anthropic chromadb redis fastapi uvicorn prometheus_client dotenv httpx"

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

装好后重新运行 ./dev.sh。
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

echo "解释器: $PY"
echo "▶ 后端  http://localhost:8000  (API 文档 /docs)"
(
  cd "$BACKEND"
  exec "$PY" -m uvicorn api.main:app --host 0.0.0.0 --port 8000
) &

if [ ! -d "$FRONTEND/node_modules" ]; then
  echo "首次运行，安装前端依赖..."
  (cd "$FRONTEND" && npm install)
fi

echo "▶ 前端  http://localhost:5173"
(
  cd "$FRONTEND"
  exec npm run dev
) &

wait
