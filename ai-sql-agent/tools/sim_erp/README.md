# 模拟 ERP

一份 27,000 行的模拟业务数据，按美国橱柜厂的口径编（builder / GC 客户、job / lot /
subdivision、不含税金额、change order、punch list），用来**压测系统能力**。

不是演示数据的加厚版，是拿来找系统答错在哪的。

## 为什么需要它

项目自带的四个演示库加起来约 100 行——8 张销售订单、9 张工单。那个规模能证明
一条查询跑得通，但证明不了别的：

- **聚合问题没有意义**。"上个月签了多少单"在 8 行数据上问不出名堂。
- **规划器从来不需要真的筛选**，因为所有数据一眼就能看完。
- **整类客户问题背后没有数据**。用 12 个真实问法探过一轮，"什么时候送货"和
  "我少了两个铰链"这两类，无论怎么调模型都答不了——不是模型问题，是这些业务
  对象压根没被建模。演示库的 `payment` 表只关联采购单不关联销售订单，所以
  "尾款还差多少"在**结构上**就无解。

这份数据把这些补上了：1,200 个工程、2,483 张应收发票、936 条送货记录、
410 张售后单、516 条补件请求。

## 这份数据**不能**代替什么

**它不预演"读不懂真 ERP 表结构"这个问题。** 真实的金蝶 / 用友 / SAP 抽出来长这样：
表叫 `T_SAL_ORDER`，字段叫 `FBILLNO`、`FDOCUMENTSTATUS`，状态是单字母代码，没有注释。
这里的字段名是业务白话（`job_no`、`contract_amount`），刻意的——那道坎是接真 ERP
最大的一块工作量，而这份数据**绕开了它**。别把跑通这份数据当成"接真库没问题了"。

它预演的是另外三件事，而且都真的踩到了：语义层怎么写、跨库关联怎么声明、
以及配置层的坑（见下）。

## 怎么用

```bash
# 1. 生成数据（确定性的，同一个 SEED 出同样的库）
python tools/sim_erp/build.py --out runtime_data/sim

# 2. 生成语义层 YAML（描述改在 tools/sim_erp/semantic.py 的规格表里，不要手改 YAML）
python tools/sim_erp/semantic.py

# 3. 校验：会连上真实库比对结构
python -m ai_sql_agent.semantic validate tools/sim_erp/semantic

# 4. 指过去跑
AI_SQL_SEMANTIC_DIR=tools/sim_erp/semantic python -m uvicorn ai_sql_agent.api.app:app --port 8000
```

演示库不受影响：不配 `AI_SQL_SEMANTIC_DIR` 就还是原来的四个演示库。

## 四个库

| 库 | 表 | 回答什么 |
| --- | --- | --- |
| `erp_db` | customer, job, job_item, change_order, invoice, payment_received | 这单多少钱、还欠多少、改过什么 |
| `prod_db` | work_order, stage_log, qc_check | 做到哪一步了、为什么卡住 |
| `wms_db` | supplier, material, receipt, inventory | 有没有货、供应商准不准时 |
| `svc_db` | delivery, service_ticket, replacement_part | 什么时候送、少件怎么补、补件寄了吗 |

## 埋进去的边界情况

压测的价值全在这些地方。每一条都是真实业务里会有的形状，也都是容易**静默答错**的地方：

| # | 情况 | 错在哪 |
| --- | --- | --- |
| 1 | 4% 工程已取消 | 算签单量时不排除会高估 |
| 2 | 6% 工程只是签了没下达，没有工单 | 与 `prod_db` 做内连接静默丢掉约 14% 的工程 |
| 3 | 商业工程 `subdivision`/`lot_no`/`plan_name` 全为 NULL | 按小区筛会把整个商业业务筛没 |
| 4 | 零售客户 `credit_limit` 为 NULL 不是 0 | `AVG` 跳过它们、`> x` 也筛不到 |
| 5 | 变更单金额有正有负（builder 折让是负数） | 不用 `SUM(amount_delta)` 就算错 |
| 5b | 待审变更单 `approved_date` 为 NULL | 算审批时长要排除 |
| 6 | 457 张发票已过到期日仍未收清 | 逾期判据要同时看 `due_date` 和状态 |
| 6b | 部分回款晚于到期日 | "谁付款晚"才有得算 |
| 7 | 402 张发票是 `partial` | 状态读不出还差多少，必须开票额减回款合计 |
| 8 | 在制工单 `completed_date` 为 NULL | 按完工日期过滤会排除掉所有在做的 |
| 8b | 17 张工单挂起并带 `hold_reason` | "为什么卡住"只有这些答得上来 |
| 9 | 当前工序 `finished_at` 为 NULL | 算工序耗时不排除会把在制算成零耗时 |
| 10 | 全合格质检 `defect_code` 为 NULL | `COUNT(defect_code) != COUNT(*)`，这正是算法本身 |
| 11 | 277 条送货已过计划日期仍未送达 | 逾期送货的判据 |
| 11b | `failed` / `rescheduled` 的送货都还没送到 | 只按 `delivered` 反查会错算成在途 |
| 12 | 113 张售后单未结、`resolved_date` 为 NULL | 算平均处理时长不排除一定偏小 |
| 13 | 近似重名客户（Marlowe Homes / Marlowe Homes LLC） | 精确匹配会漏，模糊匹配会多 |
| 14 | 36 行州名大小写不一致（Ca / Nv） | `WHERE state='CA'` 静默漏掉 |
| 15 | 15% 的物料从未盘点，`last_counted` 为 NULL | 这些账面数最不可信 |
| 16 | lot 号跨小区重复 | 单个 lot 42 不能唯一定位工程 |
| 17 | 9% 到货记录 `received_date` 为 NULL（压根没来） | 只比较到货日晚于承诺日，会漏掉最严重的迟到 |

## 建这份数据时踩到的一个真问题

`PipelineConfig.db_path` 以前默认是 `runtime_data/erp_demo.db`（非空），而
`_resolve_databases` 里那个"兼容既有用法"的覆盖分支因此**无条件触发**：语义层里
任何叫 `erp_db` 的库，连接都会被静默替换成内置演示文件。

现象是最难查的那种——语义层加载正常、日志显示四个库都在、`/admin/schema/status`
四个指纹都对，但模型看到的是演示库的表，SQL 照跑、有结果、结果来自错误的库。
四个库里只有重名的那个是错的，另外三个都对，所以第一眼看不出是配置问题。

已修：`db_path` 默认改为空，覆盖只在调用方显式指定时生效。演示路径行为不变——
演示语义层自己就声明了同一个路径，那个默认值本来就是重复的。

## 数据规模

```
erp.customer               42     prod.work_order       1,083
erp.job                 1,200     prod.stage_log        6,011
erp.job_item            9,064     prod.qc_check           780
erp.change_order          424     wms.supplier             12
erp.invoice             2,483     wms.material            140
erp.payment_received    2,301     wms.receipt           1,800
svc.delivery              936     wms.inventory           140
svc.service_ticket        410
svc.replacement_part      516     总计                 27,342
```

数据窗口 2025-01-01 至 2026-09-03，一直到"今天"——时间相关的问题（"上个月签了多少"、
"这周要送的"）只有数据延伸到当下才问得出意义。
