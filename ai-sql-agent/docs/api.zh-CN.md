# 接口与项目结构

[English](api.md) | [简体中文](api.zh-CN.md)

## 接口一览

| 接口 | 用途 |
| --- | --- |
| `POST /query` | 自然语言查询，返回答案、分步 SQL / 结果、风险与追踪信息 |
| `POST /a2a` | A2A JSON-RPC：`message/send` 返回已完成的任务；`message/stream` 通过 SSE 推送阶段事件，最后给出与 `/query` 相同的字段（少一个 `reason`） |
| `GET /.well-known/agent.json` · `GET /a2a/databases` | A2A Agent Card，以及当前提供查询的业务库清单 |
| `GET /history` | 查询历史，默认仅自己的记录 |
| `GET /runs/{run_id}` · `GET /runs/{run_id}/events` | 一次请求的运行记录：状态、入口、用量和结论；以及它按序号排列的事件日志（阶段、模型调用、SQL 执行）。只能看自己的，管理员例外 |
| `POST /runs/{run_id}/cancel` | 停下一次还在跑的运行；已经结束回 `409`，不是自己的回 `404` |
| `GET /whoami` | 本次请求解析出的身份（与 `/query` 同一套解析）与服务能力 |
| `GET /catalog` | 当前身份可见的数据目录：数据域、表、字段、单位与业务术语 |
| `POST /catalog/{database}/{table}/rows` | 只读分页预览，支持搜索 / 筛选 / 排序；不接受 SQL |
| `POST /catalog/{database}/{table}/profile` | 只有聚合、没有记录：总行数、各字段填了多少、取值分布、按月的记录数；筛选条件与预览同义 |
| `POST /memory/save` | 保存个人长期记忆 |
| `POST /feedback` · `GET /feedback/stats` | 回答反馈与管理员统计 |
| `GET /health` · `GET /readyz` | 存活与就绪检查 |
| `GET /metrics` | Prometheus 格式指标 |
| `POST /admin/schema/reload` | 管理员重建 Schema 索引 |
| `/admin/model` · `/admin/model/embedding` | 管理员：测试、保存或清除页面设置里用的模型连接与 Embedding 连接 |

`POST /query` 响应包含 `answer`、`has_risk`、`degraded`、`trace_id` 及分步执行信息。

每次 `/query` 或 A2A 请求都是一次运行（run）。`/query` 在响应头 `X-Run-Id` 里带回运行号；A2A 的 taskId 就是运行号。事件日志按序号分页读取：`GET /runs/{run_id}/events?after=<seq>&limit=<n>`。

取消在下一个检查点生效：

- SQL 立即中断；
- 正在进行的那一次模型调用会等它返回（最长为模型超时），结果丢弃，之后不再发起新的调用；
- 审计照常写入，状态码记为 `499`，这次请求照样计入当日配额。

调用方断开连接后，这次运行在下一个检查点取消（A2A 契约 v1.13 §5.4）。这个行为默认开启；设 `AI_SQL_CANCEL_ON_DISCONNECT=false` 可以改回跑完为止。

在 A2A 上，调用方还连着的时候被取消，终态是 `canceled`：流式的终态帧和 `message/send` 返回的 Task 都带 `state: "canceled"`，`metadata.error` 形如 `cancelled: api`（契约 §4.4）。限流、配额，以及流式并发准入满了，都在流打开之前返回 HTTP `429`，JSON-RPC 错误码 `-32029`，带 `Retry-After` 头（契约 §3.2 / §5.5）。

目录接口沿用 `/query` 的调用方身份与表列权限。预览语句由服务端按「真实存在且可见的列」拼出，值全部走绑定参数，执行走生成 SQL 用的同一个只读执行器与授权关卡。取回的内容在数据库里就设了上限，不是取回来再截：长文本截到 2000 个字符，二进制字段只取大小。「数据浏览」页上的统计图靠表概况接口，规矩相同，而且只返回聚合结果；一个字段的分布不受它自己的筛选条件影响，所以选了一个取值之后还能换成另一个。

## 请求示例

用内置演示库：

```bash
curl -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"u1","session_id":"s1","query":"查询准时率低于 0.9 的供应商名称"}'
```

开启 `AI_SQL_AUTH_MODE=api_key` 后加上 `-H 'X-API-Key: …'`；Key 及其表列权限的配置见[部署指南](deployment.zh-CN.md#三权限配置)。

## 项目结构

```text
src/ai_sql_agent/
  api/ · catalog/ · a2a/ · mcp_server/   网页、HTTP API、数据目录与 Agent 接口
  pipeline/ · normalization/             意图路由、问题归一化与编排
  semantic/ · retrieval/                 业务语义、分层检索与 SchemaGraph
  indexing/ · memory/                    向量索引、会话与长期记忆
  planning/ · generation/                跨库计划与 SQL 生成
  execution/ · repair/                   受控执行与失败修复
  validation/ · feedback/                结果校验与反馈回路
  auth/ · audit/ · core/ · ops/          权限、审计、运行时与只读账号工具
  runs/                                  每次请求的运行记录、事件日志与取消
tests/ · benchmark/                      测试、评测集、基线与门禁
examples/ · tools/                       分模块示例与模拟 ERP 数据（sim_erp、simple_erp）
deploy/                                  Docker 镜像与 Compose 编排
docs/                                    深入文档、架构图与截图
```
