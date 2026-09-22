<div align="center">

# AI Helpdesk

**多 Agent 智能客服与工单编排系统**

识别问题 · 检索证据 · 专家协作 · 人工接手

[English](README.md) · **简体中文**

![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)
![React](https://img.shields.io/badge/React-18-149ECA?style=flat-square&logo=react&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6?style=flat-square&logo=typescript&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-pgvector-4169E1?style=flat-square&logo=postgresql&logoColor=white)

[核心能力](#核心能力) · [系统架构](#系统架构) · [技术栈](#技术栈) · [快速开始](#快速开始) · [工程验证](#工程验证)

</div>

AI Helpdesk 将一条客服请求串成可追踪的处理链路：识别意图、读取记忆、检索知识、选择专家、校验答案，并在置信度不足时创建人工工单。React 控制台提供流式对话、路由检查、知识库管理、监控和评测入口。

内置业务知识与 SOP 以橱柜定制、安装、报价和订单履约为例，可通过知识库与 `backend/skills/` 替换业务内容。

## 项目预览

<!-- 截图准备好后：保存为 docs/images/main-interface.png，并将下方 src 替换为该路径。中英文 README 共用这一张图。 -->
<p align="center">
  <img src="docs/images/screenshot-placeholder.svg" alt="项目主界面截图占位区" width="100%" />
</p>
<p align="center"><sub>主界面截图预留位 · 建议展示对话区与路由检查面板</sub></p>

## 核心能力

| 能力 | 实现方式与工程价值 |
| --- | --- |
| **可解释的意图识别** | 19 类意图，融合 LLM、Embedding、规则三路信号（70% / 20% / 10%），输出置信度、实体和各路分数。规则锚点命中时跳过意图模型调用，减少高频问题的等待。 |
| **带反馈的混合检索** | pgvector 语义召回 + tsvector 关键词召回，RRF 按排名融合；完整链路再做查询改写、并行召回、去重和重排。证据不足时最多补检一轮，限制额外开销。 |
| **专家协作与降级** | 产品与安装、报价与账款、订单履约 3 个专家，加 1 个通用 Agent。按意图、实体和领域分路由；跨域问题可并行协作，失败有兜底，低置信度转人工。 |
| **流式回答守卫** | 对无知识库或 SOP 依据的价格、时效承诺做规则校验与改写；SSE 按句释放内容，使流式与普通回复共用同一套策略。 |
| **记忆与工单闭环** | Redis 保存短期会话，PostgreSQL 保存长期记忆、画像、工单和审计；HMAC 令牌推导用户身份，通知通过 Outbox 记录投递状态与重试。 |
| **运行反馈与质量回归** | Monitor 根据成功率和延迟调整实例路由权重；LLM-as-Judge 按四个维度评测，显式提升基线后才改变回归参照。 |

### 值得深入看的设计

- **两条执行路径**：规则快通道跳过意图 LLM 调用以及检索改写、重排；完整链路按需增加推理步骤。这里的“快”指减少调用，最终回答仍可能调用模型。[意图识别](backend/app/core/intent_recognizer.py)
- **召回与排序分工**：中文关键词使用字符二元组；RRF 融合不调用 LLM。重排优先使用配置的 cross-encoder，否则回退 LLM；充分性判断复用 LLM 重排结果。SQLite 模式提供 Python BM25 稀疏检索。[知识检索](backend/app/tools/knowledge_base.py) · [重排](backend/app/core/reranker.py)
- **业务规则可维护**：按 Agent 注入独立 `SKILL.md`，支持热加载；模型生成之后仍经过确定性的答案守卫。意图、路由、Agent 置信度分别设门槛。[编排器](backend/app/agents/agent_orchestrator.py) · [回答策略](backend/app/core/answer_policy.py) · [SOP](backend/skills/README.md)
- **下游故障有边界**：订单、库存、回款等实时数据可委派给独立的 `ai-sql-agent`，工具层统一管理超时与熔断；查询结果与知识检索共同进入回答上下文。[业务查询](backend/app/tools/sql_agent_client.py) · [工具治理](backend/app/tools/tool_manager.py)

## 系统架构

![AI Helpdesk 系统架构，由 Archify 生成](docs/diagrams/architecture.svg)

[交互架构图 HTML](docs/diagrams/architecture.html) · [可编辑 JSON](docs/diagrams/architecture.archify.json) · [详细架构](docs/architecture.md)

图由 [Archify](https://github.com/tt-a1i/archify) 生成。GitHub 直接展示静态 SVG；下载 HTML 后用浏览器打开，可缩放、查看节点、切换主题和导出。图中的接入、检索、编排与守卫是同一个 FastAPI 服务内的逻辑模块。

一条请求的主要路径：

```text
身份校验 → 读取记忆 → 意图识别 → 按需检索 / 业务查询
        → 专家路由与执行 → 答案守卫 → 置信度闸门
        → 返回答案或转人工 → 记忆与审计落库
```

## 技术栈

| 层次 | 技术 | 在项目中的职责 |
| --- | --- | --- |
| 前端 | React 18 · TypeScript 5 · Vite 6 | 控制台、组件与类型约束、开发代理和构建 |
| 界面与数据 | Tailwind CSS 4 · TanStack Query 5 · react-markdown | 样式、服务端状态缓存、Markdown 回复；`fetch + ReadableStream` 消费 POST SSE |
| API 与编排 | Python 3.12 · FastAPI · Pydantic 2 · asyncio | 请求校验、异步 I/O、自定义 Agent 路由与并行执行 |
| 模型接入 | Anthropic SDK · OpenAI SDK · HTTPX | 兼容两种模型协议；`LLM_PROVIDER` 切换；Embedding 与 Rerank 端点可独立配置 |
| 持久化与检索 | PostgreSQL 16 · pgvector · tsvector · asyncpg | 业务持久化、向量与关键词检索；本地持久化可回退 SQLite |
| 短期状态 | Redis 7 | 工作记忆、按用户限流、跨实例熔断与路由惩罚共享 |
| 可观测性 | OpenTelemetry · Prometheus · Grafana · Tempo · Loki | 指标、日志与链路追踪；当前日志采集器为 Promtail |
| 交付与测试 | Docker Compose · Nginx · GitHub Actions · pytest · Ruff · Vitest · Testing Library | 11 服务编排、反向代理、后端/前端/契约/镜像四类 CI 检查 |

依赖版本以 [后端依赖](backend/requirements.txt)、[前端依赖](frontend/package.json) 与 [Compose](docker-compose.yml) 为准。

## 快速开始

### Docker 本地体验

需要 Docker 与 Docker Compose。在仓库根目录执行：

```bash
git clone https://github.com/heyemlee/ai-helpdesk.git
cd ai-helpdesk
cp .env.example .env
cp backend/.env.example backend/.env
```

编辑 `backend/.env`：填写 `ANTHROPIC_API_KEY`，并将 **`APP_ENV` 改为 `development`**，用于本机体验。也可选择 `LLM_PROVIDER=openai`，再配置 `OPENAI_API_KEY`、`OPENAI_MODEL` 及需要的 `OPENAI_BASE_URL`。默认示例为生产模式，只填模型密钥无法通过启动校验。

```bash
docker compose up -d --build
```

| 地址 | 用途 |
| --- | --- |
| [localhost:5174](http://localhost:5174) | 客服控制台 |
| [localhost:8080/docs](http://localhost:8080/docs) | OpenAPI / Swagger |
| [localhost:3000](http://localhost:3000) | Grafana，凭据见根目录 `.env` |
| [localhost:9090](http://localhost:9090) | Prometheus |

<details>
<summary>本地开发：Python + Node.js</summary>

使用 Python 3.12、Node.js 24 和 Redis。完成上面的环境文件配置后，在仓库根目录执行：

```bash
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
(cd frontend && npm ci)
docker compose up -d redis
```

将 `backend/.env` 的 `REDIS_URL` 设为 `redis://:<根目录 REDIS_PASSWORD>@127.0.0.1:6379/0`；密码中的特殊字符需 URL 编码。未设置 `POSTGRES_DSN` 时使用 SQLite 持久化，**Redis 仍需运行**。本地评测可设置 `EVAL_BASELINE_PATH=./data/eval/baseline.json`。

```bash
scripts/dev.sh
```

访问 [localhost:5173](http://localhost:5173)。后端默认端口为 `8000`，可通过 `API_PORT` 调整，前端代理随之更新。

</details>

## 工程验证

在仓库根目录执行（后端虚拟环境已安装依赖，另需安装 `ruff`）：

```bash
(cd backend && .venv/bin/python -m pytest && .venv/bin/ruff check .)
(cd frontend && npm test && npm run typecheck && npm run build)
```

[CI](.github/workflows/ci.yml) 分别验证后端、前端、跨仓库 A2A 契约和 Docker 镜像。后端 CI 使用真实 pgvector 服务；契约任务拉取固定版本的 `a2a-contract` fixtures。测试数量随代码变化，以实际运行结果为准。

<details>
<summary>检索评测与压测</summary>

```bash
(cd backend && .venv/bin/python scripts/eval_retrieval.py)
(cd backend && .venv/bin/python scripts/load_test.py --unique-messages --requests 100 --concurrency 10)
```

检索脚本对比 dense / lexical / hybrid 的 MRR 与 hit@3；内置 8 条用例适合做小规模回归对照。压测保留 `--unique-messages`，避免重复问题命中意图缓存导致延迟偏低。LLM-as-Judge 可通过控制台或 `POST /eval/run` 运行，使用 `POST /eval/promote` 显式更新基线。

</details>

## 部署边界

当前定位为**单租户系统**：用户身份与记忆可隔离，下游业务数据权限仍取决于共享服务密钥，尚未实现按客户的行级权限隔离。`SQL_AGENT_IMPERSONATE` 或声明 `multi_tenant` 都不等于具备多租户授权能力。

生产环境需配置管理密钥、用户签名密钥、对话鉴权、令牌签发来源白名单和部署范围；缺项会被启动校验拒绝。本机体验配置不可直接用于公网部署。详细约束见[部署与安全边界](docs/deployment-boundaries.zh-CN.md)及[登录集成说明](docs/auth-token-integration.md)。

项目具备评测、观测与部署工具，尚不以真实生产流量证明容量或 SLA。

## 代码与文档

```text
backend/app/      API、意图识别、Agent、检索、记忆、工单与评测
backend/skills/   按 Agent 分隔、可热加载的业务 SOP
frontend/src/    对话、知识库、监控、评测与路由检查控制台
deploy/          Nginx 与指标、日志、追踪配置
scripts/         开发启动、部署、备份恢复与本地 CI
docs/            架构、代码导览、身份集成与运维记录
```

[架构详解](docs/architecture.md) · [代码导览](docs/code-guide.md) · [后端说明](backend/README.md) · [前端说明](frontend/README.md) · [架构图维护](docs/diagrams/README.md)
