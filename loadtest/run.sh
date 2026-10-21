#!/usr/bin/env bash
# 压测的一档：把 deploy/drill 那一套按参数配好，跑 k6（loadtest/runs.js），收结果。
#
#   WORKERS=4 LLM_RPM=100 DEGRADE=off MOCK_RPM=120 loadtest/run.sh four-workers
#
# | 变量 | 默认 | 说明 |
# |---|---|---|
# | WORKERS | 1 | worker 容器数（每个同时跑 4 个 run） |
# | LLM_RPM | 0 | 令牌桶（core/admission）的 RPM 上限，0 不限 |
# | DEGRADE | off | 降级的积压阈值（四个数），off 不降级 |
# | MOCK_RPM | 120 | 假模型的配额：每分钟超过这么多就回 429 |
# | MOCK_LATENCY_MS | 800 | 假模型作答（流式）第一个字之前等多久 |
# | MOCK_STRUCTURED_MS | 400 | 假模型结构化输出（意图、改写、重排）一次多久 |
# | QUEUE_MAX | 200 | 积压到多少开始拒收（429） |
# | PROFILE | 15,30,60,60 | 每一级的到达率（每分钟多少个 run） |
# | STAGE | 1m | 每一级多长 |
#
# 结果在 loadtest/results/<名字>/：summary.txt（k6 的摘要）、mock.json（假模型收到的请求按状态码
# 分）、backlog.log（每 5 秒一行积压）、config.json（这一档的参数）。
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
NAME=${1:?用法: loadtest/run.sh <名字>}
OUT=${OUT_DIR:-$ROOT/loadtest/results}/$NAME
COMPOSE=(docker compose -f "$ROOT/deploy/drill/docker-compose.yml")
WORKERS=${WORKERS:-1}
LLM_RPM=${LLM_RPM:-0}
DEGRADE=${DEGRADE:-off}
MOCK_RPM=${MOCK_RPM:-120}
MOCK_LATENCY_MS=${MOCK_LATENCY_MS:-800}
MOCK_STRUCTURED_MS=${MOCK_STRUCTURED_MS:-400}
QUEUE_MAX=${QUEUE_MAX:-200}
PROFILE=${PROFILE:-15,30,60,60}
STAGE=${STAGE:-1m}
API=http://127.0.0.1:${DRILL_API_PORT:-28080}
MOCK=http://127.0.0.1:${DRILL_MOCK_PORT:-29000}
mkdir -p "$OUT"

backlog() { curl -s "$API/health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["runs"].get("backlog", 0))'; }

DRILL_WORKERS=$WORKERS DRILL_LLM_RPM=$LLM_RPM DRILL_DEGRADE_BACKLOG=$DEGRADE DRILL_QUEUE_MAX=$QUEUE_MAX \
  "${COMPOSE[@]}" up -d --wait >/dev/null
# 上一档没跑完的 run 先等它们排空，不然这一档一开始就背着积压
until [ "$(backlog)" = "0" ]; do sleep 2; done
curl -s -X POST "$MOCK/admin/reset" >/dev/null
curl -s -X POST "$MOCK/admin/config" \
  -d "{\"rpm\": $MOCK_RPM, \"latency_ms\": $MOCK_LATENCY_MS, \"structured_latency_ms\": $MOCK_STRUCTURED_MS}" >/dev/null
printf '{"workers": %s, "llm_rpm": %s, "degrade": "%s", "mock_rpm": %s, "mock_latency_ms": %s, "mock_structured_ms": %s, "queue_max": %s, "profile": "%s", "stage": "%s"}\n' \
  "$WORKERS" "$LLM_RPM" "$DEGRADE" "$MOCK_RPM" "$MOCK_LATENCY_MS" "$MOCK_STRUCTURED_MS" "$QUEUE_MAX" "$PROFILE" "$STAGE" > "$OUT/config.json"
STARTED=$(date +%s)

( while true; do echo "$(date +%s) $(backlog)"; sleep 5; done ) > "$OUT/backlog.log" &
SAMPLER=$!
trap 'kill $SAMPLER 2>/dev/null || true' EXIT

docker run --rm --network aihd-drill_default \
  -v "$ROOT/loadtest:/scripts:ro" -v "$OUT:/results" \
  -e API=http://api:8000 -e PROFILE="$PROFILE" -e STAGE="$STAGE" \
  grafana/k6:2.3.0 run --quiet /scripts/runs.js | tee "$OUT/summary.txt"
curl -s "$MOCK/admin/stats" > "$OUT/mock.json"
# 每分钟跑完多少个（服务端的 finished_at，按分钟分桶）：k6 的总数里含着收尾那一截，看不出平台
docker exec aihd-drill-postgres-1 psql -U drill -d drill -At -F ' ' -c \
  "SELECT floor((finished_at - $STARTED) / 60)::int, count(*), count(*) FILTER (WHERE degrade_level > 0),
          count(*) FILTER (WHERE (result->>'service_unavailable')::boolean)
     FROM agent_runs WHERE created_at >= $STARTED AND finished_at IS NOT NULL GROUP BY 1 ORDER BY 1" \
  > "$OUT/per_minute.txt"
python3 - "$OUT" <<'PY'
import json, sys
out = sys.argv[1]
mock = json.load(open(f"{out}/mock.json"))
k6 = json.load(open(f"{out}/summary.json"))["metrics"]
finished = k6.get("runs_finished", {}).get("values", {}).get("count", 0)
ok = mock["statuses"].get("200", 0)
print(f"model calls: {mock['statuses']} → {ok / finished:.2f} successful calls per finished run" if finished else "no run finished")
print("per minute (minute, finished, degraded, unavailable):")
print(open(f"{out}/per_minute.txt").read().strip())
PY
