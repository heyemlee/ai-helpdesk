# 关键设计

[English](design.md) | [简体中文](design.zh-CN.md)

| 工程问题 | 实现方式 | 源码入口 |
| --- | --- | --- |
| 业务问法与字段名不一致 | 用 YAML 管理业务描述、别名与指标口径，供检索、规划和口径解释共享 | [semantic](../src/ai_sql_agent/semantic) |
| Schema 太大，直接塞入 Prompt 成本高 | 库 → 表 → 字段分层召回，BM25 + 向量经 RRF 融合和精排；按相关性裁剪 SchemaGraph，优先保留关联键 | [retrieval](../src/ai_sql_agent/retrieval) · [预算裁剪](../src/ai_sql_agent/pipeline/schema_budget.py) |
| 跨库不能直接执行一条 JOIN | 每步绑定目标库，通过中间结果回填条件；检查步骤与目标库是否一致，阻止 SQL 引用其他库的表 | [pipeline](../src/ai_sql_agent/pipeline/text2sql_pipeline.py) |
| SQL 能执行，却算错了 | SchemaGraph 标注 JOIN 基数，生成时约束去重与主键投影；执行后检查空结果、基数与语义一致性 | [graph_builder](../src/ai_sql_agent/retrieval/graph_builder.py) · [validation](../src/ai_sql_agent/validation) |
| 模型生成了越权查询 | 在模型上下文中过滤无权 Schema；执行前用 SQLGlot 解析表、列与作用域，视图展开到基表再判，解析失败则拒绝；再叠加基于 SQLGlot AST 的只读守卫、SQLite 默认拒绝的 authorizer 与数据库只读连接 | [AST 分析](../src/ai_sql_agent/auth/sql_analysis.py) · [视图](../src/ai_sql_agent/auth/views.py) · [execution](../src/ai_sql_agent/execution) |
| 模型或 SQL 执行失败 | 错误定位与局部修复，默认最多 2 次；模型不可用时尝试重放该用户同题成功 SQL，重新授权并显式标记降级 | [repair](../src/ai_sql_agent/repair) · [降级逻辑](../src/ai_sql_agent/pipeline/text2sql_pipeline.py) |
| 线上问题难定位、难复现 | `trace_id` 关联 SQL、库表、耗时、修复和 Token 用量；用户反馈可导出评测用例，确认正确的查询可用作生成范例 | [audit](../src/ai_sql_agent/audit) · [feedback](../src/ai_sql_agent/feedback) |

执行默认限制为 **1,000 行、10 秒、每次结果 8 MiB**（SQLite 另限单个值 8 MiB），可通过 [PipelineConfig](../src/ai_sql_agent/pipeline/objects.py) 与 `AI_SQL_MAX_*` 环境变量调整，见部署指南的[执行上限](deployment.zh-CN.md#执行上限)。结果风险检查提供提示，不代表业务正确性保证。

## 深入阅读

[项目详解](walkthrough.zh-CN.md) · [部署与运维](deployment.zh-CN.md) · [授权模型](authz-model.md) · [最小权限配置](least-privilege-runbook.md) · [检索设计](modules/retrieval.md) · [执行器设计](modules/execution.md)
