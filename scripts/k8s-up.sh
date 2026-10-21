#!/usr/bin/env bash
# 在本地 kind 集群上起排队模式的一整套（deploy/k8s）：建集群、构建并装载镜像、装 KEDA、
# apply、等就绪。重复跑是安全的：集群已经在就复用，镜像重新构建、重新装载，滚动重启。
#
#   scripts/k8s-up.sh
#   kubectl -n aihd port-forward svc/api-staff 38080:8000     # API
#   scripts/k8s-down.sh                                         # 整个集群删掉
#
# 需要 docker、kind（brew install kind）、kubectl。KEDA 的版本钉在 KEDA_VERSION。
set -euo pipefail

CLUSTER=${CLUSTER:-aihd}
KEDA_VERSION=${KEDA_VERSION:-2.20.2}
ROOT=$(cd "$(dirname "$0")/.." && pwd)

for tool in docker kind kubectl; do
  command -v "$tool" >/dev/null || { echo "需要 $tool" >&2; exit 1; }
done

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  kind create cluster --name "$CLUSTER" --wait 120s
fi
kubectl config use-context "kind-$CLUSTER" >/dev/null

RELEASE=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo dev)
docker build -t aihd/backend:dev --target production --build-arg AI_HELPDESK_RELEASE="$RELEASE" "$ROOT/backend"
# 自己的镜像本地构建、直接装进节点。pgvector 和 redis 由节点自己去 Docker Hub 拉：
# Docker Desktop 的 containerd 镜像库里它们是多平台镜像，kind load 会报 content digest not found
kind load docker-image aihd/backend:dev --name "$CLUSTER"

kubectl apply --server-side -f \
  "https://github.com/kedacore/keda/releases/download/v${KEDA_VERSION}/keda-${KEDA_VERSION}.yaml" >/dev/null
kubectl -n keda rollout status deploy/keda-operator --timeout=180s
kubectl -n keda rollout status deploy/keda-metrics-apiserver --timeout=180s
kubectl -n keda rollout status deploy/keda-admission --timeout=180s

kubectl apply -k "$ROOT/deploy/k8s"
kubectl -n aihd rollout status statefulset/postgres --timeout=180s
kubectl -n aihd rollout status statefulset/redis --timeout=180s
# 镜像换了标签没变（都是 :dev）：重启一下才会用上刚装进去的那一个
kubectl -n aihd rollout restart deploy/mock-llm deploy/api-staff deploy/api-customer \
  deploy/worker-staff deploy/worker-customer >/dev/null
for deploy in mock-llm api-staff api-customer worker-staff worker-customer; do
  kubectl -n aihd rollout status "deploy/$deploy" --timeout=300s
done
kubectl -n aihd get pods,scaledobjects
echo
echo "API:   kubectl -n aihd port-forward svc/api-staff 38080:8000"
echo "演练:  scripts/k8s-drill.sh"
