# A2A 接入导读

[返回首页](../README.md) · [权威契约](../a2a-streaming-contract.md) · [交互时序图](diagrams/a2a-sequence.zh-CN.html)

> 本文解释实现与配置，不替代权威契约。代码依据固定在 Helpdesk `9366654`、SQL Agent `1472e2c`，契约版本为 v1.12。

## 一次委派的边界

Helpdesk 把自然语言问题交给 `order_data_query` 工具。只有命中数据类意图，或在宽泛意图下识别到订单号、物料号等业务实体，才尝试委派。SQL Agent 收到的是问题与身份/会话信息；SQL 的检索、规划、生成和受控执行由 SQL Agent 自己负责。

这是客服服务到查询服务的一次定向委派。两个应用各自包含内部模块；图中的数据库节点代表可能涉及多个库的逻辑执行边界，不表示所有数据部署在同一台数据库上。

当前 Agent Card 声明的 `protocolVersion` 为 `0.2`，实现范围以本契约为准；`tasks/get`、`tasks/cancel`、`tasks/resubscribe` 不在当前支持范围内。

## 三条 transport

`SQL_AGENT_TRANSPORT=query` 是 Helpdesk 默认值，直接请求 `POST /query`。设置为 `a2a` 后：

1. 客户端发现 `GET /.well-known/agent.json`，缓存 30 秒；发现失败时按不支持流式处理。
2. `capabilities.streaming=true` 时，向 `POST /a2a` 发送 JSON-RPC 2.0 `message/stream`，接受 `text/event-stream`。
3. 不支持流式时走 `message/send`。如果 Card 缓存尚未过期、服务端开关已经关闭，开流前的 `-32004` 同样允许回退到 `send`。
4. 流已经打开后的失败直接失败，不再发一次 `send`，避免重复执行。运行时没有“任意失败都自动重试 `/query`”的链条；切回 `/query` 是配置回滚。

SQL Agent 的流式成功顺序：

```text
task
  → status-update(working) × N
  → artifact-update(text part + data part)
  → status-update(state=completed, final=true)
```

阶段数量不固定。只收到 artifact、连接正常关闭但缺少终态、`failed` 终态，都不能算协议成功。Helpdesk 将可展示的阶段映射为 `progress` SSE 事件；最终回复再经过自己的回答守卫与置信度判断。

**SSE 是进度通道，不等于 SQL Agent 逐 token 输出最终答案。** 图中的只读查询可以包含跨库步骤、错误修复与多条进度事件。Helpdesk 到浏览器的 SSE 和 SQL Agent 到 Helpdesk 的 SSE 是两段独立连接；前者有等待心跳，后者按契约不依赖心跳续命。

## 结果为什么不能直接相信

三种 transport 的结果归一后共用 `core.evidence.assess()`。关注三个层次：

| 层次 | 判断 | 例子 |
| --- | --- | --- |
| 结构 | 字段、类型与枚举是否符合 schema | 字符串 `"false"` 不能替代布尔值；v1.12 的 `execution_status` 必填 |
| 执行 | `success` / `partial_success` / `no_data` / `failed` / `not_applicable` | 零行查询成功，不能说成查询失败 |
| 证据 | 是否查库、是否有风险或降级、步骤与数据库标识是否合法 | 有一段自然语言 answer，并不代表有业务数据出处 |

保守判定包括：缺少 `queried_database` 视为未查库；缺少 `has_risk` 或 `degraded` 视为不可信。部分成功只能讨论成功步骤支撑的内容；结果截断时不能声称总数、全量、求和或比例。币种和数据时间也需要保留来源，不能用本地推测冒充数据事实。

JSON Schema 验证结构，不能独自完成全部业务信任判断；`trust-negative` fixtures 专门覆盖“结构合法、仍不能当出处”的情况。两侧在消费者测试中使用共享 schema；调用方运行时的归一与信任判断是独立的代码逻辑，不能把它描述为每次请求都调用 JSON Schema 校验器。

## 超时预算

| 位置 | 默认值 | 配置 |
| --- | --- | --- |
| Helpdesk 直接查询客户端 | 45 秒 | `SQL_AGENT_TIMEOUT_S` |
| Helpdesk 外层工具 | 上一项 + 2 秒，即 47 秒 | 由工具注册逻辑推导 |
| A2A `message/send` 客户端 | 45 秒 | `SQL_AGENT_A2A_SEND_TIMEOUT_S` |
| A2A 流式静默 | 20 秒 | `SQL_AGENT_A2A_SILENCE_S` |
| A2A 流式硬上限 | 90 秒 | `SQL_AGENT_A2A_HARD_S` |

首页示例将 `SQL_AGENT_TIMEOUT_S` 设为 90，使外层预算为 92 秒，覆盖流式硬上限。`send` 保持 45 秒；两者可独立配置。启动前置校验会强制外层预算大于 `send` 预算；**当前这条断言没有同时检查流式硬上限**，所以仍需显式配置外层覆盖流式预算。

这些是客户端与工具层预算，不代表模型一定能在预算内完成，也不能替代代理服务器的连接/读超时配置。长查询仍可能因 20 秒没有进度而失败。

## 身份与权限

默认下游 `user_id` 为 `SQL_AGENT_USER_ID`（默认 `ai-helpdesk`）。Helpdesk 用可信用户身份与本地会话 ID 的摘要生成下游会话 ID，避免用户通过猜测会话 ID 混入他人的记忆。

会话隔离不等于业务数据隔离。默认请求共用服务密钥的角色策略；`SQL_AGENT_IMPERSONATE` 只改变下游身份，不会自动重新解析最终用户权限，也不提供租户行级隔离。请遵循 Helpdesk 的单租户部署范围与 SQL Agent 的最小授权配置。

## 可复核的实现入口

下列链接固定到本次查阅的提交，避免后续 `main` 移动后无法解释图中的行为。

| 依据 | 入口 |
| --- | --- |
| 数据类意图门控、默认 transport、下游会话摘要、开流前回退 | [Helpdesk · sql_agent_client.py](https://github.com/heyemlee/ai-helpdesk/blob/9366654d90fed74d2fadfac4ea6b81845470e592/backend/app/tools/sql_agent_client.py) |
| Card 缓存、JSON-RPC 请求、SSE 消费与终态判断 | [Helpdesk · a2a_client.py](https://github.com/heyemlee/ai-helpdesk/blob/9366654d90fed74d2fadfac4ea6b81845470e592/backend/app/tools/a2a_client.py) |
| 跨 transport 的归一与信任判定 | [Helpdesk · evidence.py](https://github.com/heyemlee/ai-helpdesk/blob/9366654d90fed74d2fadfac4ea6b81845470e592/backend/app/core/evidence.py) |
| 外层超时推导与启动断言 | [Helpdesk · preflight.py](https://github.com/heyemlee/ai-helpdesk/blob/9366654d90fed74d2fadfac4ea6b81845470e592/backend/app/core/preflight.py) |
| 浏览器 SSE 转发、工具注册、结果注入 | [Helpdesk · api/main.py](https://github.com/heyemlee/ai-helpdesk/blob/9366654d90fed74d2fadfac4ea6b81845470e592/backend/app/api/main.py) |
| Card、`/a2a` 路由、Task / artifact / 终态与流式开关 | [SQL Agent · a2a/server.py](https://github.com/heyemlee/ai-sql-agent/blob/1472e2ce5571de91253f796f2e10442fce99dfd2/src/ai_sql_agent/a2a/server.py) |
| 结果摘要及执行状态映射 | [SQL Agent · result_summary.py](https://github.com/heyemlee/ai-sql-agent/blob/1472e2ce5571de91253f796f2e10442fce99dfd2/src/ai_sql_agent/api/result_summary.py) |
| 结构约束与共享样本 | [artifact schema](../schema/artifact-data-part.schema.json) · [fixture 清单](../fixtures/index.json) |

部分模块开头仍保留早期“尚未实现”的历史注释。本文依据当前函数分支与测试，不以旧注释作为当前实现状态。
