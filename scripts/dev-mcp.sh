#!/usr/bin/env bash
# 启动开发用 MCP server（backend/scripts/dev_mcp_server.py），由 .mcp.json 调用。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# worktree 里没有 venv，用主检出的（porcelain 输出的第一条就是主检出）
MAIN="$(git -C "$ROOT" worktree list --porcelain | sed -n '1s/^worktree //p')"
PY="$MAIN/backend/.venv/bin/python"

# 只写 stderr：stdout 是 MCP 协议本身
if ! "$PY" -c 'import mcp' 2>/dev/null; then
    echo "$PY 里没有 mcp。把 backend/requirements-dev.txt 装进这个环境，见 docs/setup.md" >&2
    exit 1
fi

exec "$PY" "$ROOT/backend/scripts/dev_mcp_server.py"
