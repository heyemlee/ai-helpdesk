"""
跑 `benchmark.gate --repeat N`，逐轮存下每题结果；并且**只认远端向量**：

建 runner 时（Text2SQLPipeline 在构造里就建好 Schema 检索服务）检查实际在用的向量，
不是远端、或者启动时记了降级，就在调用任何对话模型之前退出，退出码 4。
门禁整个过程只建这一个 runner，留出集也复用它的 pipeline，所以只在这里查一次就够了。

用法：python gate_remote.py OUT_DIR [ROUNDS]
退出码：门禁自己的 0 / 1 / 2 / 3，外加 4 = 退回了本地向量。
"""
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
rounds = sys.argv[2] if len(sys.argv) > 2 else "3"
out.mkdir(parents=True, exist_ok=True)

import benchmark.runner as runner_mod  # noqa: E402

original_init = runner_mod.BenchmarkRunner.__init__


def init_remote_only(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    status = dict(getattr(self.pipeline, "embedding_status", {}) or {})
    (out / "embedding.json").write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    if status.get("backend") != "remote" or status.get("fallback"):
        (out / "exit_code").write_text("4", encoding="utf-8")
        print(f"[gate_remote] Schema 检索没在用远端向量，退出：{status}", file=sys.stderr)
        sys.exit(4)


runner_mod.BenchmarkRunner.__init__ = init_remote_only

original_run = runner_mod.BenchmarkRunner.run
state = {"n": 0}


def run_and_keep(self, *args, **kwargs):
    report = original_run(self, *args, **kwargs)
    state["n"] += 1
    (out / f"round{state['n']}.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    return report


runner_mod.BenchmarkRunner.run = run_and_keep

import benchmark.gate as gate  # noqa: E402

original_holdout = gate.run_holdout_recall
holdout_state = {"n": 0}


def holdout_and_keep(*args, **kwargs):
    value = original_holdout(*args, **kwargs)
    holdout_state["n"] += 1
    (out / f"holdout{holdout_state['n']}.json").write_text(json.dumps({"holdout_recall": value}), encoding="utf-8")
    return value


gate.run_holdout_recall = holdout_and_keep

sys.argv = ["benchmark.gate", "--repeat", rounds, "--partial-output", str(out / "incomplete.json")]
code = gate.main()
(out / "exit_code").write_text(str(code), encoding="utf-8")
sys.exit(code)
