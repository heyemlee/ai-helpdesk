"""
并发压测：验证契约 §8.4 那个「单副本约 2.3 QPS」的估算。

那个数字是算出来的不是测出来的：`/query` 是同步 def，跑在 anyio 默认 40 线程的
线程池里，一次查库 17.7s 就占住一个线程 17.7s → 40/17.7 ≈ 2.3 QPS。算得对不对，
以及真压上去会不会有别的东西先崩（连接、内存、模型侧限流），只有压了才知道。

**这个脚本会真的烧模型配额**：并发数 × 轮数 次真实调用。默认参数下约 12 次。

关注三件事，任何一件出问题都比吞吐数字本身重要：
  1. 有没有请求失败（并发下才会暴露的竞态、连接池耗尽）
  2. 延迟随并发怎么变（线性劣化 = 排队；突然爆炸 = 有东西满了）
  3. 结果对不对（并发下串号的话，A 的答案会出现在 B 的响应里）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

RUN = uuid.uuid4().hex[:8]

# 每个请求问一个**不同**的工程号，这样能顺带检出并发串号：
# 如果 A 的响应里出现了 B 问的那个工程号，就是上下文串了。
JOBS = ["J25-00003", "J25-00006", "J25-00008", "J26-00001",
        "J25-00012", "J26-00004", "J25-00015", "J25-00019"]


def one(port: int, idx: int) -> dict:
    job = JOBS[idx % len(JOBS)]
    body = json.dumps({"user_id": f"load-{idx}", "session_id": f"l-{RUN}-{idx}",
                       "query": f"{job} 的合同额是多少？"}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/query", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            d = json.loads(r.read())
        dt = time.time() - t0
        sqls = " ".join((s.get("final_sql") or "") for s in d.get("step_logs") or [])
        # 串号检查：本次的 SQL 里只应出现自己问的那个工程号
        others = [j for j in JOBS if j != job and j in sqls]
        return {"i": idx, "job": job, "ok": True, "s": round(dt, 1),
                "queried": d.get("queried_database"), "crosstalk": others}
    except Exception as ex:
        return {"i": idx, "job": job, "ok": False, "s": round(time.time() - t0, 1),
                "error": f"{type(ex).__name__}: {ex}"}


def wave(port: int, n: int, offset: int) -> list[dict]:
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(lambda i: one(port, offset + i), range(n)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--levels", default="1,2,4", help="并发级别，逗号分隔")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    report = []
    offset = 0
    for n in [int(x) for x in a.levels.split(",")]:
        t0 = time.time()
        rows = wave(a.port, n, offset)
        wall = time.time() - t0
        offset += n
        okays = [r for r in rows if r["ok"]]
        lat = [r["s"] for r in okays]
        cross = [r for r in okays if r.get("crosstalk")]
        stat = {
            "并发": n, "成功": f"{len(okays)}/{n}",
            "墙钟s": round(wall, 1),
            "吞吐QPS": round(len(okays) / wall, 2) if wall else 0,
            "延迟p50": round(statistics.median(lat), 1) if lat else None,
            "延迟max": round(max(lat), 1) if lat else None,
            "串号": len(cross),
            "失败": [r.get("error") for r in rows if not r["ok"]],
        }
        report.append(stat)
        print(f"并发 {n:>2}: 成功 {stat['成功']}  墙钟 {stat['墙钟s']}s  "
              f"吞吐 {stat['吞吐QPS']} QPS  p50 {stat['延迟p50']}s  max {stat['延迟max']}s  "
              f"串号 {stat['串号']}", flush=True)
        for e in stat["失败"]:
            print(f"        失败: {e}", flush=True)

    if a.out:
        from pathlib import Path
        Path(a.out).write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
