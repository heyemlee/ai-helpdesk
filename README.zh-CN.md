<div align="center">

# AI Helpdesk

**多 Agent 客服系统：按问题路由的专家 Agent、通过 A2A 协作的 SQL Agent、逐句检查的答案守卫，置信度不够时交给人。**

一个 FastAPI Agent 服务：每个问题先经过意图投票，再并发调用工具（带自纠错重试的混合知识检索，以及通过 A2A 协议委派给独立 SQL Agent 的数据查询），由专家 Agent 作答，问题跨领域时多个专家并行，每句话流出之前都要过检查。配套的 React 工作台实时展示每一步 Agent 决策。

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](backend/Dockerfile)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.119-009688?style=flat-square&logo=fastapi&logoColor=white)](backend/requirements.txt)
[![React](https://img.shields.io/badge/React-18-149ECA?style=flat-square&logo=react&logoColor=white)](frontend/package.json)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16_%2B_pgvector-4169E1?style=flat-square&logo=postgresql&logoColor=white)](docker-compose.yml)
[![CI](https://img.shields.io/badge/CI-GitHub_Actions-2088FF?style=flat-square&logo=githubactions&logoColor=white)](https://github.com/heyemlee/ai-helpdesk/actions/workflows/ci.yml)

[English](README.md) · **简体中文**

[技术亮点](#技术亮点) · [系统架构](#系统架构) · [技术栈](#技术栈) · [功能](#功能) · [本地开发](#本地开发)

</div>

<p align="center">
  <img src="docs/screenshots/chat.webp" width="960" alt="在 AI Helpdesk 工作台打开学习模式问三个问题：台面开裂的问题经过一轮补检后由两个专家 Agent 并行作答，一句没有依据的日期承诺被替换；订单进度交给 SQL Agent 查询，它找表、规划、查询和自修的每一步都实时显示在工作台上；客户要求找人时建了工单，通知已送达" />
</p>

<p align="center"><sub>虚构的公司、订单和文档，由模拟的 API 提供。</sub></p>

## 技术亮点

- **路由加并行专家。** LLM、Embedding 和规则按 0.7 / 0.2 / 0.1 对 19 个意图投票，用 gpt-6-luna 时 90 条标注消息对了 77 条。再按领域分数从三个专家 Agent 里选出主处理；第二个专家分数不低于 0.45、且不低于第一名的 55% 时并行加入，专家失败时由通用 Agent 兜底。
- **Agent 间委派。** 数据类问题交给独立的 SQL Agent，它自己规划、写 SQL、执行、出错自修，并通过 A2A 把每个阶段实时传回。另有 Agent Card 发现、退回 `message/send`、静默和总时长超时，以及钉在 `v1.12` 的契约测试在 CI 里跑。
- **Agent 之间默认不信任。** 委派结果只有在执行状态为 success 或 partial_success、且没有风险或降级标记时才算证据；字段缺失一律按不可信处理。价格和日期只能以证据、检索到的片段和 SOP 为依据。
- **带自纠错回路的检索。** 改写成三个子查询，同时查 pgvector 和关键词索引；LLM 重排时判断依据够不够，不够就针对缺的部分再检索一轮：108 道测试题里有 101 道把正确文档排进前 3。
- **护栏与人工接管。** 回答一句一句流出，纯规则的守卫在发送前替换掉没有依据的价格或日期承诺。置信度低时建工单；模型停机时停止调用，也不建工单。

## 系统架构

<p align="center">
  <img src="docs/architecture.svg" width="960" alt="员工工作台和客户端连到用 Docker Compose 部署的 Agent 服务，分员工实例和客户实例；路由对意图投票，工具并发执行（知识检索，以及通过 A2A 委派给独立 SQL Agent 的订单查询，SQL Agent 自己规划、写 SQL、执行和自修），专家 Agent 作答，跨领域时并行，答案守卫和置信度门槛决定回复还是建转人工工单；Redis 存工作记忆，PostgreSQL 加 pgvector 存知识、记忆、工单和审计记录，遥测数据进 Tempo、Prometheus、Loki 和 Grafana" />
</p>

问题从工作台进员工实例，或者经 80 端口的 Nginx 进客户实例。服务先从 Redis 读工作记忆，再运行路由；选中的工具并发执行，在 PostgreSQL 里做混合检索，在员工实例上还会通过 A2A 委派 [ai-sql-agent](https://github.com/heyemlee/ai-sql-agent) 查数据。专家 Agent 结合各自的 SOP 作答，守卫逐句检查后流回；需要人工时建工单，通知先写进 outbox 再投递到 webhook。

- **规则选工具，模型写回答。** 查不查知识库、查不查数据，在任何 Agent 运行之前就由投票得出的意图和实体决定，所以每一轮的模型调用次数可预期，两路查询也能同时进行。Agent 不自己调用工具；开放式的循环（规划 SQL、出错自修）放在 SQL Agent 里，由钉住版本的契约约束。
- **两个入口，同一个镜像。** 客户实例从不委派 SQL Agent，也不保留跨会话记忆：它的用户 ID 取自请求体，猜中一个员工 ID 就可能带进员工的历史。只有 80 端口的 Nginx 接受其他机器的连接。

## 技术栈

| 层         | 技术                                                                         | 作用                                                         |
| ---------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------ |
| Agent 服务 | Python 3.12 · FastAPI · Pydantic 2 · asyncio                                 | 路由、并发工具、并行专家、兜底与转人工、SSE 流式输出         |
| 模型       | Anthropic SDK · OpenAI SDK                                                   | 意图投票、查询改写、重排、专家作答、LLM-as-Judge；可在线切换 |
| Agent 协议 | 基于 JSON-RPC 的 A2A（`message/stream`、`message/send`）· Agent Card         | 委派给 SQL Agent，带实时进度，契约版本在 CI 里钉住           |
| 检索       | PostgreSQL 16 · pgvector · `tsvector` · BM25 · RRF                           | 知识片段的混合检索；本地兜底用 SQLite 加 BM25                |
| 记忆与状态 | Redis 7 · asyncpg                                                            | 工作记忆、会话摘要和用户画像；限流和共享的熔断状态           |
| 护栏       | 纯规则的答案策略 · 置信度门槛 · HMAC 用户令牌                                | 逐句检查、转人工判定、签名的用户身份                         |
| 工作台     | React 18 · TypeScript · Vite 6 · TanStack Query 5 · Tailwind CSS 4           | 带逐轮 Agent 讲解的对话、知识库、工单和模型设置，中英文界面  |
| 运维       | Docker Compose · Nginx · OpenTelemetry · Prometheus · Grafana · Tempo · Loki | 一次部署 12 个服务，带链路追踪、指标、日志和告警规则         |
| 质量       | pytest · Ruff · Vitest · Testing Library · GitHub Actions                    | CI 里跑单元、契约和镜像检查；另有检索评测和端到端评测        |

## 功能

### Agent 编排

一轮流式回答依次收到 `progress → meta → routing → delta → result`：工作台实时显示每个阶段，学习模式逐条讲解每个决策。

- **路由。** 命中规则锚点时不调模型就定下意图，并跳过改写和重排；锚点互相冲突时交给完整投票，问题不清楚时先追问。
- **协作与兜底。** 并行专家的回答合并在一起，各自标明主处理还是辅助处理。专家失败时由通用 Agent 接手，但流式输出到一半时例外：已经发出去的内容收不回来，这一轮如实报告没有答完。
- **记忆与技能。** 工作记忆在 Redis 里保留 24 小时，攒到 15 条时由模型压缩成摘要；以前会话的摘要和用户画像也会带回上下文。每个专家从各自的 `SKILL.md` 加载 SOP，不用重启就能重新加载。

| 用 gpt-6-luna 跑一次      | 结果                                                       |
| ------------------------- | ---------------------------------------------------------- |
| 90 条标注消息上的意图投票 | 对 77 条（85.6%），macro-F1 0.798                          |
| LLM 评审的对话轮次        | 完成的 15 轮里通过 14 轮；16 轮中有 1 轮被上游连接错误打断 |

评审模型就是被测模型，用例都是中文；详见[评测记录](docs/evaluation/end-to-end.md)。自带的意图、专家、SOP 和知识是一个制造商的客服场景；换到别的领域，除了换文档，还要在代码里改意图和专家。

### Agent 间委派

员工实例上的数据类问题交给 [ai-sql-agent](https://github.com/heyemlee/ai-sql-agent)，它用自己的发布门禁衡量准确率。

- **流式加降级。** Agent Card 缓存 30 秒；每个阶段（找表、规划、写 SQL、查询、自修）按界面语言显示在工作台上。流打开之前收到 `-32004` 就改用 `message/send`；静默超时或超过总时长就结束调用。
- **证据规则。** 除了状态和标记，部分成功时失败的那几步不能下结论，结果被截断时不能报总数。
- **一份契约。** 消费方测试跑 [a2a-contract](https://github.com/heyemlee/a2a-contract) 的 golden fixtures，钉在 `v1.12`；拿不到 fixture 时测试失败，而不是跳过。

### 检索

- **两路召回，按名次融合。** 稠密一路（pgvector）和关键词一路（`ts_rank_cd`，SQLite 上是按中文二字切分的 BM25）各取 10 个候选，按名次融合，而不是把量纲不同的分数硬加在一起。只有本地 n-gram 向量时，关键词结果在前，向量结果补位。
- **换模型不停服。** 换向量模型时，服务一边继续回答，一边把库里的向量全部重算，算完再切换；库里是别的模型生成的向量时，服务拒绝启动。

PostgreSQL 上按题型统计的前 3 命中数：

| 题型             |    题数 | 本地 n-gram 向量 | Qwen3-Embedding-0.6B |
| ---------------- | ------: | ---------------: | -------------------: |
| 用词贴近原文     |      35 |               31 |                   33 |
| 口语换说法       |      30 |               22 |                   28 |
| 型号与专有名词   |      11 |               11 |                   11 |
| 用英文问中文文档 |      22 |                7 |                   21 |
| 需要两篇文档     |       6 |                3 |                    4 |
| 新旧版本并存     |       4 |                3 |                    4 |
| **合计**         | **108** |           **77** |              **101** |

52 篇文档和 122 道题（其中 14 道没有答案）都是仿真的测试数据。即使用远端向量，6 道有新旧版本的题里仍有 4 道是旧版排在前面。记录：[本地向量](docs/evaluation/retrieval-ca-factory.md)、[远端向量](docs/evaluation/retrieval-remote-embedding.md)。

### 护栏与转人工

- **答案守卫。** 一句话同时有金额或时间、承诺动词、没有对冲词、也找不到出处，就会被替换成一句以报价或排期为准的话；员工能看到原句和拦下它的规则。
- **门槛与工单。** 意图 < 0.45、路由 < 0.35 或 Agent < 0.70 时转人工；这些门槛是经验信号，不是校准过的概率。工单有四个状态，通知经 outbox 投递并会重试。
- **故障时宁停不错。** Key 被拒后，同一份配置不再调用，直到有人解除；429 退避 5–60 秒，原因不明的 403 退避 30–600 秒，服务端给了 `retry-after` 就照办，最长 900 秒。

### 知识库、工单与管理

<p align="center">
  <img src="docs/screenshots/workspace.webp" width="960" alt="浏览一个知识分类，用一个问题检索知识库得到三个排好序的片段；打开一张工单并标为已分配；然后切换向量模型，进度条显示重算 312 个向量后完成切换" />
</p>

<p align="center"><sub>同一家虚构公司，同样是模拟的 API。</sub></p>

**知识库**

- 按固定分类浏览；给文档换分类不影响检索。
- **按问题检索**显示对话链路会检索到的片段和分数。
- 可以导入 `.txt`、`.md` 或 JSON 文档列表；相同的正文不会重复导入。

**工单**

- 按状态筛选；只提供后端状态机允许的流转。
- 每张工单都有转人工的原因、完整对话和通知的投递记录。
- 对话里的工单卡片链到工单，并跟着工单状态更新。

**管理与诊断**

- 模型连接测试通过才保存；Key 只能写入不能读出。
- LLM-as-Judge 评测，只有显式提升时才更新基线。
- 监控、SOP，以及能查看任一条回复的路由分数和策略拦截记录的检查器。

## 本地开发

```bash
cp .env.example .env                  # Compose 变量：数据库、Redis 和 Grafana 的密码
cp backend/.env.example backend/.env  # 设 APP_ENV=development，填一个模型服务的 Key
docker compose up -d --build          # 起全栈；工作台在 http://localhost:5174
scripts/dev.sh                        # 或从源码运行，工作台在 http://localhost:5173
scripts/dev-all.sh                    # 同上，并从相邻目录启动 ai-sql-agent
```

配置、模型服务、导入知识、不用 Docker 的本地环境和 API 一览见 [docs/setup.md](docs/setup.md)（英文）；完整的中文使用指南见 [backend/README.md](backend/README.md)。

## 测试

```bash
scripts/ci-local.sh                         # 在本机跑一遍 CI；--with-images 连镜像一起构建
cd backend && .venv/bin/python -m pytest    # 后端测试；设了 TEST_POSTGRES_DSN 才跑 pgvector 用例
.venv/bin/python scripts/eval_retrieval.py  # 检索的 hit@3 和 MRR，不调用模型
.venv/bin/python scripts/run_eval.py        # 端到端评测；会调用模型
cd ../frontend && npm test                  # 工作台测试
```

CI 跑 Ruff、连 pgvector 的 953 个后端用例、218 个工作台用例、生产构建、A2A 契约用例，以及两个 Docker 镜像的构建。端到端评测要花模型调用，所以只在手动触发时运行。

## 部署

```bash
docker compose up -d --build                    # 12 个服务；只有 80 端口的 Nginx 对外监听
scripts/docker-deploy.sh backup                 # 备份 Postgres、Redis、env 文件和 deploy/ 配置
scripts/docker-deploy.sh restore backups/<dir>  # 恢复并重启；恢复演练用时 17 秒
```

生产模式下，缺管理钥匙、用户签名密钥、对话保护或部署范围声明中的任何一项，服务都拒绝启动。系统按单租户设计，具体范围见[部署边界](docs/deployment-boundaries.zh-CN.md)。
