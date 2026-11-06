#!/usr/bin/env bash
# Real Chromium + API/PG/Redis/objects in an env-empty, disposable source copy.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAIN="$(git -C "$ROOT" worktree list --porcelain | sed -n '1s/^worktree //p')"
PY="$MAIN/backend/.venv/bin/python"
COPY="$(mktemp -d "${TMPDIR:-/tmp}/aihd-browser.XXXXXX")"
SERVICES="aihd-browser-$$"
API_PID=""; FRONT_PID=""
cleanup() {
  [ -z "$API_PID" ] || kill "$API_PID" 2>/dev/null || true
  [ -z "$FRONT_PID" ] || kill "$FRONT_PID" 2>/dev/null || true
  [ -z "$API_PID" ] || wait "$API_PID" 2>/dev/null || true
  [ -z "$FRONT_PID" ] || wait "$FRONT_PID" 2>/dev/null || true
  docker rm -f "$SERVICES-pg" "$SERVICES-redis" >/dev/null 2>&1 || true
  # Keep only test outputs on failure; source copy never contains credentials.
  if [ "${KEEP_BROWSER_COPY:-0}" != 1 ]; then rm -rf "$COPY"; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
git -C "$ROOT" ls-files -co --exclude-standard -z -- backend frontend a2a-contract |
  tar -C "$ROOT" --null -T - -cf - | tar -xf - -C "$COPY"
port() { "$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
PG_PORT="$(port)"; REDIS_PORT="$(port)"; API_PORT="$(port)"; FRONT_PORT="$(port)"
docker run -d --rm --name "$SERVICES-pg" -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=test \
  -p "127.0.0.1:$PG_PORT:5432" --tmpfs /var/lib/postgresql/data pgvector/pgvector:pg16 >/dev/null
docker run -d --rm --name "$SERVICES-redis" -p "127.0.0.1:$REDIS_PORT:6379" redis:7-alpine >/dev/null
for _ in $(seq 1 60); do
  if docker exec "$SERVICES-pg" pg_isready -h 127.0.0.1 -U test -d test >/dev/null 2>&1; then break; fi
  sleep 1
done
BASE_ENV=(env -i "HOME=$HOME" "PATH=$PATH" "TMPDIR=${TMPDIR:-/tmp}" LANG=en_US.UTF-8)
cd "$COPY/backend"
"${BASE_ENV[@]}" ANTHROPIC_API_KEY=test-key ATTACHMENT_BROWSER_FIXTURE=1 \
  "TEST_POSTGRES_DSN=postgresql://test:test@127.0.0.1:$PG_PORT/test" "TEST_REDIS_URL=redis://127.0.0.1:$REDIS_PORT/0" \
  "BROWSER_API_URL=http://127.0.0.1:$API_PORT" AI_HELPDESK_ATTACHMENT_STORAGE=local "AI_HELPDESK_ATTACHMENT_DIR=$COPY/objects" \
  "$PY" -m uvicorn browser_attachments_server:app --app-dir tests --host 127.0.0.1 --port "$API_PORT" > "$COPY/api.log" 2>&1 &
API_PID=$!
cd "$COPY/frontend"
"${BASE_ENV[@]}" npm_config_cache="${npm_config_cache:-/tmp/aihd-browser-npm}" npm ci --no-audit --no-fund
mkdir -p e2e/fixtures
"$PY" - <<'PY'
from PIL import Image
for name, color in [('clear', 'white'), ('conflict', 'red')]:
    Image.new('RGB', (160, 120), color).save('e2e/fixtures/' + name + '.png')
PY
"${BASE_ENV[@]}" "VITE_API_URL=http://127.0.0.1:$API_PORT" ./node_modules/.bin/vite --host 127.0.0.1 --port "$FRONT_PORT" > "$COPY/frontend.log" 2>&1 &
FRONT_PID=$!
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$FRONT_PORT" >/dev/null 2>&1 && curl -fsS "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$API_PID" 2>/dev/null; then cat "$COPY/api.log"; exit 1; fi
  sleep 1
done
if ! "${BASE_ENV[@]}" "BROWSER_FRONTEND_URL=http://127.0.0.1:$FRONT_PORT" "BROWSER_OUTPUT=$COPY/results" \
  ./node_modules/.bin/playwright test -c e2e/playwright.config.ts; then
  cat "$COPY/api.log"
  printf 'Browser diagnostics: %s\n' "$COPY"
  KEEP_BROWSER_COPY=1
  exit 1
fi
