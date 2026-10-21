#!/usr/bin/env bash
# 在 kind 集群上跑故障演练（scripts/k8s-up.sh 起的那一套）：worker 用 kubectl delete --force
# 杀掉（不给宽限期，等同于节点掉电），Redis 的 pod 删掉重建；同时每 5 秒记一行 KEDA 管着的
# worker 副本数，看积压上来时它怎么扩、退下去时怎么缩。
#
#   scripts/k8s-drill.sh --runs 80 --rate 3 --kills 3 --restart-redis
#
# 参数原样交给 backend/scripts/chaos_drill.py。副本数的记录写到 $DRILL_OUT（默认临时目录）。
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
NS=${NS:-aihd}
OUT=${DRILL_OUT:-$(mktemp -d)}
PIDS=()

# port-forward 是钉在一个 pod 上的：演练里 Redis 的 pod 被删掉重建，那一路转发就断了。
# 所以每一路包一个循环，断了一秒之后重连
# （子 shell 继承了 set -e：kubectl 断开时退出码非零，不加 || true 整个循环就跟着退了）
forward() {
  ( while true; do kubectl -n "$NS" port-forward "svc/$1" "$2" >/dev/null 2>&1 || true; sleep 1; done ) &
  PIDS+=($!)
}
cleanup() {
  for pid in "${PIDS[@]}"; do
    pkill -P "$pid" 2>/dev/null || true
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT

forward api-staff 38080:8000
forward postgres 35432:5432
forward redis 36379:6379
forward mock-llm 39000:9000
for _ in $(seq 30); do curl -sf http://127.0.0.1:38080/readyz >/dev/null && break; sleep 1; done

( while true; do
    echo "$(date +%s) $(kubectl -n "$NS" get deploy worker-staff -o jsonpath='{.spec.replicas} {.status.readyReplicas}')"
    sleep 5
  done ) > "$OUT/replicas.log" &
PIDS+=($!)

"$ROOT/backend/.venv/bin/python" "$ROOT/backend/scripts/chaos_drill.py" --driver k8s --namespace "$NS" \
  --api http://127.0.0.1:38080 --mock http://127.0.0.1:39000 \
  --pg postgresql://aihd:aihd@127.0.0.1:35432/aihd --redis redis://127.0.0.1:36379/0 "$@"
status=$?
echo "worker 副本数（时间戳 期望 就绪）：$OUT/replicas.log"
exit $status
