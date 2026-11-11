<div align="center">

# AI Helpdesk

**两个协作的 Agent 服务和它们之间的契约：多 Agent 客服服务、它通过 A2A 委派的 Text2SQL Agent，以及一份带版本、由机器校验的 A2A 契约，都在一个仓库里。**

[![Python](https://img.shields.io/badge/Python-3.12_%C2%B7_3.11-3776AB?style=flat-square&logo=python&logoColor=white)](backend/Dockerfile)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.119_%C2%B7_0.141-009688?style=flat-square&logo=fastapi&logoColor=white)](backend/requirements.txt)
[![React](https://img.shields.io/badge/React-18-149ECA?style=flat-square&logo=react&logoColor=white)](frontend/package.json)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16_%2B_pgvector-4169E1?style=flat-square&logo=postgresql&logoColor=white)](docker-compose.yml)
[![A2A](https://img.shields.io/badge/A2A_0.2-contract_v1.15-6E56CF?style=flat-square)](a2a-contract/README.zh-CN.md)
[![CI](https://img.shields.io/badge/CI-GitHub_Actions-2088FF?style=flat-square&logo=githubactions&logoColor=white)](https://github.com/heyemlee/ai-helpdesk/actions/workflows/ci.yml)

[English](README.md) · **简体中文**

[技术亮点](#技术亮点) · [系统架构](#系统架构) · [技术栈](#技术栈) · [运行](#运行) · [文档](#文档)

</div>

<p align="center">
  <img src="docs/screenshots/chat.webp" width="960" alt="在 AI Helpdesk 控制台里问两个问题。订单进度的问题经 A2A 委派给 SQL Agent，各个阶段实时出现在控制台里：判断问题类型、找相关的表、规划、在两个库上编写并执行查询、修正一条出错的查询。回复由程序按通过校验的行渲染出来：订单状态和发货日期、数据时点、需由员工审核的提示，以及可核对的依据。随后打开学习模式，说明这一轮的经过：加权的意图投票、选中的专家、委派及其校验、回答检查和是否转人工。接着问保修期，回复是知识库里已发布政策的原文摘录，带版本、适用范围和生效时间" />
</p>

<p align="center"><sub>虚构的公司、订单和文档，由模拟的 API 提供，播放有加速。</sub></p>

## 技术亮点

- **两个 Agent，一份契约。** 客服 Agent 把数据类问题通过 A2A 委派给独立的 Text2SQL Agent（JSON-RPC 2.0，每个阶段经 SSE 实时传回）。两者之间的契约（JSON Schema 加 golden fixture）就在同一个仓库里，两个 Agent 的测试读的是同一个提交上的同一份。
- **流水线里套一个有边界的循环。** LLM、Embedding 和规则对意图投票，规则选出工具并用 `asyncio.gather` 并发执行，再按领域分数路由给专家 Agent。专家发现资料还不够时，通过 function calling 自己再调同样的工具，次数有固定上限。
- **回复由已验证的证据渲染。** 模型决定查什么、要不要转人工；用户读到的文字由程序根据绑定到行的 SQL 证据和已发布的政策条文渲染出来。没有证据就不做业务断言，字段缺失或格式不对一律按不可信处理。
- **权限闸门后面的 Text2SQL。** SQL Agent 按数据源逐步规划，由模型写 SQL，在 SQLGlot AST 闸门上逐表逐列核对权限后只读执行，失败最多修复两次，并自查结果。它的发布门禁测得跨库准确率 94.7%，没有提示的错误答案占 0.9%。
- **带一轮自纠错的混合检索。** pgvector 和 `tsvector` 关键词索引按名次融合，cross-encoder 或 LLM 重排并判断依据够不够，不够就针对缺的部分再检索一轮。
- **人在回路里。** 置信度低，或者专家自己提出需要人工时，这一轮带着工单交给员工。一个 Case 把同一个问题跨多条消息串起来，每份草稿在证据复核之后由员工批准。员工用本地账号登录，按角色授权。
- **一轮对话不跟着 worker 一起死。** 排队模式下一轮对话是 Postgres 里的一个 run，经 Redis Streams 派给持有带栅栏租约的 worker，控制台断线后带 `Last-Event-ID` 续上流。在杀 worker、重启 Redis 的混沌演练里，340 个 run 全部跑完，没有出现第二张工单或第二个回答。
- **可观测，有门禁。** 一轮对话的每一步都是 Postgres 里一条有序事件，也可以改走 Kafka；另有 OpenTelemetry 链路和 Grafana 看板。每个 pull request 都要过五项必需的 CI 检查，共 3,000 多个测试。

## 系统架构

<p align="center">
  <img src="docs/architecture.svg" width="844" alt="系统架构：一个仓库里有两个 Agent 服务和它们之间的契约。员工控制台（React 18、Vite）把问题发给 backend/ 里的客服 Agent（FastAPI、Python 3.12、asyncio），客户经 Nginx 进来；回答通过 SSE 流回。客服 Agent 分五个阶段：1 Understand，意图投票和工作记忆；2 Gather，混合检索并通过 A2A 委派；3 Route，路由给专家 Agent；4 Answer，在有边界的工具循环里作答；5 Verify，用已验证的证据渲染回复，或者作为 Case 或工单交给员工复核。Gather 把数据类问题通过 A2A（JSON-RPC）委派给 ai-sql-agent/ 里的 SQL Agent（FastAPI、Python 3.11）：它用 BM25 和向量检索表结构，按数据源逐步规划，由模型写 SQL，在 SQLGlot AST 闸门检查后只读执行并修复；证据通过 SSE 传回。契约 v1.15（JSON Schema 和 golden fixture）由两边共同测试。下方是 PostgreSQL 16（pgvector、tsvector）、Redis 7（记忆和 run 队列）、可选的 Kafka（对话事件日志）、Anthropic 和 OpenAI 的模型 API，以及 SQL Agent 读取的业务数据。平台：Docker Compose、Kubernetes、KEDA、Nginx、OpenTelemetry、Grafana 和 GitHub Actions。光点按这个顺序跟着一个数据类问题走完全程" />
</p>

一个问题从员工控制台进来，或者由客户经 Nginx 进来，在客服 Agent 里走五个阶段。**Understand** 读取记忆并对意图投票。**Gather** 同时在 PostgreSQL 里做混合检索，并在数据类问题上通过 A2A 委派给 SQL Agent。**Route** 选出专家 Agent，**Answer** 让它们在有边界的工具循环里作答，**Verify** 用通过校验的证据渲染回复，或者把这一轮交给人。SQL Agent 跑自己的循环（检索、规划、写 SQL、闸门、执行、修复），并把每个阶段实时传回。图里蓝色是请求和数据，青色是 A2A 交互，橙色是模型调用，紫色是契约。

两个 Agent 保持为独立的服务：只有 SQL Agent 能接触业务数据、执行模型写的 SQL；两边各自锁定 Python 和框架版本；一条重查询拖不垮对话。

| 目录 | 是什么 | 文档 |
| --- | --- | --- |
| `backend/` · `frontend/` | 客服 Agent 和它的员工控制台 | [setup](docs/setup.md) |
| `ai-sql-agent/` | Text2SQL Agent，提供 A2A、MCP 和 HTTP 接口 | [README](ai-sql-agent/README.zh-CN.md) |
| `a2a-contract/` | 契约：协议文档、JSON Schema、golden fixture、自检 | [README](a2a-contract/README.zh-CN.md) |
| `deploy/` · `scripts/` | Compose、Kubernetes 和监控配置；CI、备份和发布脚本 | [setup](docs/setup.md) |

## 技术栈

| 层 | 技术 | 作用 |
| --- | --- | --- |
| Agent 运行时 | Python 3.12 · FastAPI · asyncio · Pydantic 2 | SSE 对话，工具和专家并发执行，模型的每个决策都是校验过的 schema |
| 模型调用 | Anthropic SDK · OpenAI SDK（Chat Completions、Responses） | 一套接口覆盖两家，运行时可切换，支持 function calling |
| Agent 协议 | A2A 0.2 · JSON-RPC 2.0 · SSE · Agent Card · JSON Schema 2020-12 | 委派、发现，以及两个 Agent 共同测试的契约 |
| Text2SQL Agent | Python 3.11 · FastAPI · SQLGlot · psycopg 3 · MCP | AST 权限闸门后面的只读 SQL，支持 PostgreSQL 和 SQLite |
| 检索 | PostgreSQL 16 · pgvector · `tsvector` · cross-encoder 重排 | 向量和关键词两路按名次融合 |
| 状态与事件 | Redis 7（Streams、Lua）· Apache Kafka 4.3（可选） | 工作记忆、run 队列和共享限流；对话事件日志 |
| 控制台 | React 18 · TypeScript · Vite 6 · TanStack Query 5 · Tailwind CSS 4 | 流式对话、Case、工单、知识库、数据和管理页面 |
| 可观测性 | OpenTelemetry · Prometheus · Grafana · Tempo · Loki | 链路、指标、日志和告警规则 |
| 交付 | Docker Compose · Nginx · Kubernetes · KEDA · GitHub Actions | 一次 Compose 部署 13 个服务，或用清单在 Kubernetes 上由 KEDA 伸缩 worker |
| 测试 | pytest · Ruff · Vitest · k6 | 单元和契约测试、压测和混沌演练 |

## 运行

```bash
cp .env.example .env                  # Compose 变量；填上演示管理员才能登录控制台
cp backend/.env.example backend/.env  # 设 APP_ENV=development 和模型 Key
docker compose up -d --build          # 整套服务；控制台在 http://localhost:5174
scripts/dev.sh                        # 或者从源码跑后端和控制台，在 http://localhost:5173
scripts/ci-local.sh                   # 在一次性副本里跑完所有 CI 作业
```

模型接入、排队模式、事件日志和从源码运行见 [docs/setup.md](docs/setup.md)。SQL Agent 用自己的 Python 3.11 环境（[本地开发](ai-sql-agent/README.zh-CN.md)）。

## 文档

- **组件：** [SQL Agent](ai-sql-agent/README.zh-CN.md) · [A2A 契约](a2a-contract/README.zh-CN.md) · [安装与 API](docs/setup.md)
- **设计：** [持久运行](docs/design/durable-agent-runs.md) · [部署边界](docs/deployment-boundaries.zh-CN.md)
- **测量记录：** [检索](docs/evaluation/retrieval-remote-embedding.md) · [工具循环 A/B](docs/evaluation/tool-loop-ab.md) · [混沌演练](docs/evaluation/chaos-drill.md) · [压测](docs/evaluation/load-test.md) · [SQL Agent 发布门禁](ai-sql-agent/README.zh-CN.md)
- **验收：** 用一套冻结题集给发布把关的[验收工具](acceptance/README.md)

## AI 辅助开发

这个仓库用 Claude Code 开发，外面有这几道约束：

- **`main` 分支保护。** 改动只能通过 pull request 进来，五项 CI 检查必须通过，管理员也不能绕过。
- **项目 skill。** [`test-backend`](.claude/skills/test-backend/SKILL.md) 在一次性副本里跑测试，真实的 `.env` 和数据库碰不到测试。
- **只读的 MCP server。** [`helpdesk-dev`](backend/scripts/dev_mcp_server.py) 让 agent 读开发库里记录下来的对话轮次，并拒绝连接生产库。
- **只提建议的 Claude 审查。** [claude-review.yml](.github/workflows/claude-review.yml) 在 pull request 上留评论，但不能批准或拦截。
