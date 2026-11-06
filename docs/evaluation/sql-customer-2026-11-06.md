# 客户问法：新判分规则真实模型重跑

2026-11-06（America/Los_Angeles），单轮 24/24 完成，`incomplete=false`。按现行判分通过 **15/24（62.50%）**；未通过 9 题，必须结合下列错因理解，不能全部解释为业务事实答错。

执行时间：2026-11-06 09:11:14 PST 至 2026-11-06 09:26:51 PST，总耗时 937.0 秒。

## 固定条件与范围

- 代码固定于 `26c4a75a4bd3ad5d5408a38611ff100d0c47bd9e` 的干净导出；包含截至 `c5aeacc` 的多步骤可用性、截断和别名矛盾检查。运行中未改题、标准答案、提示词或判分。
- 题集为 `ai-sql-agent/benchmark/customer_cases.json`，24 题（中文 20、英文 4，全部包含订单号），SHA-256：`2aac3f23251019e76551020c3540cfffd87ee3d2288c6d086cf85c8be2f2170e`。
- 模拟 ERP 按现有固定种子生成于临时目录。标准答案由题集的 gold SQL 查询该库取得；未连接已有业务数据库。Schema 向量索引只在内存中构建，pgvector DSN 关闭。
- 沿用运行中 SQL 服务已有模型连接：生成/路由/规划/校验均请求 `gpt-6.1-sol`，第三方兼容网关 `api.kabi.pro`，Chat Completions 协议；成功响应返回同一模型名。Embedding 使用 SiliconFlow 的 `Qwen/Qwen3-Embedding-0.6B`，1024 维，初始化确认 remote，未回落本地哈希向量。
- 这是 SQL Agent 单独的客户问法评测，先记录生产路由，再强制运行 Pipeline；没有执行 Helpdesk 最终回复、Case、附件或正式三轮发布验收，不计入 150 题 SQL 门禁。此次没有部署或修改运行中的模型配置。

## 判分规则

1. 行数相同，标准答案各行的值能一一对应到模型结果；忽略列名/行列顺序，最多允许多输出一列。
2. 最后一步按原规则判分；若正确答案在前面的步骤，补计前必须确认整轮没有风险提示或降级、该步完整进入答案预览，并且其他成功步骤没有给出同一答案字段的矛盾值。
3. 矛盾检查包含匹配步骤使用的字段别名；完全不同列名所表达的语义矛盾仍不在自动检查覆盖内。

## 结果

| 指标 | 本轮 |
|---|---|
| 新规则通过 | 15/24（62.50%） |
| 未通过：输出列过多 | 6 |
| 未通过：取值不对 | 1 |
| 未通过：行数不对 | 1 |
| 未通过：空结果 | 1 |
| 其他未通过 | 0 |
| 静默未通过（原指标，含输出列过多） | 8/24（33.33%） |
| 其中取值/行数不对且无风险提示 | 2/24（8.33%），c10、c24 |
| 正确答案位于前面步骤且通过可用性检查 | 1，c03 |
| 前面步骤匹配但未满足可用性检查 | 1，c18 |
| 模型路由为查库 | 24/24 |
| 进 Prompt 字段召回 | 100.00% |
| 检索字段召回（裁剪前） | 100.00% |

对本轮同一批输出离线重新计数：仅看最后一步为 14/24；任一步匹配、尚未执行可用性检查为 16/24；最终采用现行新规则的 15/24。该对照没有新增模型调用，不是另一轮评测或旧代码质量比较。

**错误标签不等于已证实的错误业务陈述。** c10 将一条售后单状态与两条补件明细合并成三行，导致行数不符合 gold，并非查到了错误订单。c24 原问是“补件是否已发货”，gold 只输出发货日期 `2026-04-13`，模型返回 `is_shipped=1`；它没有提供标准答案要求的日期，因此被程序归为 `wrong_values`，但“已发货”这个判断与该日期一致。此处存在题目措辞与 gold 输出要求的口径差异。本轮保留既定标准答案和 15/24 分数，将差异明确列出；没有把这两题自动认定为已发生的错误客户答复。

## 实际调用与用量

总计 **219 次外部 HTTP 请求尝试**：GPT 生成 185 次、Embedding 34 次、其他 0 次。状态分布：`{"200": 218, "ConnectTimeout": 1}`。首次 GPT 连接超时后由现有重试机制恢复；全部题完成，无降级题。

| 类型 | 输入 token | 输出 token | 有 usage 的响应数 |
|---|---:|---:|---:|
| GPT-6.1 Sol | 227,032 | 14,334 | 184 |
| Embedding | 8,420 | 0 | 34 |

用量按供应商响应累加，包含初始化索引、路由、各步骤校验和生成。连接超时没有返回 usage；不能根据 token 数推断网关实际扣费，账单金额未取得。

单题顺序运行耗时（包含路由、检索、规划、SQL 和校验）：p50 29.12 秒，p95 75.77 秒（nearest rank），范围 20.45–78.73 秒。这不是并发容量或端到端 Helpdesk 延迟。

## 逐题记录

| 题号 | 客户问法 | 判定 | 匹配步骤 | 耗时（秒） |
|---|---|---|---:|---:|
| c01 | 我的订单 J26-00084 到哪了？ | 通过 | 3 | 78.73 |
| c02 | J26-00010 这单现在做到哪一步了？ | 取值都对，多输出了列 | — | 41.57 |
| c03 | 我这单 J26-00044 还要多久能送到？ | 通过 | 1 | 66.62 |
| c04 | J25-00213 怎么还没发货？ | 取值都对，多输出了列 | — | 69.58 |
| c05 | J26-00910 怎么还没做完？现在卡在哪道工序，什么原因？ | 取值都对，多输出了列 | — | 32.73 |
| c06 | 我家柜子 J26-00038 是哪天送到的？ | 通过 | 1 | 23.20 |
| c07 | 我这单 J26-00099 一共多少钱？ | 通过 | 1 | 20.45 |
| c08 | J26-00108 我订了哪些东西，每样几件？ | 通过 | 1 | 26.91 |
| c09 | 我那单 J25-00046 是不是被取消了？ | 通过 | 1 | 22.72 |
| c10 | J25-00036 之前报的缺件，现在处理得怎么样了？ | 行数不对 | — | 36.70 |
| c11 | J25-00137 我们报的几个问题现在都处理好了吗？ | 通过 | 1 | 28.81 |
| c12 | J25-00759 补发的铰链寄出来了没有？哪天寄的？ | 取值都对，多输出了列 | — | 34.03 |
| c13 | J25-00443 申请补发的件现在什么状态？ | 通过 | 1 | 25.46 |
| c14 | 我有两单 J26-00010 和 J26-00011，分别什么时候交货？ | 通过 | 1 | 22.84 |
| c15 | j26-00070 到哪了 | 通过 | 3 | 54.85 |
| c16 | 你好，我们是 J26-00014 那个项目，柜子开始做了没有？工地那边催得紧 | 取值都对，多输出了列 | — | 27.61 |
| c17 | J26-00039 是你们送货上门，还是要我们自己去提？ | 通过 | 1 | 29.42 |
| c18 | J26-00024 现在什么进度了？ | 查出来是空的 | 1 | 47.45 |
| c19 | J25-00068 是加急单吗？ | 通过 | 1 | 22.81 |
| c20 | J26-00119 的送货地址是哪个？我想核对一下 | 通过 | 1 | 25.85 |
| c21 | Where is my order J26-00142? | 通过 | 3 | 75.77 |
| c22 | When will order J25-00988 be delivered? | 通过 | 1 | 23.46 |
| c23 | Why hasn't J25-00094 shipped yet? | 取值都对，多输出了列 | — | 63.69 |
| c24 | Has the replacement part for order J25-00060 been shipped? | 取值不对 | — | 28.70 |

## 未通过题的事实与输出

完整逐步结果、标准答案、风险检查和请求用量均在 evidence.json；下面列出每道未通过题的标准答案和最后一步输出，保留“取值正确但列数超限”与“值/行数不对”的区别。

### c02：取值都对，多输出了列

J26-00010 这单现在做到哪一步了？

```json
{
  "gold_rows": [
    {
      "current_stage": "组装"
    }
  ],
  "last_step_rows": [
    {
      "order_no": "J26-00010",
      "current_stage": "组装",
      "production_status": "生产中"
    }
  ],
  "matched_step": null,
  "unusable_match": false,
  "flag_reasons": []
}
```

### c04：取值都对，多输出了列

J25-00213 怎么还没发货？

```json
{
  "gold_rows": [
    {
      "delivery_notes": "site not ready；配送失败，待重新配送"
    }
  ],
  "last_step_rows": [
    {
      "order_no": "J25-00213",
      "delivery_status": "待配送",
      "delivery_notes": "site not ready；配送失败，待重新配送"
    }
  ],
  "matched_step": null,
  "unusable_match": false,
  "flag_reasons": []
}
```

### c05：取值都对，多输出了列

J26-00910 怎么还没做完？现在卡在哪道工序，什么原因？

```json
{
  "gold_rows": [
    {
      "current_stage": "包装",
      "production_notes": "material shortage"
    }
  ],
  "last_step_rows": [
    {
      "order_no": "J26-00910",
      "production_status": "生产中",
      "current_stage": "包装",
      "production_notes": "material shortage"
    }
  ],
  "matched_step": null,
  "unusable_match": false,
  "flag_reasons": []
}
```

### c10：行数不对

J25-00036 之前报的缺件，现在处理得怎么样了？

```json
{
  "gold_rows": [
    {
      "service_status": "处理中"
    }
  ],
  "last_step_rows": [
    {
      "order_no": "J25-00036",
      "description": "Missing parts reported at install",
      "service_status": "处理中",
      "service_notes": "报修人：installer",
      "part_name": null,
      "quantity": null,
      "part_status": null,
      "shipped_date": null,
      "part_notes": null
    },
    {
      "order_no": "J25-00036",
      "description": null,
      "service_status": null,
      "service_notes": null,
      "part_name": "Granite countertop, 24 sqft",
      "quantity": 4,
      "part_status": "待处理",
      "shipped_date": null,
      "part_notes": "补件原因：缺件"
    },
    {
      "order_no": "J25-00036",
      "description": null,
      "service_status": null,
      "service_notes": null,
      "part_name": "Tall pantry 12\"x84\"",
      "quantity": 2,
      "part_status": "待处理",
      "shipped_date": null,
      "part_notes": "补件原因：缺件"
    }
  ],
  "matched_step": null,
  "unusable_match": false,
  "flag_reasons": []
}
```

### c12：取值都对，多输出了列

J25-00759 补发的铰链寄出来了没有？哪天寄的？

```json
{
  "gold_rows": [
    {
      "shipped_date": "2025-09-08"
    }
  ],
  "last_step_rows": [
    {
      "part_name": "Soft-close hinge set",
      "shipping_confirmation": "已寄出",
      "shipped_date": "2025-09-08"
    }
  ],
  "matched_step": null,
  "unusable_match": false,
  "flag_reasons": []
}
```

### c16：取值都对，多输出了列

你好，我们是 J26-00014 那个项目，柜子开始做了没有？工地那边催得紧

```json
{
  "gold_rows": [
    {
      "production_status": "生产中"
    }
  ],
  "last_step_rows": [
    {
      "order_no": "J26-00014",
      "production_status": "生产中",
      "current_stage": "组装",
      "production_notes": null
    }
  ],
  "matched_step": null,
  "unusable_match": false,
  "flag_reasons": []
}
```

### c18：查出来是空的

J26-00024 现在什么进度了？

```json
{
  "gold_rows": [
    {
      "order_status": "已签单"
    }
  ],
  "last_step_rows": [],
  "matched_step": 1,
  "unusable_match": true,
  "flag_reasons": [
    "empty_result",
    "empty_result"
  ]
}
```

### c23：取值都对，多输出了列

Why hasn't J25-00094 shipped yet?

```json
{
  "gold_rows": [
    {
      "delivery_notes": "access blocked by other trades；配送失败，待重新配送"
    }
  ],
  "last_step_rows": [
    {
      "order_no": "J25-00094",
      "delivery_status": "待配送",
      "delivery_notes": "access blocked by other trades；配送失败，待重新配送"
    }
  ],
  "matched_step": null,
  "unusable_match": false,
  "flag_reasons": []
}
```

### c24：程序标签为“取值不对”，实际缺少 gold 要求的日期

Has the replacement part for order J25-00060 been shipped?

```json
{
  "gold_rows": [
    {
      "shipped_date": "2026-04-13"
    }
  ],
  "last_step_rows": [
    {
      "part_name": "Soft-close hinge set",
      "is_shipped": 1
    }
  ],
  "matched_step": null,
  "unusable_match": false,
  "flag_reasons": []
}
```

## 证据与复现

运行前在同一固定源码副本中验证 `tests/test_customer_cases.py`、`tests/test_score_all_steps.py`：19 passed。没有为这次运行修改运行时代码。标准入口为：

```bash
python -m benchmark.run_benchmark --customer --output customer-report.json
```

本轮额外使用外部观测脚本记录逐题用量、原始查询结果和实际请求尝试；其 SHA-256 保存在 run-metadata.json。模型凭据只经隔离进程读取，不在归档中。

- [原始判分报告](evidence/sql-customer-2026-11-06/report.json)
- [固定配置、统计与用量](evidence/sql-customer-2026-11-06/run-metadata.json)
- [标准答案、逐步结果、请求和事件](evidence/sql-customer-2026-11-06/evidence.json)
- [离线检查日志](evidence/sql-customer-2026-11-06/offline-check.log)
- [观测脚本](evidence/sql-customer-2026-11-06/capture-run.py)（需置于固定源码导出的上一级目录，配置文件另行安全提供）
- [归档文件 SHA-256](evidence/sql-customer-2026-11-06/sha256.json)

单轮 24 题只用于定位当前客户问法的不足，不能替代固定保留集三轮正式验收。此次判分也不自动评价自由文本解释中所有语义错误；不与旧代码、不同模型或旧判分口径的历史数字直接作提升/退化结论。
