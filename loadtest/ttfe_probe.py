"""
首句多久出来：同一套后端，inline（一轮跑在请求里）和排队模式（提交成 run、worker 跑、帧流转回来）
各问 N 次，量客户端看到第一句回答（第一个 delta 帧）和整轮结束各用了多久。

排队模式多出来的是"写库 + 派发 + worker 领走 + 帧经 Redis 转一手"那一截。负载很低时测，
量的就是这一截本身，不混排队。两边交替着发，抵掉假模型和机器的漂移。

    docker compose -f deploy/drill/docker-compose.yml --profile compare up -d api-inline
    backend/.venv/bin/python loadtest/ttfe_probe.py --n 30
"""
import argparse
import asyncio
import json
import math
import pathlib
import statistics
import time
import uuid

import httpx


async def one(client: httpx.AsyncClient, url: str, i: int, tag: str) -> tuple[float, float]:
    body = {"message": f"柜门关不严怎么办，柜号 W{7000 + i}-{i % 7 + 1}", "user_id": f"probe-{tag}",
            "conv_id": f"probe-{tag}-{uuid.uuid4().hex[:8]}"}
    started = time.perf_counter()
    first = total = None
    async with client.stream("POST", f"{url}/chat/stream", json=body, timeout=120) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if line == "event: delta" and first is None:
                first = time.perf_counter() - started
            elif line in ("event: result", "event: error"):
                total = time.perf_counter() - started
                break
    return first or float("nan"), total or float("nan")


def stats(values: list[float]) -> dict[str, float]:
    ordered = sorted(v for v in values if not math.isnan(v))
    if not ordered:
        return {}
    return {"p50": statistics.median(ordered), "p95": ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))],
            "mean": statistics.fmean(ordered)}


async def main() -> None:
    parser = argparse.ArgumentParser(description="inline 和排队模式的首句延迟")
    parser.add_argument("--inline", default="http://127.0.0.1:28081")
    parser.add_argument("--queued", default="http://127.0.0.1:28080")
    parser.add_argument("--n", type=int, default=30)
    parser.add_argument("--json", default="")
    args = parser.parse_args()
    results: dict[str, dict[str, list[float]]] = {"inline": {"first": [], "total": []},
                                                  "queued": {"first": [], "total": []}}
    async with httpx.AsyncClient() as client:
        for mode, url in (("inline", args.inline), ("queued", args.queued)):   # 热身：两边各一次不计
            await one(client, url, 0, mode)
        for i in range(1, args.n + 1):
            for mode, url in (("inline", args.inline), ("queued", args.queued)):
                first, total = await one(client, url, i, mode)
                results[mode]["first"].append(first)
                results[mode]["total"].append(total)
    summary = {mode: {k: stats(v) for k, v in data.items()} for mode, data in results.items()}
    for kind in ("first", "total"):
        inline, queued = summary["inline"][kind], summary["queued"][kind]
        print(f"{kind:>5}: inline p50 {inline['p50']:.3f}s p95 {inline['p95']:.3f}s | "
              f"queued p50 {queued['p50']:.3f}s p95 {queued['p95']:.3f}s | "
              f"difference p50 {(queued['p50'] - inline['p50']) * 1000:+.0f} ms")
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps({"n": args.n, "summary": summary, "raw": results}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
