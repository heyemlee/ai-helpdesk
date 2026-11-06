# 统一业务验收

本目录提供开发校准、固定题集评分、隔离镜像验收、旧版比较、成本记录与发布门禁。模型质量只有真实供应商完整执行的记录可以证明。模拟响应只验证连线、并发、故障和评分器接口；不产生可发布的质量分数。

## 冻结数据与独立裁判

默认使用 `data/v3`：54 道开发题，200 道发布题，后者固定为 60 道订单、50 道政策、50 道跨消息、40 道图片。12 道开发校准题写在 manifest 中。业务值来自独立合成记录，政策来自明确生效区间与适用条件；生成器和裁判均不导入产品核验器。实体、实际问法模板、政策正文模板和图片版式在两个集合间分开。

v1、v2 的所有文件与旧评测记录原样保留。v1 manifest SHA-256 为 `c918170dfc710f153d96ae1bb6b7b54230e8eb06e933589f29477077701e6ea6`，v2 为 `88a8954e19b0c7e7870b41e42b16f47b61630a83553e9eb6446640ee61f92b9b`，v3 为 `403fd101f834ee384b46c07570e1efefd3ac5275353d991f872f98b88b206b2e`。当初从 v1 到 v2 的修订发生在真实调用前，依据阶段 3 已接受的同实体、同查询行、可读型号锚点规则。图片分别评分像素提取和业务核验；无共同记录证明的字段不得提升为已核实，且所有未核实字段进入待补清单。保守的负面措辞可以不同，正面核验仍须满足严格规则。

v3 在 v2 已发生真实供应商调用之后修订，依据是开发评测中发现的政策问法歧义：问题只提“配送费用”等主题，正文实际规定处理时限或办理步骤。v3 仅明确 65 道政策题（开发 15、发布 50）的问法，其中 52 道询问处理时限、13 道询问办理步骤，两类都要求有效版本与引用；同时更新派生问法模板与模板哈希。v2 的全部 truth/gold、政策正文、业务记录、适用范围与边界、55 张图片、其余 189 道题、歧义与冲突要求、校准选择和验收门槛均保持不变。开发与发布继续采用不同模板族。v3 是经过开发反馈修订的语料，包含已经接触过模型的 v2 发布材料，不能描述为整套未评测或完全盲测；修订没有查阅旧正式基线答案内容，也没有据此修改 gold。旧版失败记录及原分数不被回填。v3 的真实评测使用另行建立、绑定新 manifest 哈希的批准与账本。

`revise_v3.py` 只从固定哈希的 v2 复制，逐字节复用图片，不调用模型或重新绘图。生成器默认验证 v3；显式 `--freeze` 同样走 v3 修订入口，只能写入不存在的新目录，拒绝覆盖冻结目录或写入 v1/v2 内部。原始 v2 合成函数仅保留供检查。差异白名单验证比较全部 JSON 叶字段、文件清单和图片字节哈希，额外修改 gold、范围、正文、其他题或门槛即使重新计算哈希也会失败。完整差异证明与测试记录见 `tests/evidence/corpus-v3/`。

`oracle.py` 核对原始查询行、单位、时点、引用、所有可见业务陈述、Case 状态与副作用。正确数字与错误数字同时出现不能通过。真实 API 的 `evidence_payload` 包装由保留的实际服务记录覆盖。空字段、零行、查询失败分开处理。任何无法自动证明的陈述不能自动算正确。旧版共同事实由 `common_facts.py` 按内容比较，无法自动判断时保留 `review_required`；审核须有明确归属、理由及绑定的原记录哈希。

两版共同事实使用相同复核入口：`python -m acceptance.review --report 报告.json --scope round-1或baseline --scenario 题号 --review 独立复核.json`。复核须注明 evidence_sha256、status=reviewed、reviewed_by、reviewer_type（human/ai）、rationale、fact_correct 与 independent_of_system_under_test=true；AI 复核另注明 reviewer_model，不能冒充人工。入口只接收已完成的独立判断，不生成占位通过结论，也不覆盖确定性错误或执行失败。强制共同事实复核仅适用于两版共同支持的 130 题；其余 70 题仍须通过候选版正式契约评分，不因共同事实评分未知而放行。任一共同支持题仍未测或未判时，两版准确率保持缺口，完整验收被阻止。真实记录尚未到齐时不制作复核结论。

## 离线入口

在仓库根目录使用已安装依赖的 Python。后台产品测试使用维护中的 `.claude/skills/test-backend/scripts/run.sh --full` 与 `run-sql-agent.sh --full`，不在真实 backend 环境直接运行 pytest。

```sh
backend/.venv/bin/python -m unittest discover -s acceptance/tests
backend/.venv/bin/python -m acceptance.generate
backend/.venv/bin/python -m acceptance.revise_v3 --proof /private/tmp/v3-diff.json
# 仅在需要独立重现冻结文件时使用一个尚不存在的目录；不会改写 data/v3。
backend/.venv/bin/python -m acceptance.generate --freeze --output /private/tmp/new-acceptance-v3
backend/.venv/bin/python -m acceptance.stack --simulated-delay 0.8
backend/.venv/bin/python -m acceptance.runner --stack /private/tmp/aihd-stage6-STACK --output /private/tmp/new-development-run
backend/.venv/bin/python -m acceptance.load --stack /private/tmp/aihd-stage6-STACK --output /private/tmp/load.json --mixed --steady-seconds 180 --burst-seconds 120
```

源码副本和本地运行环境只适合开发校准。`container_stack.py` 从实际镜像 ID 启动 API、独立 Worker、SQL Agent 和前端，核对正在运行的容器镜像。产品源代码不挂载覆盖镜像；只挂载只读的验收辅助代码和合成数据，并记录辅助代码哈希。PG、Redis、模型代理及应用只绑定随机本机端口，保留所有失败记录。合成数量字段在测试数据字典中明确配置为 `pcs`，不会在返回结果后猜测单位。

```sh
python scripts/operations/build_release.py --ref HEAD --output /private/tmp/candidate.json
python scripts/operations/build_release.py --ref 5310da9 --baseline --output /private/tmp/baseline.json
backend/.venv/bin/python -m acceptance.campaign --manifest /private/tmp/candidate.json --baseline-manifest /private/tmp/baseline.json --provider simulated --max-scenarios 3 --output /private/tmp/new-image-rehearsal
```

`campaign` 总是创建三套独立状态，固定分母仍是 200。上例只检查每轮前三题，报告必须拒绝发布。旧版真实 OpenAPI 能力清单与每题记录一同保存：附件、显式多 Case、纠正与重开契约列为 unsupported；普通补充与指代沿用同一 conv_id 实测共同事实。已经请求却失败的题按 measured_failure 留在分母，没有执行的题才是 unmeasured。共同事实比较采用两版相同的 130 题子集和同一裁判，Case 契约另行评分。正式真实评测移除 smoke 限制，并另行提供明确批准的批量预算；开发校准批准不适用于正式评测。

## 调用与成本边界

默认关闭真实供应商。仅代理进程读取已配置的 HTTPS 地址和凭据，应用只得到随机本地令牌。每次实际 HTTP 出站前在持久 SQLite 中原子预占请求额度；失败、重试和背景调用均消耗额度，未知用量不按零处理。SDK 重试为零，输出上限为 2,048 token。Run 与父 Run 的关联是不可改写的，延迟背景请求不会归到“最近一道题”。

开发校准预设上限为 12 道、累计 40 个真实 HTTP 请求，但配置本身不代表用户批准。审批未获接受时不得调用。供应商费率没有可核实资料，实际费用保持未知。`.stage6` 内账本不得通过重建临时环境重置。

```sh
backend/.venv/bin/python -m acceptance.cost_plan --ledger-report /private/tmp/calibration-ledger.json --output /private/tmp/proposed-budget.json
```

没有校准数据时计划返回 insufficient_calibration，不编造预算或价格。有样本时按题与题组列出调用数、输入/输出 token 分布、已知小计和未知次数，并提供基于最大观测请求数的暂定请求上限及输出 token 上界，仍需单独批准。只有部分 usage 已知时总量保持未知。

正式预算必须另含 scope=release_and_baseline、confirmed、用户批准消息引用、唯一 id、supplier_base_url、固定语料哈希、model、requests、scenarios 与 max_output_tokens。JSON 文件自身不构成批准。浏览器、真实负载小样本、三轮和基线共享同一持久账本。代理记录真实 HTTP 回执、响应摘要和 Run 身份；模拟回执、零请求、错模型、错目的地、篡改计数或缺少 Run 归属均不能通过门禁。

## 发布门禁与证据

正式报告要求同一提交、同一组三个镜像、同一固定配置：三轮完整 200 题每轮至少 190 题正确；跨消息每轮至少 48/50；五类图片字段分母完整；规定不变量 0/N 且 N 大于零；真实模型浏览器流程、50 人混合负载、至少 200 个 Case 的本地及 S3 恢复、非空图片提取与 SQL 审计、回滚和旧版比较齐全。

`gate.require` 打开每个原记录、核对文件哈希与镜像/配置，再重新计算逐题分数、共同事实对比、真实负载小样本和不变量计数。基线逐题也必须绑定固定完整提交对应的三镜像。阶段 5 报告、校准报告、乐观汇总、丢失的文件、另一个镜像和不完整分母都不能授权发布。源码副本的证据不能添加镜像标签冒充镜像验收。

CI 常规运行只做离线验证。手动发布流程下载先前保存的同一镜像归档及外部验收包，在登录 registry 和推送前执行统一门禁。实际发布、远端推送、生产部署不属于本地验收动作。

负载报告分别记录业务类型延迟、排队和执行峰值、失败原因与排空时间。Run 终止不等于业务解决，也不等于回答正确。恢复历史提取记录明确标为 fixture-only，只证明持久性；模拟 S3 不代表真实云 IAM 或跨机恢复已验证。

镜像基础设施负载固定采用 0.8 秒模拟响应延迟、4 个 Worker 并发、30 秒重派间隔和原始降级阈值；完整 Run 结果及 Case 状态随记录保存。门禁核对 50 个不同员工同时提交、三种业务、按时长计算的稳态/突发次数和实际到达时间，再独立评分每条业务结果。`faults` 在独立镜像项目执行权限撤销、角色/实例隔离、陈旧审批、重复完成回调、Worker 替换、Redis 丢失、取消、SQL 只读和来源/证据篡改。fault_scenario_count 是实际场景数；trials.total 明确为观测断言数，并非独立重复注入次数。0/N 不引用历史测试通过数。真实模型样本独立保存为 load_real 组件，不能用模拟负载代替。

```sh
backend/.venv/bin/python -m acceptance.faults --manifest /private/tmp/candidate.json --output /private/tmp/invariants.json
backend/.venv/bin/python -m acceptance.recovery_campaign --manifest /private/tmp/candidate.json --output /private/tmp/recovery-local
backend/.venv/bin/python -m acceptance.recovery_campaign --manifest /private/tmp/candidate.json --output /private/tmp/recovery-s3 --s3
# 下面两项真实工作仅在相应批量预算另获批准后运行。
backend/.venv/bin/python -m acceptance.load_sample --stack /private/tmp/aihd-stage6-REAL --budget /private/tmp/approved-budget.json --output /private/tmp/load-real.json
backend/.venv/bin/python -m acceptance.assemble --campaign /private/tmp/campaign --budget /private/tmp/approved-budget.json --component invariants=/private/tmp/invariants.json --component load_real=/private/tmp/load-real.json
```

## 外部证据包交付

在冻结提交上导出一次镜像。后续质量、浏览器、负载和恢复均使用这组三镜像，代码若改动必须重建并重新取对应证据，不能给旧记录改标签。`assemble` 保留 campaign 原报告，生成不可覆盖的 assembled-哈希.json。完成所有门禁后，用下列离线命令将所选 assembled 文件的原字节置于包内 report.json，所有引用原记录、清单及文件摘要一并保存：

```sh
backend/.venv/bin/python -m acceptance.bundle pack --report /private/tmp/campaign/assembled-HASH.json --manifest /private/tmp/candidate.json --output /private/tmp/stage6-acceptance.zip
```

此命令拒绝不完整或失败验收。由获授权的操作者将 stage6-acceptance.zip 作为本仓库 release 的资产上传后，在受信 main 上运行 `Receive externally measured acceptance`，输入完整 candidate_revision、release tag 和离线返回的 SHA-256。接收流程检查候选属于本仓库 main 历史、包含已接受的认证边界，然后校验整个包和每个文件，重新运行门禁后上传名为 stage6-acceptance 的 Actions artifact。它不执行包内代码、不调用供应商、不重建镜像。

随后 `publish-verified` 选择同一个 candidate_revision、原镜像所在 release_run_id 与上述接收流程的 acceptance_run_id；它再次检查受信历史和候选提交、原镜像 ID、报告完全一致，再使用已保存的镜像。main 上后续文档提交不会要求重建候选镜像。上传、发布和生产部署须有对应授权；本地验收不会执行这些动作。
