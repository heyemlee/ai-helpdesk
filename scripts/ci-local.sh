#!/usr/bin/env bash
#
# 推之前在本机跑一遍 CI 的必过门禁。
#
# 算数的是 GitHub Actions：每个 PR 和 main 上都跑，main 开了分支保护，改动要走 PR，
# 下面四个 job 都过了才能合。这个脚本让你不用推就能提前看到结果。
#
# 它不碰工作区。生产的 Docker Compose 和练习环境共用这个工作区，从它起的后端还挂着
# frontend/dist，所以每一步都在一次性副本里跑：副本只有 git 认识的文件（包括没提交的改动），
# 环境变量从空开始。真实的 backend/.env、frontend/node_modules 和 frontend/dist 都用不到，
# 跑完 git status 不变。
#
# 步骤和 .github/workflows/ci.yml 的 job 一一对应，改了那边这边也要跟着改：
#
#   backend   .claude/skills/test-backend/scripts/run.sh：ruff + pytest。默认离线，
#             要 Postgres、Redis、Kafka 的用例跳过；--full 起一次性容器真跑（要 Docker，沙箱外跑）
#   contract  和 backend 是同一次 run.sh、同一次 pytest：契约用例从钉住的 tag 导出
#             （tests/test_contract_fixtures.py 的 PINNED_TAG），找不到 tag 就失败
#   frontend  npm ci → typecheck → test → build
#   images    在另一份副本里构建两个镜像。默认跳过，--with-images 打开（要 Docker 引擎在跑）
#
# 还有一处和 CI 不同：Python 和 Node 用本机的（backend/.venv、PATH 上的 node），
# CI 钉在 Python 3.12、Node 24。
#
#     scripts/ci-local.sh                # 后端（离线）+ 契约 + 前端
#     scripts/ci-local.sh --full         # 后端连上一次性的 pgvector、Redis、Kafka
#     scripts/ci-local.sh --with-images  # 再加两个镜像构建
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

step() { printf '\n\033[1;34m── %s\033[0m\n' "$1"; }
fail() { printf '\n\033[1;31m✗ %s\033[0m\n' "$1"; exit 1; }

FULL=""
WITH_IMAGES=0
for arg in "$@"; do
    case "$arg" in
        --full) FULL=1 ;;
        --with-images) WITH_IMAGES=1 ;;
        # 打错参数要报错：悄悄忽略的话，跑的是缩水的门禁，结果还是绿的
        *) fail "不认识的参数：$arg（可用 --full、--with-images）" ;;
    esac
done

COPY="$(mktemp -d "${TMPDIR:-/tmp}/aihd-ci.XXXXXX")"
trap 'rm -rf "$COPY"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# 把 git 认识的文件拷到 $1，做法和 run.sh 一样：
# 工作区里删掉还没提交的文件 ls-files 仍会列出来，tar 碰到会报错，所以先滤掉
copy_git_files() {
    mkdir "$1"
    git -C "$ROOT" ls-files -co --exclude-standard -z |
        while IFS= read -r -d '' file; do
            if [ -e "$ROOT/$file" ] || [ -L "$ROOT/$file" ]; then
                printf '%s\0' "$file"
            fi
        done |
        tar -C "$ROOT" --null -T - -cf - | tar -xf - -C "$1"
}

step "后端 + 契约（test-backend/scripts/run.sh）"
# A2A_CONTRACT_REQUIRED=1：契约仓库找不到时**失败**而不是跳过。
# 一个会自动跳过的门禁等于没有门禁，而且它坏掉的时候报告是绿的。
A2A_CONTRACT_REQUIRED=1 "$ROOT/.claude/skills/test-backend/scripts/run.sh" ${FULL:+--full} ||
    fail "后端门禁（ruff、pytest 或契约用例）"

step "拷一次性副本（只含 git 认识的文件）"
copy_git_files "$COPY/frontend-steps"
cd "$COPY/frontend-steps/frontend"

# 环境也从空开始：VITE_API_BASE_URL 会被打进产物，NODE_ENV=production 会让 npm ci 漏装
# devDependencies。AI_HELPDESK_DESIGN_DIR 指向不存在的目录，和 CI 一样跳过 sync:css：
# 本机有设计工程时，prebuild 会把 src/console.css 换成那边的版本，测的就不是 git 里这份了。
NPM_ENV=(env -i "HOME=$HOME" "PATH=$PATH" "TMPDIR=${TMPDIR:-/tmp}" LANG=en_US.UTF-8
    "AI_HELPDESK_DESIGN_DIR=$COPY/no-design-dir")
# npm ci 要联网、要写缓存，只放行代理、证书、registry 和缓存位置。
# 沙箱里 ~/.npm 不可写，缓存不命中时报 EPERM，这时用 npm_config_cache 指到 $TMPDIR 下
for name in HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy \
    NODE_EXTRA_CA_CERTS NODE_USE_SYSTEM_CA npm_config_registry npm_config_cache; do
    if [ -n "${!name:-}" ]; then
        NPM_ENV+=("$name=${!name}")
    fi
done

step "前端干净依赖安装（npm ci）"
# 不是 npm install：lock 与 package.json 对不上时必须在这里失败，
# 那说明 lock 该更新了，是一次要进提交的改动。
"${NPM_ENV[@]}" npm ci || fail "npm ci —— lock 与 package.json 对不上？"

step "前端类型检查（tsc）"
"${NPM_ENV[@]}" npm run typecheck || fail "typecheck"

step "前端测试（vitest）"
"${NPM_ENV[@]}" npm test || fail "前端测试"

step "前端生产构建"
# 单测挡不住构建：有一次少一个类型导入，48 个单测全绿、构建是挂的。
"${NPM_ENV[@]}" npm run build || fail "前端构建"

if [ "$WITH_IMAGES" = "1" ]; then
    # 再拷一份：上面 npm 的产物不能进构建上下文，CI 是从干净的 checkout 构建的
    copy_git_files "$COPY/images"
    step "镜像构建：frontend"
    docker build -t ai-helpdesk-frontend:ci "$COPY/images/frontend" || fail "前端镜像"
    step "镜像构建：backend"
    docker build -t ai-helpdesk:ci --target production "$COPY/images/backend" || fail "后端镜像"
else
    printf '\n\033[1;33m跳过两个镜像构建（加 --with-images 打开，需要 Docker 引擎在跑）\033[0m\n'
fi

printf '\n\033[1;32m✓ 门禁全部通过\033[0m\n'
