#!/usr/bin/env bash
# 只读核对生产，不改任何东西，随时可以跑。任一项不对就退出 1。要 Docker 和本机端口，沙箱外跑。
#
# 最要紧的是第一项：:80 必须转到客户实例。2026-10-18 重建后端时两个容器的 IP 对调，
# 80 有约 98 秒转到了员工实例（没有 API key、能查订单）。那段时间 /health 照样返回 ok，
# 只有比对实例名才看得出来。
#
# SQL Agent 不发布宿主机端口，它的探针在它自己的容器里跑。"客户实例连不到它"用一个挂在客户实例
# 网络上的一次性容器来试（用客户实例的镜像，跑完就删），不进客户实例的容器。
#
# 这里核对的是**直连** SQL Agent 这一条。经员工实例间接委派那条路还开着：员工实例没开 Key，又和
# 客户实例同在 ai-helpdesk-network（Docker Desktop 上还有 host.docker.internal:8080 和 :5174）。
# 合仓设计 §3.3 写明了这次不堵，要堵就给员工实例设 AI_HELPDESK_API_KEY 和 AI_HELPDESK_PROTECT_CHAT。
# 下面会打一行 note 说它还开着，不算失败。
set -uo pipefail

EDGE="${EDGE:-http://127.0.0.1:80}"
STAFF="${STAFF:-http://127.0.0.1:8080}"
CUSTOMER="${CUSTOMER:-http://127.0.0.1:8081}"
CONSOLE="${CONSOLE:-http://127.0.0.1:5174}"
# compose 项目名就是仓库目录名 ai-helpdesk，网络名是 <项目>_<网络>
CUSTOMER_NET="${CUSTOMER_NET:-ai-helpdesk_ai-helpdesk-network}"
SQL_NET="${SQL_NET:-ai-helpdesk_sql-net}"
# 这个脚本在主检出的 .claude/skills/deploy/scripts/ 下；控制台的 Key 文件在主检出的 deploy/secrets/console/
ROOT="${ROOT:-$(cd "$(dirname "$0")/../../../.." && pwd)}"
CONSOLE_KEY_FILE="${CONSOLE_KEY_FILE:-$ROOT/deploy/secrets/console/sql_agent_key}"

FAILED=0
ok() { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; FAILED=1; }

IS_CUSTOMER='.status == "ok" and .event_log.instance == "customer" and .event_log.mode == "full"
    and .event_log.read == false and .event_log.dropped == 0 and .event_log.retention.body_days == 14
    and .tool_loop.enabled == false and .memory.cross_session == false
    and .capabilities.business_data == false'
IS_STAFF='.status == "ok" and .event_log.instance == "staff" and .event_log.mode == "full"
    and .event_log.read == true and .event_log.dropped == 0
    and .tool_loop.enabled == true and .memory.cross_session == true
    and .capabilities.business_data == true'
SUMMARY='{status, instance: .event_log.instance, mode: .event_log.mode, read: .event_log.read,
    dropped: .event_log.dropped, body_days: .event_log.retention.body_days,
    tool_loop: .tool_loop.enabled, cross_session: .memory.cross_session,
    business_data: .capabilities.business_data}'

check_health() {
    local name="$1" url="$2" want="$3" body summary
    body="$(curl -s -m 5 "$url/health")"
    if printf '%s' "$body" | jq -e "$want" >/dev/null 2>&1; then
        ok "$name"
    else
        summary="$(printf '%s' "$body" | jq -c "$SUMMARY" 2>/dev/null)"
        bad "$name: ${summary:-没有返回 JSON}"
    fi
}

check_code() {
    local url="$1" want="$2" code
    code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$url")"
    if [ "$code" = "$want" ]; then
        ok "$url → $code"
    else
        bad "$url → $code，应该是 $want"
    fi
}

# 光看 404 不够：/turns/x 这样的路径漏到后端也回 404（查无此轮）。
# nginx 自己回的是 HTML，后端回的是 JSON，要看是谁回的
check_blocked() {
    local url="$1" result
    result="$(curl -s -m 5 -o /dev/null -w '%{http_code} %{content_type}' "$url")"
    case "$result" in
        "404 text/html"*) ok "$url 被 nginx 挡住" ;;
        *) bad "$url → $result，应该是 nginx 自己回的 404" ;;
    esac
}

# 在 SQL Agent 自己的容器里请求它自己，打印"状态码 响应体"
sql_agent_get() {
    docker exec ai-helpdesk-sql-agent python -c '
import sys, urllib.error, urllib.request
try:
    response = urllib.request.urlopen("http://127.0.0.1:8000" + sys.argv[1], timeout=5)
    print(response.status, response.read().decode())
except urllib.error.HTTPError as error:
    print(error.code, error.read().decode())
' "$1" 2>/dev/null
}

# 容器挂在哪些网络上（空格分隔、排好序）
networks_of() {
    docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}' "$1" 2>/dev/null |
        tr ' ' '\n' | sed '/^$/d' | sort | tr '\n' ' ' | sed 's/ $//'
}

echo "入口 :80（客户唯一的入口）"
check_health ":80 转到客户实例，配置对得上" "$EDGE" "$IS_CUSTOMER"
# nginx 白名单只放行 /chat、/chat/stream、/runs/<id> 和 /health。GET /chat 回 405 说明路由通到了后端
check_code "$EDGE/chat" 405
for route in / /docs /tickets /knowledge/stats /turns/x /conversations/x; do
    check_blocked "$EDGE$route"
done

echo "实例"
check_health "员工实例配置对得上" "$STAFF" "$IS_STAFF"
check_health "客户实例配置对得上" "$CUSTOMER" "$IS_CUSTOMER"

echo "控制台"
check_code "$CONSOLE/" 200
check_health "控制台的 /api 通到员工实例" "$CONSOLE/api" '.status == "ok" and .event_log.instance == "staff"'
# 数据页面：/sql-api/ 由控制台的 nginx 带上 console（admin）那把 Key 转给 SQL Agent。
# whoami 不查库、不调模型，认出是 console 且是管理员，就说明 Key 在服务端加上了。
# 请求要带控制台自己的头（X-Console-Request: 1），和页面一样；不带的、标明跨站的、Host 不是本机的都该被拒
CONSOLE_HEADER='X-Console-Request: 1'
whoami="$(curl -s -m 5 -H "$CONSOLE_HEADER" "$CONSOLE/sql-api/whoami")"
if printf '%s' "$whoami" | jq -e '.identity.caller == "console" and .identity.is_admin == true' >/dev/null 2>&1; then
    ok "控制台的 /sql-api 带着管理员 Key 通到 SQL Agent"
else
    bad "控制台的 /sql-api/whoami → $(printf '%s' "$whoami" | head -c 160)"
fi
# 跨站请求（CSRF）和 DNS 重绑定：这几种在 Key 生效之前就该被 nginx 拒掉
check_code "$CONSOLE/sql-api/whoami" 403
code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' -H "$CONSOLE_HEADER" -H 'Sec-Fetch-Site: cross-site' "$CONSOLE/sql-api/whoami")"
if [ "$code" = 403 ]; then ok "标明跨站的 /sql-api 请求 → 403"; else bad "标明跨站的 /sql-api 请求 → $code，应该是 403"; fi
code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' -H "$CONSOLE_HEADER" -H 'Host: rebind.example' "$CONSOLE/sql-api/whoami")"
if [ "$code" = 421 ]; then ok "Host 不是本机的请求 → 421"; else bad "Host 不是本机的请求 → $code，应该是 421"; fi
# Key 只在控制台的容器里：页面、JS 包和响应头里都不能有它。拿 Key 文件当 grep 的模式（-f），不进命令行、不打印
if [ -s "$CONSOLE_KEY_FILE" ]; then
    # 模式里不能有空行：空模式匹配一切，会报"泄露"
    key_pattern() { grep -v '^[[:space:]]*$' "$CONSOLE_KEY_FILE"; }
    leaked=0
    page="$(curl -s -m 5 "$CONSOLE/")"
    for asset in / $(printf '%s' "$page" | grep -oE '/assets/[A-Za-z0-9._-]+\.(js|css)' | sort -u); do
        if curl -s -m 10 "$CONSOLE$asset" | grep -qF -f <(key_pattern); then leaked=1; fi
    done
    if curl -s -m 5 -D - -o /dev/null -H "$CONSOLE_HEADER" "$CONSOLE/sql-api/whoami" | grep -qF -f <(key_pattern); then leaked=1; fi
    if [ "$leaked" = 0 ]; then
        ok "页面、JS 包和 /sql-api 的响应头里都没有控制台的 Key"
    else
        bad "控制台的 Key 出现在了浏览器拿得到的内容里"
    fi
else
    bad "缺 $CONSOLE_KEY_FILE（控制台带给 SQL Agent 的 Key）"
fi

echo "SQL Agent"
readyz="$(sql_agent_get /readyz)"
if [ "${readyz%% *}" = 200 ] && printf '%s' "${readyz#* }" | jq -e '.ready == true and .checks.auth_mode == "api_key"' >/dev/null 2>&1; then
    ok "/readyz 就绪，鉴权是 api_key"
else
    bad "/readyz → ${readyz:-没有响应}"
fi
history="$(sql_agent_get /history)"
if [ "${history%% *}" = 401 ]; then
    ok "不带 Key 的 /history → 401"
else
    bad "不带 Key 的 /history → ${history%% *}，应该是 401"
fi

echo "网络隔离"
published="$(docker inspect --format '{{json .HostConfig.PortBindings}}' ai-helpdesk-sql-agent 2>/dev/null)"
case "$published" in
    "{}"|"null") ok "SQL Agent 没有发布宿主机端口" ;;
    *) bad "SQL Agent 发布了宿主机端口：${published:-容器不存在}" ;;
esac
want_networks() {
    local container="$1" want="$2" got
    got="$(networks_of "$container")"
    if [ "$got" = "$want" ]; then
        ok "$container 只在 $want"
    else
        bad "$container 在 ${got:-（容器不存在）}，应该只在 $want"
    fi
}
want_networks ai-helpdesk-sql-agent "$SQL_NET"
want_networks ai-helpdesk-frontend "$SQL_NET"
for container in ai-helpdesk-customer ai-helpdesk-nginx ai-helpdesk-postgres; do
    want_networks "$container" "$CUSTOMER_NET"
done
# 对照：同一个探针站在 sql-net 上要连得通，不然下面"连不到"什么也说明不了
code="$(docker run --rm --network "$SQL_NET" --entrypoint curl ai-helpdesk-ai-helpdesk-customer:latest \
    -s -m 3 -o /dev/null -w '%{http_code}' http://sql-agent:8000/health 2>/dev/null)"
if [ "$code" = 200 ]; then
    ok "对照：sql-net 上连得到 sql-agent:8000"
else
    bad "对照：sql-net 上 sql-agent:8000/health → ${code}，应该是 200"
fi
# 站在客户实例的网络上连：服务名应当解析不到，宿主机的 8000 上也应当什么都没有
# （发布过端口、或者旧进程还在跑，都会从 host.docker.internal 连得上）
for url in http://sql-agent:8000/health http://host.docker.internal:8000/health; do
    code="$(docker run --rm --network "$CUSTOMER_NET" --entrypoint curl ai-helpdesk-ai-helpdesk-customer:latest \
        -s -m 3 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null)"
    if [ "$code" = 000 ]; then
        ok "客户实例的网络上连不到 $url"
    else
        bad "客户实例的网络上 $url → $code"
    fi
done
# 已知还开着的间接路径（见开头），只提示
code="$(docker run --rm --network "$CUSTOMER_NET" --entrypoint curl ai-helpdesk-ai-helpdesk-customer:latest \
    -s -m 3 -o /dev/null -w '%{http_code}' http://ai-helpdesk:8000/health 2>/dev/null)"
if [ "$code" != 000 ]; then
    printf '  note  客户实例的网络上连得到员工实例（ai-helpdesk:8000 → %s）：经它间接委派这条路这次不堵\n' "$code"
fi

echo "容器"
for container in ai-helpdesk-app ai-helpdesk-customer ai-helpdesk-frontend ai-helpdesk-nginx ai-helpdesk-sql-agent; do
    health="$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null)"
    image="$(docker inspect --format '{{slice .Image 7 19}}' "$container" 2>/dev/null)"
    release="$(docker inspect --format '{{json .Config.Env}}' "$container" 2>/dev/null |
        jq -r '[.[] | select(startswith("AI_HELPDESK_RELEASE=") or startswith("AI_SQL_RELEASE="))
            | sub("^[A-Z_]+=" ; "")] | first // "-"')"
    if [ "$health" = healthy ]; then
        ok "$container  镜像 $image  版本 $release"
    else
        bad "$container  状态 ${health:-不存在}"
    fi
done

if [ "$FAILED" = 1 ]; then
    printf '\n有项目不对\n'
    exit 1
fi
printf '\n全部通过\n'
