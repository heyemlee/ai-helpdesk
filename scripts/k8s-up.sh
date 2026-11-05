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
docker build -t aihd/sql-agent:dev -f "$ROOT/ai-sql-agent/deploy/Dockerfile" \
  --build-arg AI_SQL_RELEASE="$RELEASE" "$ROOT/ai-sql-agent"
# 自己的镜像本地构建、直接装进节点。pgvector 和 redis 由节点自己去 Docker Hub 拉：
# Docker Desktop 的 containerd 镜像库里它们是多平台镜像，kind load 会报 content digest not found
kind load docker-image aihd/backend:dev --name "$CLUSTER"
kind load docker-image aihd/sql-agent:dev --name "$CLUSTER"

kubectl apply --server-side -f \
  "https://github.com/kedacore/keda/releases/download/v${KEDA_VERSION}/keda-${KEDA_VERSION}.yaml" >/dev/null
kubectl -n keda rollout status deploy/keda-operator --timeout=180s
kubectl -n keda rollout status deploy/keda-metrics-apiserver --timeout=180s
kubectl -n keda rollout status deploy/keda-admission --timeout=180s

# 建 topic 的 Job 每次都重跑（幂等）：Job 建好之后不能改，先删掉旧的
kubectl -n aihd delete job kafka-topics --ignore-not-found >/dev/null
kubectl apply -k "$ROOT/deploy/k8s"
kubectl -n aihd rollout status statefulset/postgres --timeout=180s
kubectl -n aihd rollout status statefulset/redis --timeout=180s
kubectl -n aihd rollout status statefulset/kafka --timeout=300s
kubectl -n aihd wait --for=condition=complete job/kafka-topics --timeout=300s

# SQL Agent 的语义层和四个业务库不在 git 里：从本机的 deploy/sql-agent/data（semantic/、simple/，
# 和 docker-compose 用的是同一份）拷进 sql-agent-data 卷。没有这份数据时 SQL Agent 起不来
# （生产就绪检查），其余照常；生成方法见 ai-sql-agent/tools/simple_erp/README.md
DATA="$ROOT/deploy/sql-agent/data"
SQL_AGENT=sql-agent
if [ -d "$DATA/semantic" ] && [ -d "$DATA/simple" ]; then
  kubectl -n aihd delete pod sql-agent-data-loader --ignore-not-found >/dev/null
  kubectl -n aihd run sql-agent-data-loader --image=aihd/sql-agent:dev --restart=Never \
    --overrides='{"spec":{"securityContext":{"runAsUser":10001,"fsGroup":10001},"containers":[{"name":"loader","image":"aihd/sql-agent:dev","command":["sleep","600"],"volumeMounts":[{"name":"data","mountPath":"/data"}]}],"volumes":[{"name":"data","persistentVolumeClaim":{"claimName":"sql-agent-data"}}]}}' \
    >/dev/null
  kubectl -n aihd wait --for=condition=Ready pod/sql-agent-data-loader --timeout=120s
  kubectl -n aihd exec sql-agent-data-loader -- rm -rf /data/semantic /data/simple
  kubectl -n aihd cp "$DATA/semantic" sql-agent-data-loader:/data/semantic
  kubectl -n aihd cp "$DATA/simple" sql-agent-data-loader:/data/simple
  kubectl -n aihd delete pod sql-agent-data-loader --wait=false >/dev/null
else
  echo "没有 $DATA/semantic 和 simple：SQL Agent 起不来，员工实例的订单类问题退回知识库" >&2
  SQL_AGENT=
fi

# 镜像换了标签没变（都是 :dev）：重启一下才会用上刚装进去的那一个
DEPLOYS="mock-llm api-staff api-customer worker-staff worker-customer
         projector-staff projector-customer sampler-staff sampler-customer $SQL_AGENT"
for deploy in $DEPLOYS; do kubectl -n aihd rollout restart "deploy/$deploy" >/dev/null; done
for deploy in $DEPLOYS; do
  kubectl -n aihd rollout status "deploy/$deploy" --timeout=300s
done
kubectl -n aihd get pods,scaledobjects
echo
echo "API:   kubectl -n aihd port-forward svc/api-staff 38080:8000"
echo "演练:  scripts/k8s-drill.sh            # 加 --kafka 对账事件日志那一侧"
