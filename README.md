# AI Helpdesk

一个面向企业场景的智能客服系统。它能识别用户问题、检索知识库、分配给合适的客服 Agent，并在答案不够可靠时自动转人工。

后端使用 FastAPI，前端是 React + TypeScript 控制台；内置工单、知识库、监控、追踪和回归评测能力。

## 项目介绍

这是一个面向生产环境设计的多 Agent 智能客服与工单编排系统。它把一条客服请求拆成“识别问题、补充上下文、检索知识、专家协作、安全校验、人工接手、质量回归”七个可独立治理的环节，并提供可视化控制台与完整的可观测性能力。

- **高可靠意图识别与低延迟路径**：将客服请求细分为 18 类意图，融合 LLM（70%）、向量（20%）和规则（10%）三路信号，输出意图、置信度、来源分数、紧急度及订单号、金额、物料编号等结构化实体。针对 9 类高频确定性问题，内置 53 个高精度锚点短语；命中后无需网络调用，并将单次工单的 LLM 往返从 4 次降至 1 次。
- **按需触发的混合检索 RAG**：仅在业务类意图下检索知识库。检索为双路并行——pgvector 余弦向量负责语义相近，tsvector 关键词（中文按字符二元组建索引，不依赖外部分词扩展）负责字面命中，再用 RRF 按名次融合。这一路补的正是稠密向量的系统性盲区：领域专名和订单号这类精确串。在内置语料的检索评测上，hit@3 从纯向量的 6/8 提升到 8/8（`scripts/eval_retrieval.py` 可复现）。融合发生在存储层、不花 LLM 调用，因此规则快通道也能享受到，零网络往返的承诺不受影响。完整链路在此之上再做查询改写 3 子查询并行召回、内容哈希去重、重排取 top-3；重排优先走 cross-encoder，未配置时回落 LLM。未配置 Postgres 时可回退 SQLite（稀疏侧为纯 Python BM25），便于零依赖本地开发。知识导入使用内容哈希去重，重复上传不会生成重复文档。
- **有反馈的检索回路**：重排之后不直接交付，先判定证据是否足以回答问题，不足则围绕缺口补检一轮（硬性上限一轮，仍不足则交给置信度闸门转人工）。充分性判定与 LLM 重排合并在同一次调用里返回，额外 LLM 往返数为零。
- **多 Agent 编排与人工兜底**：编排产品与安装、报价与账款、订单履约 3 个专家 Agent 和 1 个通用兜底 Agent。路由会综合意图、40 余个关键词锚点和实体信息选择主 Agent；跨领域问题满足阈值时由辅助 Agent 并行参与。意图不清先追问，任一置信度信号偏低则创建工单转人工。
- **把回答安全做成硬约束**：出站答案经过零 LLM 的规则守卫逐句检查。对没有知识库或 SOP 依据的价格、时效承诺进行拦截并改写为保守表述，同时记录结构化违规信息；流式 SSE 输出也使用同一套句级校验，避免流式与非流式结果标准不一致。
- **面向多租户的安全与记忆**：通过 HMAC 签名令牌在服务端推导用户身份，避免客户端伪造 `user_id` 读取他人记忆；Redis 提供按用户限流和 24 小时工作记忆，pgvector 保存情景记忆与用户画像，工单和对话审计日志可持久化回查。
- **完整工程化闭环**：Monitor 每 10 秒根据 Agent 成功率和延迟调整路由惩罚，并通过 Redis 在多实例间共享；LLM-as-Judge 从相关性、准确性、完整性、帮助性四个维度回放评测，并基于人工提升的基线识别回归。项目包含 216 个后端测试、13 个前端测试、11 服务 Docker Compose 环境，以及 Prometheus、Grafana、Tempo、Loki 和 OpenTelemetry 组成的指标、日志、链路追踪体系。

## 一次请求如何流转

![AI Helpdesk 完整请求链路](docs/diagrams/request-flow.svg)

## 技术架构

![AI Helpdesk 技术架构](docs/diagrams/tech-stack.svg)

## 快速开始

### 用 Docker 运行完整系统

准备 Docker 和 Docker Compose，然后在项目根目录执行：

```bash
cp .env.example .env
cp backend/.env.example backend/.env
# 编辑 backend/.env，填入 ANTHROPIC_API_KEY
docker compose up -d --build
```

启动后可访问：

| 地址 | 用途 |
| --- | --- |
| http://localhost:5174 | 客服控制台 |
| http://localhost:8000/docs | API 文档 |
| http://localhost:3000 | Grafana 监控、日志与链路追踪 |
| http://localhost:9090 | Prometheus 指标 |

### 本地开发

先按 `backend/.env.example` 创建并填写 `backend/.env`，再执行：

```bash
./dev.sh
```

这会同时启动后端和前端。浏览器打开 http://localhost:5173 即可。

## 常用能力入口

| 需求 | 入口 |
| --- | --- |
| 与客服对话 | 控制台，或 `POST /chat` |
| 流式对话 | `POST /chat/stream` |
| 查看知识库 | 控制台，或 `GET /knowledge/stats` |
| 导入知识 | `POST /knowledge/add`、`POST /knowledge/upload` |
| 查看人工工单 | `GET /tickets` |
| 查看服务状态 | `GET /health`、`GET /monitor` |
| 运行评测 | `POST /eval/run` |

完整接口说明可在启动后打开 http://localhost:8000/docs 查看。

## 核心组件

```text
backend/         FastAPI 服务、Agent 编排、知识库、记忆、工单与监控
frontend/        React 管理控制台
backend/skills/  不同客服 Agent 使用的服务规范（SOP）
```

系统使用 Redis 保存短期状态，如限流和近期对话；使用 PostgreSQL + pgvector 保存知识库、工单、审计记录和长期记忆。没有配置 Postgres 时，后端可退回 SQLite，方便本地体验。

## 测试

```bash
cd backend && pytest && ruff check .
cd frontend && npm test && npm run typecheck
```

当前：后端 **216 passed / 2 skipped**，前端 **13 passed**。
CI 分 3 阶段，其中一阶段带 pgvector service container，跑真实 Postgres 路径而不是 SQLite 兜底。

## 性能

压测脚本 `backend/scripts/load_test.py`，实测数据与企业落地线对照见
`backend/wiki/企业落地与产品定位.md` 的「量化指标 vs 企业落地线」一节。

```bash
cd backend && ./.venv/bin/python scripts/load_test.py --unique-messages --requests 100 --concurrency 10
```

**`--unique-messages` 不是可选项**：消息池只有 10 条，不加这个开关时脚本循环发送，
第 11 个请求起 `core/intent_recognizer.py` 的进程内意图缓存全命中，
后续请求直接跳过意图识别的 LLM 调用，p95 会比真实首次工单低约 11%。
缺开关时脚本会打印警告，报告里也会记 `mode` 字段。

## 技术栈

FastAPI · React · TypeScript · PostgreSQL + pgvector · Redis · Anthropic 兼容 API · OpenTelemetry · Prometheus · Grafana · Docker Compose
