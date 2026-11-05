#!/usr/bin/env bash
# 打印各服务容器环境变量的指纹，部署前后各存一份再 diff，就知道这次改了哪些配置。
# 只输出变量名和值的哈希，不输出值：里面有密钥。要 Docker，沙箱外跑。
# 不存在的容器记一行"-"（比如第一次部署 SQL Agent 之前），不中断。
set -euo pipefail

# 连不上 Docker（比如在沙箱里跑）时每个容器都会被当成"不存在"，前后两份一样、diff 为空，
# 等于没核对就过了。先确认连得上
if ! docker version > /dev/null 2>&1; then
    echo "连不上 Docker，没法取环境变量指纹（要在沙箱外跑）" >&2
    exit 1
fi

for container in ai-helpdesk-app ai-helpdesk-customer ai-helpdesk-frontend ai-helpdesk-sql-agent; do
    if ! docker inspect "$container" > /dev/null 2>&1; then
        echo "$container -"
        continue
    fi
    docker inspect --format '{{json .Config.Env}}' "$container" | python3 -c '
import hashlib, json, sys

for item in sorted(json.load(sys.stdin)):
    name, _, value = item.partition("=")
    print(sys.argv[1], name, hashlib.sha256(value.encode()).hexdigest()[:12])
' "$container"
done
