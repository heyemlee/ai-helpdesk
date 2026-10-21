#!/usr/bin/env bash
# 删掉 scripts/k8s-up.sh 建的 kind 集群（连同里面的数据）。
set -euo pipefail
kind delete cluster --name "${CLUSTER:-aihd}"
