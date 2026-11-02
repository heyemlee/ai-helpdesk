#!/usr/bin/env bash
# 打印三个容器环境变量的指纹，部署前后各存一份再 diff，就知道这次改了哪些配置。
# 只输出变量名和值的哈希，不输出值：里面有密钥。要 Docker，沙箱外跑。
set -euo pipefail

for container in ai-helpdesk-app ai-helpdesk-customer ai-helpdesk-frontend; do
    docker inspect --format '{{json .Config.Env}}' "$container" | python3 -c '
import hashlib, json, sys

for item in sorted(json.load(sys.stdin)):
    name, _, value = item.partition("=")
    print(sys.argv[1], name, hashlib.sha256(value.encode()).hexdigest()[:12])
' "$container"
done
