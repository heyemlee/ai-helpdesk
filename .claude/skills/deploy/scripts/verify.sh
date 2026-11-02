#!/usr/bin/env bash
# 只读核对生产，不改任何东西，随时可以跑。任一项不对就退出 1。要 Docker 和本机端口，沙箱外跑。
#
# 最要紧的是第一项：:80 必须转到客户实例。2026-10-18 重建后端时两个容器的 IP 对调，
# 80 有约 98 秒转到了员工实例（没有 API key、能查订单）。那段时间 /health 照样返回 ok，
# 只有比对实例名才看得出来。
set -uo pipefail

EDGE="${EDGE:-http://127.0.0.1:80}"
STAFF="${STAFF:-http://127.0.0.1:8080}"
CUSTOMER="${CUSTOMER:-http://127.0.0.1:8081}"
CONSOLE="${CONSOLE:-http://127.0.0.1:5174}"

FAILED=0
ok() { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; FAILED=1; }

IS_CUSTOMER='.status == "ok" and .event_log.instance == "customer" and .event_log.mode == "full"
    and .event_log.read == false and .event_log.dropped == 0 and .event_log.retention.body_days == 14
    and .tool_loop.enabled == false and .memory.cross_session == false'
IS_STAFF='.status == "ok" and .event_log.instance == "staff" and .event_log.mode == "full"
    and .event_log.read == true and .event_log.dropped == 0
    and .tool_loop.enabled == true and .memory.cross_session == true'
SUMMARY='{status, instance: .event_log.instance, mode: .event_log.mode, read: .event_log.read,
    dropped: .event_log.dropped, body_days: .event_log.retention.body_days,
    tool_loop: .tool_loop.enabled, cross_session: .memory.cross_session}'

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

echo "容器"
for container in ai-helpdesk-app ai-helpdesk-customer ai-helpdesk-frontend ai-helpdesk-nginx; do
    health="$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null)"
    image="$(docker inspect --format '{{slice .Image 7 19}}' "$container" 2>/dev/null)"
    release="$(docker inspect --format '{{json .Config.Env}}' "$container" 2>/dev/null |
        jq -r '[.[] | select(startswith("AI_HELPDESK_RELEASE=")) | ltrimstr("AI_HELPDESK_RELEASE=")] | first // "-"')"
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
