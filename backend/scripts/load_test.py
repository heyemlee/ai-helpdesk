"""
/chat 压测脚本：并发打请求，输出 RPS 和 P50/P95/P99 延迟。

用途：给"首响应秒级"的声明补实测数据。跑一次，把结果写进 README，
比"设计目标"硬气得多。

用法（服务需已启动，会产生真实 LLM 调用费用——先算一下 requests 的量）：

    python scripts/load_test.py --base-url http://localhost:8000 \\
        --concurrency 10 --requests 100

    # 只测规则通道（意图零 LLM，只有 Agent 作答一次 LLM 调用）：
    python scripts/load_test.py --fast-path-only --requests 50

    # 带鉴权与限流的环境：
    python scripts/load_test.py --api-key admin-key --user-token <token>

不依赖 locust：httpx + asyncio 一百多行就够，依赖已在 requirements 里。
"""
import argparse
import asyncio
import json
import statistics
import sys
import time

import httpx

# 规则通道消息：意图识别零网络调用，延迟主要来自 Agent 的一次 LLM 作答
FAST_PATH_MESSAGES = [
    "补件什么时候到",
    "这套报价多少",
    "订单状态怎么样了",
    "转人工客服",
    "柜门关不严",
    "尾款被重复扣款了",
]

# 完整链路消息：三路融合 + RAG 改写重排，LLM 往返最多
ENSEMBLE_MESSAGES = [
    "你们这个柜子我用着有点问题想咨询一下",
    "上周装好的橱柜现在有点情况需要处理",
    "麻烦看看我这单最近的情况",
    "之前反馈的事情后来怎么样了",
]


async def worker(
    client: httpx.AsyncClient,
    queue: asyncio.Queue,
    results: list,
    headers: dict,
) -> None:
    while True:
        try:
            idx, message = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        payload = {"message": message, "user_id": f"loadtest-{idx % 20}"}
        t0 = time.monotonic()
        try:
            resp = await client.post("/chat", json=payload, headers=headers)
            elapsed_ms = (time.monotonic() - t0) * 1000
            body = resp.json() if resp.status_code == 200 else {}
            results.append({
                "status": resp.status_code,
                "total_ms": elapsed_ms,
                "intent_ms": body.get("intent_latency_ms"),
                "strategy": body.get("intent_strategy"),
            })
        except Exception as ex:
            results.append({"status": 0, "total_ms": (time.monotonic() - t0) * 1000,
                            "error": str(ex)})
        finally:
            queue.task_done()


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1))))
    return ordered[k]


async def main() -> int:
    parser = argparse.ArgumentParser(description="ai-helpdesk /chat 压测")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--fast-path-only", action="store_true",
                        help="只发规则通道消息（LLM 调用最少，成本可控）")
    parser.add_argument("--api-key", default=None, help="X-API-Key（PROTECT_CHAT 开启时）")
    parser.add_argument("--user-token", default=None, help="X-User-Token（令牌鉴权开启时）")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    messages = FAST_PATH_MESSAGES if args.fast_path_only else FAST_PATH_MESSAGES + ENSEMBLE_MESSAGES
    headers = {}
    if args.api_key:
        headers["X-API-Key"] = args.api_key
    if args.user_token:
        headers["X-User-Token"] = args.user_token

    queue: asyncio.Queue = asyncio.Queue()
    for i in range(args.requests):
        queue.put_nowait((i, messages[i % len(messages)]))

    results: list[dict] = []
    print(f"压测 {args.base_url}/chat: {args.requests} 请求 × {args.concurrency} 并发 …")
    t0 = time.monotonic()
    async with httpx.AsyncClient(base_url=args.base_url, timeout=args.timeout) as client:
        await asyncio.gather(*[
            worker(client, queue, results, headers) for _ in range(args.concurrency)
        ])
    wall_s = time.monotonic() - t0

    ok = [r for r in results if r["status"] == 200]
    errors = [r for r in results if r["status"] != 200]
    latencies = [r["total_ms"] for r in ok]
    fast = [r for r in ok if r.get("strategy") == "fast_path"]
    intent_fast = [r["intent_ms"] for r in fast if r.get("intent_ms") is not None]

    report = {
        "requests": len(results),
        "success": len(ok),
        "errors": len(errors),
        "wall_time_s": round(wall_s, 2),
        "rps": round(len(ok) / wall_s, 2) if wall_s else 0,
        "latency_ms": {
            "p50": round(percentile(latencies, 50), 1),
            "p95": round(percentile(latencies, 95), 1),
            "p99": round(percentile(latencies, 99), 1),
            "mean": round(statistics.mean(latencies), 1) if latencies else 0,
            "max": round(max(latencies), 1) if latencies else 0,
        },
        "fast_path": {
            "count": len(fast),
            "share": round(len(fast) / len(ok), 3) if ok else 0,
            "intent_p99_ms": round(percentile(intent_fast, 99), 3) if intent_fast else None,
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if errors:
        sample = errors[0]
        print(f"\n首个错误样本: status={sample['status']} {sample.get('error', '')}",
              file=sys.stderr)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
