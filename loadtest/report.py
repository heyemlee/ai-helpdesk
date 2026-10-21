"""
把 loadtest/run.sh 跑出来的几档结果汇成一张表（Markdown），写评测记录用。

    python3 loadtest/report.py loadtest/results/a1-one-worker loadtest/results/a2-two-workers ...

每一档报：配置、提交/拒收/跑完、平台吞吐（每分钟跑完多少个，取最高的两分钟的平均——到达率
最高的那两级）、排队和总耗时的分位数、按"模型服务不可用"答的、降级的、每个跑完的 run 花了
几次成功的模型调用、假模型回了多少 429。
"""
import json
import pathlib
import sys


def load(path: pathlib.Path) -> dict:
    config = json.loads((path / "config.json").read_text())
    k6 = json.loads((path / "summary.json").read_text())["metrics"]
    mock = json.loads((path / "mock.json").read_text())
    minutes = []
    for line in (path / "per_minute.txt").read_text().split("\n"):
        parts = line.split()
        if len(parts) == 4:
            minutes.append(tuple(int(p) for p in parts))

    def count(name):
        return int(k6.get(name, {}).get("values", {}).get("count", 0))

    def stat(name, key):
        return k6.get(name, {}).get("values", {}).get(key)

    finished = count("runs_finished")
    ok_calls = mock["statuses"].get("200", 0)
    # 平台：到达率最高的两级（第 3、4 分钟）跑完的个数的平均；收尾那几分钟不算
    plateau = [n for minute, n, _d, _u in minutes if minute in (3, 4)]
    return {
        "name": path.name,
        "config": config,
        "accepted": count("runs_accepted"),
        "rejected": count("runs_rejected_429"),
        "finished": finished,
        "unavailable": count("runs_answered_unavailable"),
        "retried": count("runs_retried"),
        "plateau": sum(plateau) / len(plateau) if plateau else None,
        "queue_p50": stat("run_queue_wait_s", "med"),
        "queue_p95": stat("run_queue_wait_s", "p(95)"),
        "total_p50": stat("run_total_s", "med"),
        "total_p95": stat("run_total_s", "p(95)"),
        "degraded": sum(d for _m, _n, d, _u in minutes),
        "calls_per_run": ok_calls / finished if finished else None,
        "upstream_429": mock["statuses"].get("429", 0),
        "per_minute": minutes,
    }


def fmt(value, digits=1):
    return "—" if value is None else f"{value:.{digits}f}"


def main() -> None:
    rows = [load(pathlib.Path(arg)) for arg in sys.argv[1:]]
    print("| Scenario | Workers | Token bucket | Degrade | Accepted / 429 | Finished/min at peak | "
          "Queue wait p50 / p95 (s) | Submit→done p50 / p95 (s) | Answered unavailable | Degraded | "
          "Model calls per run | Upstream 429s |")
    print("| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in rows:
        c = r["config"]
        print(f"| {r['name']} | {c['workers']} | {c['llm_rpm'] or 'off'} | {c['degrade']} | "
              f"{r['accepted']} / {r['rejected']} | {fmt(r['plateau'])} | "
              f"{fmt(r['queue_p50'])} / {fmt(r['queue_p95'])} | {fmt(r['total_p50'])} / {fmt(r['total_p95'])} | "
              f"{r['unavailable']} | {r['degraded']} | {fmt(r['calls_per_run'], 2)} | {r['upstream_429']} |")
    print()
    print("Finished per minute (minute: finished / degraded / answered unavailable):")
    print()
    for r in rows:
        series = ", ".join(f"{m}: {n}/{d}/{u}" for m, n, d, u in r["per_minute"])
        print(f"- {r['name']}: {series}")


if __name__ == "__main__":
    main()
