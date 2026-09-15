#!/usr/bin/env bash
#
# 在本机跑一遍 CI 的必过门禁。
#
# **为什么需要这个脚本。** GitHub Actions 上这个仓库**一次都没跑过**。
# 2026-09-05 用 API 逐条核过，workflow 文件这边没有问题：
#
#   gh api repos/heyemlee/ai-helpdesk/actions/permissions
#     → {"enabled": true, "allowed_actions": "all"}          仓库级 Actions 开着
#   gh api repos/heyemlee/ai-helpdesk/actions/workflows
#     → total_count 1，"CI"，path .github/workflows/ci.yml，state "active"
#        但 updated_at 仍是创建时的 2026-08-21——**文件后来改过（ed6d621 加了
#        workflow_dispatch），GitHub 却从没重新读过它**
#   gh api repos/heyemlee/ai-helpdesk/actions/runs   → total_count 0
#   gh api .../commits/cae9715/check-runs            → total_count 0
#   提交确实推上去了：origin/main = cae9715，pushed_at 2026-09-04
#
# 文件本身也查过：UTF-8 无 BOM、无 CRLF、路径正确、YAML 合法、
# `on:` 是 push[main,master] + pull_request + workflow_dispatch，没有路径过滤。
#
# 也就是说原因在**仓库之外**（账号级 Actions 策略 / 计费 / 账号被限），
# 本仓库改不动，要用户去 GitHub 上处理。在它修好之前，
# **门禁的内容修好了却一次没跑过，等于没有门禁**——所以至少要有一条命令
# 能在本机把同一组检查跑完。
#
# 步骤与 .github/workflows/ci.yml 一一对应，改了那边这边也要跟着改。
# 唯一的差别是 Docker 镜像那两条：默认跳过（需要 Docker 引擎在跑），
# 用 `--with-images` 打开。
#
#     scripts/ci-local.sh                # 后端 + 前端 + 契约
#     scripts/ci-local.sh --with-images  # 再加两个镜像构建
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"

# 契约仓库钉在这个 tag 上。三处必须一致：这里、ci.yml 的 `ref:`、
# tests/test_contract_fixtures.py 的 PINNED_TAG。
CONTRACT_TAG="v1.12"
CONTRACT_DIR="${A2A_CONTRACT_DIR:-$ROOT/../a2a-contract}"

WITH_IMAGES=0
[ "${1:-}" = "--with-images" ] && WITH_IMAGES=1

step() { printf '\n\033[1;34m── %s\033[0m\n' "$1"; }
fail() { printf '\n\033[1;31m✗ %s\033[0m\n' "$1"; exit 1; }

PY="$BACKEND/.venv/bin/python"
[ -x "$PY" ] || fail "找不到 $PY —— 先建后端 venv 并装 requirements.txt"

# 后端测试不发真实网络请求，占位 key 只是满足启动期配置校验。
# `env -u ANTHROPIC_BASE_URL`：从带网关配置的 shell 里跑时，那个变量会把
# 测试指向真实网关，本地一跑就产生费用。
BACKEND_ENV=(env -u ANTHROPIC_BASE_URL ANTHROPIC_API_KEY=test-key)

step "后端 lint（ruff）"
(cd "$BACKEND" && "$PY" -m ruff check .) || fail "ruff"

step "后端测试（pytest）"
(cd "$BACKEND" && "${BACKEND_ENV[@]}" "$PY" -m pytest -q) || fail "后端测试"

step "契约用例（a2a-contract $CONTRACT_TAG）"
if [ -f "$CONTRACT_DIR/fixtures/index.json" ]; then
    # A2A_CONTRACT_REQUIRED=1：契约仓库找不到时**失败**而不是跳过。
    # 一个会自动跳过的门禁等于没有门禁，而且它坏掉的时候报告是绿的。
    (cd "$BACKEND" && "${BACKEND_ENV[@]}" \
        A2A_CONTRACT_DIR="$CONTRACT_DIR" A2A_CONTRACT_REQUIRED=1 \
        "$PY" -m pytest -q tests/test_contract_fixtures.py) || fail "契约用例"
else
    fail "找不到 a2a-contract（$CONTRACT_DIR）。checkout 到 tag $CONTRACT_TAG，
或用 A2A_CONTRACT_DIR 指过去：
    git clone --depth 1 --branch $CONTRACT_TAG <a2a-contract> ../a2a-contract"
fi

step "前端干净依赖安装（npm ci）"
# 不是 npm install：lock 与 package.json 对不上时必须在这里失败，
# 那说明 lock 该更新了，是一次要进提交的改动。
(cd "$FRONTEND" && npm ci) || fail "npm ci —— lock 与 package.json 对不上？"

step "前端类型检查（tsc）"
(cd "$FRONTEND" && npm run typecheck) || fail "typecheck"

step "前端测试（vitest）"
(cd "$FRONTEND" && npm test) || fail "前端测试"

step "前端生产构建"
# 单测挡不住构建：2026-09-04 那次少一个类型导入，48 个单测全绿、构建是挂的。
(cd "$FRONTEND" && npm run build) || fail "前端构建"

if [ "$WITH_IMAGES" = "1" ]; then
    step "镜像构建：frontend"
    docker build -t ai-helpdesk-frontend:ci "$FRONTEND" || fail "前端镜像"
    step "镜像构建：backend"
    docker build -t ai-helpdesk:ci --target production "$BACKEND" || fail "后端镜像"
else
    printf '\n\033[1;33m跳过两个镜像构建（加 --with-images 打开，需要 Docker 引擎在跑）\033[0m\n'
fi

printf '\n\033[1;32m✓ 门禁全部通过\033[0m\n'
