# 真实模型评测基线（2026-09-08）

绑定 model + commit + 数据版本的门禁评测结果，3 轮取均值与区间。

## 结论：门禁 **FAIL**（2 项硬性不达标，1 项压线不达标）

| 指标 | 均值 | 区间 | 门禁线 | 结果 |
| --- | --- | --- | --- | --- |
| 单库执行准确率 | 89.60% | [88.00%, 91.20%] | ≥ 85% | ✅ PASS |
| 跨库执行准确率 | 73.33% | [72.00%, 76.00%] | ≥ 90% | ❌ **FAIL** |
| 字段召回率（评测集） | 100% | [100%, 100%] | ≥ 95% | ✅ PASS |
| 字段召回率（留出集） | 100% | [100%, 100%] | ≥ 85% | ✅ PASS |
| 平均进图字段占比 | 43.63% | [43.56%, 43.75%] | ≤ 40% | ❌ **FAIL**（压线） |
| 静默错误率 | 10.00% | [8.67%, 11.33%] | ≤ 3% | ❌ **FAIL** |
| 风险提示误报率 | 5.88% | [5.38%, 6.20%] | ≤ 20% | ✅ PASS |
| 检索/进Prompt 召回落差 | 0.00% | [0.00%, 0.00%] | ≤ 3% | ✅ PASS |

分类明细（3 轮均值）：single_table 97.3%、having 100%、time_range 93.3%、
aggregation 92.0%、multi_join 65.3%、cross_db 73.3%——multi_join 和 cross_db
是主要拖分项。

## 绑定项

| | 值 |
| --- | --- |
| model | `claude-sonnet-5`（chat + coder 同一个模型），provider anthropic，relay `claude-relay.liziqiao.com` |
| commit | `ee920e9`（分支 `feat/p0-authorization-and-business-outcome`），跑评测时工作区干净 |
| dataset | `benchmark/dataset.json`，150 条，六类各 25，sha256 `52b8041f...` |
| holdout | `benchmark/holdout.json`，40 条，sha256 `4623583f...` |
| thresholds | `benchmark/thresholds.json` full 档，sha256 `05198cb9...` |
| 语义层 | **内置演示元数据**（`AI_SQL_SEMANTIC_DIR` 留空），**不是**当前部署的 `tools/sim_erp/semantic` |

完整字段见同目录 `baseline.json`；单条题目级明细（含预测 SQL、是否命中、
是否降级/风险提示）见 `round1.json` / `round2.json` / `round3.json`。

## 一个前提性缺口：这份基线代表的是内置演示语义层，不是当前部署

直接跑评测会被 `GoldExecutor` 拦住：

```
DatasetSchemaMismatch: 评测夹具引用了这些库前缀：mes、pm，
但当前语义层只提供 erp_db、prod_db、svc_db、wms_db。
```

`benchmark/dataset.json` 的 150 条 gold SQL 是照内置演示元数据
（`erp_db`/`pm_db`/`mes_db`/`wms_db`）写的；当前部署用 `tools/sim_erp/semantic`
（`erp_db`/`prod_db`/`svc_db`/`wms_db`，见契约 v1.6 的改名说明）。
两者字段/表结构未必一致，**这份基线的准确率数字不能直接当成当前部署的准确率**——
它只回答"同一套 pipeline 代码，在标准评测夹具上跑得怎么样"。
要拿到当前部署口径的基线，需要按 `tools/sim_erp/semantic` 重写 `benchmark/dataset.json`
的 gold SQL，这是一件独立的数据工作（`runner.py:246` 的 `GoldExecutor` docstring
已说明为什么不能为了让夹具"跑通"去伪造 `mes_db`/`pm_db`）。

## 复现方法

```bash
cd ai-sql-agent
AI_SQL_SEMANTIC_DIR= .venv/bin/python -m benchmark.run_benchmark --output round.json
# 重复 3 次（模型输出不确定，单轮不足以判定，见 gate.py 的 --repeat 说明）
```

## 单轮不能引用的原因（供下次跑基线时参考）

同一份代码单轮波动明显：跨库准确率区间 [72%, 76%]，静默错误率区间
[8.67%, 11.33%]。`benchmark/gate.py` 的 `--repeat` 参数与本文档的口径一致，
发布门禁建议至少 3 轮取均值。
