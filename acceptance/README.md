# 统一业务验收

本目录提供开发校准、固定题集评分、隔离镜像验收、旧版比较、成本记录与发布门禁。模型质量只有真实供应商完整执行的记录可以证明。模拟响应只验证连线、并发、故障和评分器接口；不产生可发布的质量分数。

## 冻结数据与独立裁判

默认使用 `data/v2`：54 道开发题，200 道保留题，后者固定为 60 道订单、50 道政策、50 道跨消息、40 道图片。12 道开发校准题写在 manifest 中。业务值来自独立合成记录，政策来自明确生效区间与适用条件；生成器和裁判均不导入产品核验器。实体、实际问法模板、政策正文模板和图片版式在两个集合间分开。

v1 完整保留，manifest SHA-256 为 `c918170dfc710f153d96ae1bb6b7b54230e8eb06e933589f29477077701e6ea6`。v2 为 `88a8954e19b0c7e7870b41e42b16f47b61630a83553e9eb6446640ee61f92b9b`。修订发生在任何真实供应商调用之前，依据阶段 3 已接受的同实体、同查询行、可读型号锚点规则。图片分别评分像素提取和业务核验；无共同记录证明的字段不得提升为已核实，且所有未核实字段进入待补清单。保守的负面措辞可以不同，正面核验仍须满足严格规则。

`oracle.py` 核对原始查询行、单位、时点、引用、所有可见业务陈述、Case 状态与副作用。正确数字与错误数字同时出现不能通过。真实 API 的 `evidence_payload` 包装由保留的实际服务记录覆盖。空字段、零行、查询失败分开处理。任何无法自动证明的陈述不能自动算正确。旧版共同事实由 `common_facts.py` 按内容比较，无法自动判断时保留 `review_required`；审核须有明确归属、理由及绑定的原记录哈希。

## 离线入口

在仓库根目录使用已安装依赖的 Python。后台产品测试使用维护中的 `.claude/skills/test-backend/scripts/run.sh --full` 与 `run-sql-agent.sh --full`，不在真实 backend 环境直接运行 pytest。

```sh
backend/.venv/bin/python -m unittest discover -s acceptance/tests
backend/.venv/bin/python -m acceptance.generate
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

`campaign` 总是创建三套独立状态，固定分母仍是 200。上例只检查每轮前三题，报告必须拒绝发布。旧版真实 OpenAPI 能力清单与每题记录一同保存；没有 Case 或附件能力的题逐项列为 unsupported，不当作通过。没有测量的共同事实题列为 unmeasured。正式真实评测移除 smoke 限制，并另行提供明确批准的批量预算；开发校准批准不适用于正式评测。

## 调用与成本边界

默认关闭真实供应商。仅代理进程读取已配置的 HTTPS 地址和凭据，应用只得到随机本地令牌。每次实际 HTTP 出站前在持久 SQLite 中原子预占请求额度；失败、重试和背景调用均消耗额度，未知用量不按零处理。SDK 重试为零，输出上限为 2,048 token。Run 与父 Run 的关联是不可改写的，延迟背景请求不会归到“最近一道题”。

开发校准预设上限为 12 道、累计 40 个真实 HTTP 请求，但配置本身不代表用户批准。审批未获接受时不得调用。供应商费率没有可核实资料，实际费用保持未知。`.stage6` 内账本不得通过重建临时环境重置。

```sh
backend/.venv/bin/python -m acceptance.cost_plan --ledger-report /private/tmp/calibration-ledger.json --output /private/tmp/proposed-budget.json
```

没有校准数据时计划返回 insufficient_calibration，不编造预算或价格。有样本时提供基于最大观测请求数的暂定请求上限及输出 token 上界，仍需单独批准。

## 发布门禁与证据

正式报告要求同一提交、同一组三个镜像、同一固定配置：三轮完整 200 题每轮至少 190 题正确；跨消息每轮至少 48/50；五类图片字段分母完整；规定不变量 0/N 且 N 大于零；真实模型浏览器流程、50 人混合负载、至少 200 个 Case 的本地及 S3 恢复、非空图片提取与 SQL 审计、回滚和旧版比较齐全。

`gate.require` 打开每个原记录、核对文件哈希与镜像/配置，再重新计算逐题分数和汇总。阶段 5 报告、校准报告、乐观汇总、丢失的文件、另一个镜像和不完整分母都不能授权发布。源码副本的证据不能添加镜像标签冒充镜像验收。

CI 常规运行只做离线验证。手动发布流程下载先前保存的同一镜像归档及外部验收包，在登录 registry 和推送前执行统一门禁。实际发布、远端推送、生产部署不属于本地验收动作。

负载报告分别记录业务类型延迟、排队和执行峰值、失败原因与排空时间。Run 终止不等于业务解决，也不等于回答正确。恢复历史提取记录明确标为 fixture-only，只证明持久性；模拟 S3 不代表真实云 IAM 或跨机恢复已验证。
