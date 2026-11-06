# 阶段 6 首轮真实校准结果与诊断

2026-11-06，首轮有限真实校准已因累计 40 次请求额度用尽而停止。5 个开发场景已执行：3 个事实正确，1 个图片数据库核验失败，1 个生产查询被本地预算中断；另有 7 个计划场景未执行。图片五字段均提取正确，但没有取得可采信的数据库核验结果。**本轮不足以证明正式验收质量，也不适合直接外推正式批量预算。**

请求发生于洛杉矶时间 09:15:10 至 09:18:48。最终账本记录输入 208,291 token、输出 5,803 token，全部 40 次请求均结束并返回 HTTP 200；供应商费率、账单及美元费用仍未知。首轮有限授权本身不包括正式三轮、旧版基线、真实浏览器和真实负载样本。

用户随后在主对话回复“没有预算用就好了”，已经授权继续必要评测且不设美元预算上限，见[后续授权记录](../../.stage6/evaluation/2026-11-06/authorization.json)。后续使用独立持久账本，仍只向原目的地发送合成资料，使用同一模型、单次最多 2048 输出 token、无自动 HTTP 重试。费用未知是报告限制，不再是等待授权的阻断。本报告记录首轮结果及本地修复，不宣称真实复测或正式验收已通过。

## 执行范围与证据

用户在主对话直接授权将合成订单、政策、图片和应用提示发送至现有 `https://api.kabi.pro/v1/responses`，使用 `gpt-6.1-sol`，最多 12 个选定开发场景、累计 40 次实际请求、每次最多 2048 个输出 token、无自动 HTTP 重试。授权范围见[授权记录](../../.stage6/calibration/2026-11-06-first/authorization.json)。

本轮运行源码副本，源提交为 `26c4a75a4bd3ad5d5408a38611ff100d0c47bd9e`；业务代码与固定镜像候选 `b1b10cf683a5f99803c628ec715cf14b55cad72e` 一致。它属于开发校准，不能替代同一组固定镜像上的正式测评。54 道开发题和 200 道保留题没有改动，语料 manifest SHA-256 仍为 `88a8954e19b0c7e7870b41e42b16f47b61630a83553e9eb6446640ee61f92b9b`。源码指纹和运行参数见[源码清单](../../.stage6/calibration/2026-11-06-first/source-manifest.json)及[运行来源](../../.stage6/calibration/2026-11-06-first/provenance.json)。

原始证据保存在项目内 `.stage6/calibration/2026-11-06-first/`，由 Git 忽略，不随克隆分发。可随仓库核对的衍生材料为[机器摘要](evidence/stage-6-calibration-2026-11-06/summary.json)与[原始文件索引](evidence/stage-6-calibration-2026-11-06/archive-index.json)。本次修复没有修改逐题记录、原始评分、账本或冻结语料；SQL 快照以只读方式检查。主对话已关闭首轮隔离栈，runner 正常退出，固定账本 `.stage6/calibration.sqlite` 保留且余额为 0，见[结束记录](../../.stage6/calibration/2026-11-06-first/execution-end.json)。

## 场景结果与完成口径

| 开发场景 | 结果 | 已归属请求 | 输入 token | 输出 token | 场景耗时 |
| --- | --- | ---: | ---: | ---: | ---: |
| [订单 001](../../.stage6/calibration/2026-11-06-first/development-orders-001.json) | 事实正确 | 7 | 36,323 | 448 | 34.769 秒 |
| [政策 001](../../.stage6/calibration/2026-11-06-first/development-policies-001.json) | 事实正确 | 4 | 14,748 | 378 | 20.821 秒 |
| [图片 001](../../.stage6/calibration/2026-11-06-first/development-images-001.json) | 提取 5/5，数据库核验 0/5，业务结果失败 | 14 | 77,941 | 3,711 | 102.926 秒 |
| [跨消息 001](../../.stage6/calibration/2026-11-06-first/development-cross_message-001.json) | 连续补充后事实正确 | 9 | 45,776 | 642 | 44.273 秒 |
| [订单 002](../../.stage6/calibration/2026-11-06-first/development-orders-002.json) | 生产查询被本地请求额度中断，质量未测完 | 5 | 29,031 | 352 | 16.031 秒 |
| 无 Run 归属的后台调用 | 用量已知，保留 `unknown/background` | 1 | 4,472 | 272 | 不归入场景耗时 |
| **合计** | **5 题执行，3 题事实正确** | **40** | **208,291** | **5,803** | — |

已对全部 5 份原始观察重新执行独立裁判，逐题分数及汇总均与[原始报告](../../.stage6/calibration/2026-11-06-first/report.json)一致。未被预算中断的 4 题中，3 题事实正确、1 题图片核验失败；这个有限且有顺序选择的小样本不能代表整体准确率。生产查询预算中断须单列，不能算作已经充分测量的模型业务错误。

原始门禁评分保留 12 题计划分母，因而仍显示 3/12、25%、`complete=false`；这是当前完成进度，不能称为模型准确率。剩余 7 题是政策 002、图片 006、跨消息 002、订单 003、政策 004、图片 011、跨消息 003。订单 002 的原始 `fact_correct=false` 不被覆写，但诊断明确标为预算中断。

5 个场景均完成了 runner 的观察，累计 8 个 Helpdesk Run 进入终态；这不等于业务都成功。事实正确的 3 题产出了符合预期的待审核草稿，Case 仍在处理中；本轮 `business_resolved=0`，没有把草稿当作已完成的补件、承诺或人工处理。

## 请求与费用口径

最终数字来自[最终账本导出](../../.stage6/calibration/2026-11-06-first/ledger-final.json)，不是按题数估算。SQL Agent 消耗 29 次请求、166,581 输入 token、4,747 输出 token；Helpdesk 消耗 11 次请求、41,710 输入 token、1,056 输出 token。40 条回执均绑定实际供应商地址、请求模型、响应模型、响应标识与响应摘要；没有未结束请求或未知 token 用量。实际单次输出最大为 1105 token，未达到 2048 上限。

合计 214,094 token；其中 87,296 个缓存输入 token 已包含在输入总量中，480 个推理输出 token 已包含在输出总量中，不能重复相加。供应商账单与缓存、推理计费规则未核实，不能据此报出美元费用。

按本轮全部消耗计算，每个已启动场景平均 8 次请求、41,658.2 输入 token、1,160.6 输出 token。若将失败、中断和后台消耗也计入取得 3 个正确结果的成本，则每个正确结果摊销约 13.33 次请求、69,430.33 输入 token、1,934.33 输出 token。这两个指标只描述本次校准，不能作为生产单次咨询成本承诺；“正确结果”也不等同于 Case 已解决。

请求 31 缺少自身 Run 和父 Run 标识，虽然输入 4,472、输出 272 token 完整，仍保留 `unknown/background`。SQL 日志在 09:18:14 记录了相同用量、无 trace 的模型调用；[会话快照](../../.stage6/calibration/2026-11-06-first/sql-state/memory.db)恰在该时点为图片查询会话保存了唯一一条短期摘要。这些记录支持“异步短期摘要调用丢失上下文”的诊断，但没有足够的请求级标识将其正式重归属到图片题。故障时的摘要线程提交没有显式传播调用上下文；新的[实现](../../ai-sql-agent/src/ai_sql_agent/memory/short_term.py)已补齐传播，但不据时间邻近改写旧账本归属。

订单 002 最后的 429 来自本地网关的 `Cumulative provider HTTP request budget exhausted`，不是供应商返回的限流。该拒绝没有产生第 41 次外部请求。SQL 记录为 `degraded=true`、`execution_status=failed`，Helpdesk 随后返回服务不可用，详见[SQL 日志](../../.stage6/calibration/2026-11-06-first/diagnostics/sql.log)和[应用日志](../../.stage6/calibration/2026-11-06-first/diagnostics/backend.log)。

## 图片失败的证据链

图片提取准确读出了订单 `SO-2026-11039`、型号 `BX-11039-24`、订购数量 `5 pcs`、计划交付日期 `2026-11-22` 和订单状态“已交付”。五个值、可读性及数量日期口径均通过独立提取评分。失败发生在取得并采用业务记录的环节，不能诊断为 OCR 失败。

图片对应 SQL Run 为 `d6d6020418e24699b4c46f82bf693944`，trace 为 `17ab39a529ce47a4`，可在[SQL Run 快照](../../.stage6/calibration/2026-11-06-first/sql-state/runs.db)和[SQL 审计快照](../../.stage6/calibration/2026-11-06-first/sql-state/audit.db)中复核。

1. **请求范围扩大。** 故障时图片查询文本同时列举订购、送达、缺件数量，应收和实收型号，以及多种日期；查询生成入口只接收订单标识，没有利用已提取字段收窄所需证据。实际检索选中了 ERP、售后、仓储三个库的八张表。首次规划还把售后表放进仓储步骤，触发跨库重规划；两次规划合计约 50.203 秒。这说明本次通用查询走了宽泛的多库路径，不能据一题推断所有图片都会如此。
2. **返回行不满足核验契约。** ERP 实际 SQL 用 `UNION ALL` 返回订单头和明细，但输出仅有 `order_ref`、`ordered_qty`、`query_source`，遗漏规划中已有的型号、订单状态及计划交付日期。售后结果只有 `query_source` 和包含 JSON 字符串的 `record`，并把实际送达日期作为该记录的日期口径；仓储结果只有 `material_name`、`uom`，没有订单标识。没有一行同时承载本图所需的订单、型号、数量、状态和日期证据。原始返回行保存在会话快照 `memory_messages.id=5` 的 `payload_json.step_logs[].execution_result` 中。
3. **语义风险已经被发现。** 三步 SQL 的数据库执行均成功，分别返回 2、3、1 行，但三步语义校验全部记录 `passed=false`、`semantic_mismatch`；没有修复重试，最终 `has_risk=true`。因此 SQL 的 `status=completed`、`execution_status=success` 只能说明语句执行结束，不能证明返回了正确核验材料。现有[来源信任检查](../../backend/app/core/evidence.py)明确拒绝 `has_risk=true` 的结果。
4. **图片等待先于 SQL 结束。** SQL Run 在 09:16:13.255 开始；图片核验记录于 09:17:43.275 保存为 `unavailable`，相隔约 90.020 秒；SQL 在 09:18:07.193 才完成，审计耗时 113.931 秒，HTTP 日志耗时 113.947 秒。故障时图片工作流正好使用 90 秒等待上限，并把异常统一变成 `unavailable`。时间轴与这条超时路径吻合；原记录未单独保留异常类型，不能声称直接读取到了 `TimeoutError`。后端随后将五字段置为待补充，没有把迟到或高风险结果提升为已核实。

由此可确认两层阻断：图片流程未在等待期限内拿到查询结果；迟到结果本身也缺少所需字段与同一行证据，并带有语义风险。只延长超时不能修复后者，也不能把 5/5 提取正确改记为 5/5 业务核实正确。

## 已实现的本地修复

业务与复测入口提交：`6090071d35d72ed7af65ae7288420d7dc132e90f`。该冻结提交也继承另一个独立聊天已提交的 SQL 规划修复 `aac613b`、`9f493f7`，不是仅含本次 19 个文件的孤立代码版本。本次实现没有追加供应商请求，以下行为由本地录制回放与隔离测试验证，真实效果仍须主对话复测。

1. [图纸查询](../../backend/app/attachments/verification.py)只请求清晰字段及其受控类型：本次五字段对应订单号、型号、订购数量、计划交付日期、订单状态。OCR 的自由文字、字段值、位置说明和声称单位不插入工具查询；字段不清晰或类型未知时不扩大查询。批量图片的查询缓存同时按订单和需求文本区分。
2. 离线回放发现[局部 schema 裁剪](../../ai-sql-agent/src/ai_sql_agent/generation/schema_store.py)的额外缺口：带限定名的关联键一旦命中，规划中的未限定字段列表会被裁掉。修复保留处理对象、操作指令及输出目标明确提到的已有字段，包括未限定筛选列；来源仍限于当前已授权 schema。录制规划中的型号、状态和交付日期可完整传给 SQL 生成器。
3. [投影检查](../../ai-sql-agent/src/ai_sql_agent/generation/projection.py)只针对明确要求原始列、原始记录或原始明细的计划，检查字段直接来源、指定别名和每个 UNION 分支的列完整性。普通逐条计算不触发；完整 UNION、直接来源 CTE 和 PostgreSQL 方言别名有回归覆盖。缺列可纠正一次，仍不满足则返回 `raw_projection_incomplete` 且不执行 SQL，初次生成及跨库纠正生成均覆盖。语义校验、授权检查、`has_risk` 拒绝及产品同一行/型号/单位规则均未放宽。
4. 产品图片等待上限改为 SQL 工具现有上限加 1 秒；验收配置下为 183 秒，避免先于 180 秒客户端及 182 秒工具结束。独立图片等待超时保留明确原因，调用方取消继续向下传播。本地测试验证取消待处理查询；尚未证明真实供应商会停止已受理的请求。浏览器图片等待改为 300 秒、后续草稿 240 秒、整个流程 660 秒，记录实际耗时；开发复测场景等待 600 秒。这些是观察截止时间调整，不改变质量评分或供应商请求超时。
5. 摘要线程及后续批次回调复制发起时的调用上下文。回归在另一个前台 Run 开始后才释放旧摘要，确认仍保留原 SQL Run 与父 Run，不转嫁给较新的请求；原请求 31 的未知归属保留。

修复前回放：SQL 全量 1,250 项中，仅新增录制缺列回归失败，其余 1,249 项通过。提示词快照重录只有 6 条 `CoderModelClient.generate_sql` 输入摘要改变，总记录仍为 56 条，没有改写验收答案或裁判。

最终隔离工作区为 `/private/tmp/aihd-stage6-check-6090071`，HEAD 固定为上述提交且工作区干净；维护中的两个全量脚本再从该工作区创建无真实配置的一次性副本。后端完整门禁（契约自检、ruff、pytest）1,939 项通过；SQL 全量 1,261 项、ERP 6 项、契约确认 41 项均通过，均无跳过。其中 41 项是全量内用例的重复确认，不能重复计作新增覆盖。验收工具 32 项通过，两个浏览器脚本通过语法检查。前端产品代码未改，本次没有重跑真实浏览器或供应商模型；新镜像的实际端到端表现尚未验证。提交级[测试记录](../../.stage6/validation/6090071/validation.json)保留执行命令、日志摘要和校验值，未把共享工作区迭代结果冒充提交级门禁。

## 后续真实复测与冻结要求

[开发复测入口](../../acceptance/runner.py)新增 `--budget`，与 `--real-calibration` 互斥，只接受真实固定镜像的 development 栈，逐项比对栈内 approval 与传入文件（包括 ID、额度、模型、目的地、语料和用途），通过后才打开既有 `release_ledger`。默认不开启真实调用，拒绝覆盖已有输出；正式账本归属校验没有放宽。

主对话已准备 `.stage6/evaluation/2026-11-06/development-approval.json`，用途 `development_revalidation`，账本 ID `stage6-dev-revalidation-20261106`。先前仅初始化但未使用的开发账本保留，不重置。正式三轮、基线、浏览器和小样本负载使用另一份正式账本，最终分别保留并汇总首轮、开发复测和正式用量。

由主对话从固定提交构建，全部本地门禁通过后再发起真实复测；所有输出路径须为新路径，运行中的栈路径采用启动器实际输出。构建脚本需要项目 Python，系统 Python 版本不支持所用的解包参数：

```sh
backend/.venv/bin/python scripts/operations/build_release.py --ref 6090071d35d72ed7af65ae7288420d7dc132e90f --output .stage6/evaluation/2026-11-06/candidate-6090071.json
backend/.venv/bin/python -m acceptance.container_stack --manifest .stage6/evaluation/2026-11-06/candidate-6090071.json --split development --provider real --budget .stage6/evaluation/2026-11-06/development-approval.json
backend/.venv/bin/python -m acceptance.runner --stack /private/tmp/aihd-stage6-images-ACTUAL --budget .stage6/evaluation/2026-11-06/development-approval.json --output .stage6/evaluation/2026-11-06/development-6090071
```

runner 默认运行 manifest 中原 12 道开发题，补足七类未执行题与被预算中断的生产题，核对图纸完整同一行证据及迟到摘要归属。若再次修改业务代码，必须重新提交、测门禁、构建镜像并复测；开发通过后冻结同一组镜像再开放保留集。正式入口为 `acceptance.campaign --manifest <冻结清单> --baseline-manifest <5310da9 基线清单> --provider real --budget <正式账本批准文件> --output <新目录>`，不传 `--max-scenarios`。它会运行三轮各 200 题及旧版基线。

真实浏览器使用 development 镜像栈及正式账本的 `browser:` 归属，入口 `acceptance.browser_run --stack <实际路径> --real-sample --budget <正式文件> --node-modules <Playwright依赖目录> --output <新目录>`；真实负载样本用 `acceptance.load_sample --stack <实际路径> --budget <同一正式文件> --output <新文件>`。完整 50 用户压力测试仍与真实供应商小样本分开。正式裁判、两版独立复核、浏览器、负载、故障恢复和回滚证据都必须对应新镜像；最后用 `acceptance.assemble` 重新导出完整账本并判门禁，缺项继续拒绝发布。

[自动成本草案](../../.stage6/calibration/2026-11-06-first/cost-plan.json)输出 14,000 次暂定请求上限，来自“已归属场景最大 14 次 × 800 × 1.25”，对应 28,672,000 个输出 token 的理论上界；文件仍明确 `authorized=false`。**此数目前不适合作为正式批量的可靠预算。** 原因包括：只有 4 题未被额度中断，图片题还失败；7 类已选场景未执行，生产查询只得到部分调用数；旧版基线调用路径未测；未知归属后台请求没有进入单题最大值；该公式没有完整展开真实浏览器、真实负载样本、失败后续与修复后的新路径，也没有已核实费率。

后续授权已经具备，不再等待用户给出美元预算。请求护栏用于避免失控运行，其数字不等于可靠成本估算。首轮 40 次账本保持耗尽；新候选尚未取得真实开发复测、正式三轮、共同基线、真实浏览器和负载证据，发布门禁继续拒绝放行。

## 核对信息

最终账本导出 SHA-256：`2dc5415ca9a45d095127362c43d2744b21249357a128b0f5947c0405e9d2ef59`。原始报告 SHA-256：`e1d599e1989996aac23142a422331e8d1c9cacbabbbb00f046e606f4b75730f8`。诊断日志的来源及保存哈希见[诊断索引](../../.stage6/calibration/2026-11-06-first/diagnostics/index.json)。

SQL 快照定位：`runs.db` 的 `ai_sql_runs` 与 `ai_sql_run_events`；`audit.db` 的 `ai_sql_audit_log.id=2`；`memory.db` 的 `memory_messages.id=5` 与 `conversation_summaries.id=1`。这些是本轮停止后的一致快照，未连接生产数据库。
