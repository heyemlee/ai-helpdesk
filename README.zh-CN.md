<div align="center">

# AI Helpdesk

**多 Agent 客服系统：按问题路由的专家 Agent、通过 A2A 委派的 SQL Agent、逐句检查的答案守卫，置信度不够时交给人。**

一个 FastAPI Agent 服务：路由对每个问题的意图投票，工具并发执行，专家 Agent 作答（问题跨领域时并行），缺资料时在有边界的工具循环里自己补查，每句话发出之前都要过检查。配套的 React 工作台实时展示每一步 Agent 决策。

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](backend/Dockerfile)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.119-009688?style=flat-square&logo=fastapi&logoColor=white)](backend/requirements.txt)
[![React](https://img.shields.io/badge/React-18-149ECA?style=flat-square&logo=react&logoColor=white)](frontend/package.json)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16_%2B_pgvector-4169E1?style=flat-square&logo=postgresql&logoColor=white)](docker-compose.yml)
[![CI](https://img.shields.io/badge/CI-GitHub_Actions-2088FF?style=flat-square&logo=githubactions&logoColor=white)](https://github.com/heyemlee/ai-helpdesk/actions/workflows/ci.yml)

[English](README.md) · **简体中文**

[技术亮点](#技术亮点) · [系统架构](#系统架构) · [技术栈](#技术栈) · [功能](#功能) · [本地开发](#本地开发)

</div>

<p align="center">
  <img src="docs/screenshots/chat.webp" width="960" alt="在 AI Helpdesk 工作台打开学习模式问三个问题：台面开裂的问题经过一轮补检后由两个专家 Agent 并行作答，一句没有依据的日期承诺被替换；订单进度通过 A2A 交给 SQL Agent 查询，它找表、规划、查询和自修的每一步都实时显示在工作台上；客户要求找人时建了工单，通知已送达" />
</p>

<p align="center"><sub>虚构的公司、订单和文档，由模拟的 API 提供。</sub></p>

## 技术亮点

- **路由与专家 Agent。** LLM、Embedding 和规则按 0.7 / 0.2 / 0.1 对 19 个意图投票，用 gpt-6-luna 时 90 条标注消息对了 77 条。再按领域分数从三个专家里选出主处理，每个专家跑多个副本，由监控按成功率和延迟调整权重；第二个专家分数不低于 0.45、且不低于第一名的 55% 时并行加入。
- **有边界的工具循环，A/B 过了才打开。** 预取的资料不够时，专家通过 function calling 自己再调同样的检索和查数，每轮最多 3 次模型调用、1 次委派。72 组成对 A/B 里，规则漏掉数据的问题有 26/30 轮拿到了数据（规则组一轮都没有），没有越权调用，也没有编造数字，所以员工实例上打开了。
- **Agent 间委派。** 数据类问题交给独立的 SQL Agent，它自己规划、写 SQL、执行、出错自修。走 A2A 时每个阶段都实时传回，另有 Agent Card 发现、降级、超时，以及钉在 `v1.12` 的契约测试。
- **Agent 之间默认不信任。** 委派结果只有在执行状态为 success 或 partial_success、且没有风险或降级标记时才算证据；字段缺失一律按不可信处理。价格和日期只能以证据、检索到的片段和 SOP 为依据。
- **带自纠错回路的检索。** 改写成三个子查询，同时查 pgvector 和关键词索引，用 cross-encoder 或 LLM 重排并判断依据够不够，不够就针对缺的部分再检索一轮。单看融合检索这一步，108 道测试题里有 101 道把正确文档排进前 3。
- **护栏与人工接管。** 每句话发出之前都要过检查，没有依据的价格或日期承诺会被替换。专家会声明是否需要人工，置信度低时建工单；模型停机时暂停调用，而不是建工单。

## 系统架构

<p align="center">
  <img src="docs/architecture.svg" width="960" alt="Agent 架构图：一个问题在 Agent 运行时里依次经过五个阶段，由 React 18 工作台发出，回答经 SSE 流回。1 Understand：意图投票，结果用 Pydantic 2 校验，工作记忆在 Redis 7。2 Gather：规则选中的工具用 asyncio.gather 并发执行，在 PostgreSQL 16 上用 pgvector 和 tsvector 检索，同时通过 A2A 委派独立的 ai-sql-agent（它有自己的循环）；依据不足时回头再检索一轮。3 Route：领域分选出专家，监控每 10 秒给副本重新加权。4 Answer：三个专家调用 Anthropic 或 OpenAI 的对话模型作答，失败时由通用 Agent 兜底；在有边界的工具循环里，专家还能通过 function calling 再检索或再查数据。5 Check：守卫加三道置信度门槛，不过就建工单交给人工。向量和重排来自 Embedding API，平台是 FastAPI、Python 3.12、httpx、Docker Compose、Nginx、OpenTelemetry 和 Grafana；光点按这个顺序依次流过每条连线" />
</p>

问题从工作台进员工实例，或者经 80 端口的 Nginx 进客户实例，然后依次经过五个阶段。**理解**（Understand）读取记忆并投票定意图；**收集**（Gather）同时运行规则为它选中的工具：在 PostgreSQL 里做混合检索，在员工实例上还会通过 A2A 或 HTTP `/query` 委派 [ai-sql-agent](https://github.com/heyemlee/ai-sql-agent) 查数据；**路由**（Route）给专家打分并选出副本，**作答**（Answer）由专家结合各自的 SOP 完成，员工实例上还能自己补查缺的资料，**检查**（Check）在发出前逐句把关，需要人工时建工单，通知先写进 outbox 再投递到 webhook。

- **规则先查，专家补缺。** 查不查知识库、查不查数据，在任何 Agent 运行之前就由投票得出的意图和实体决定，两路查询同时进行。打开工具循环后，专家读完资料还缺东西时，会在预算内自己再调同样的工具；关着时每一轮的模型调用次数可预期。开放式的循环（规划 SQL、出错自修）放在 SQL Agent 里，由钉住版本的契约约束。
- **两个入口，同一个镜像。** 客户实例从不委派、不开工具循环，也不保留跨会话记忆，因为它的用户 ID 取自请求体；只有 80 端口的 Nginx 接受其他机器的连接。

## 技术栈

| 层             | 技术                                                                   | 作用                                                                                                                                                            |
| -------------- | ---------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Agent 运行时   | Python 3.12 · FastAPI · asyncio                                        | FastAPI 用 SSE 提供对话；asyncio 用 `gather` 并发执行预取、并行专家和工具循环每一步的调用，把阻塞的存储调用移出事件循环，并把进度推进流里                       |
| 模型调用       | Anthropic SDK · OpenAI SDK · Pydantic 2                                | 两家供应商共用一套接口，可在线切换，function calling 两边互译；每个模型决策（意图投票、查询改写、重排结论、用户画像、评审打分）都是 Pydantic 结构，用之前先校验 |
| Agent 协议     | A2A JSON-RPC（`message/stream`、`message/send`）· Agent Card · httpx   | 委派给 SQL Agent，带实时阶段、降级和超时；默认传输是普通 HTTP `/query`                                                                                          |
| 向量与重排     | OpenAI 兼容 `/embeddings` · 本地 n-gram 向量 · cross-encoder `/rerank` | 知识库、情景记忆和意图投票共用一个向量化入口，没配 API 时用本地向量；重排优先 cross-encoder，LLM 兜底                                                           |
| 检索存储       | PostgreSQL 16 · pgvector · `tsvector` · asyncpg · SQLite               | 稠密和关键词两路按名次融合；本地兜底用 SQLite 加 BM25                                                                                                           |
| 记忆与共享状态 | Redis 7                                                                | 24 小时工作记忆及其压缩锁、按用户限流，以及多实例共享的熔断状态和路由扣分                                                                                       |
| 护栏与身份     | 纯规则的答案策略 · 置信度门槛 · HMAC 用户令牌                          | 逐句检查、转人工判定、签名的用户身份                                                                                                                            |
| 工作台         | React 18 · TypeScript · Vite 6 · TanStack Query 5 · Tailwind CSS 4     | 从 `fetch` 流里读 SSE（EventSource 不支持 POST）、流式 Markdown 和学习模式，中英文界面                                                                          |
| 可观测性       | OpenTelemetry · Prometheus · Grafana · Tempo · Loki                    | 每个请求的链路追踪、指标、日志和告警规则，另有监控的 Z-score 异常检测                                                                                           |
| 交付与质量     | Docker Compose · Nginx · GitHub Actions · pytest · Ruff · Vitest       | 一次部署 12 个服务；CI 连 pgvector 跑测试，并跑 A2A 契约用例和两个镜像的构建                                                                                    |

## 功能

### Agent 编排

一轮流式回答依次收到 `progress → meta → routing → delta → result`；工作台实时显示每个阶段，学习模式逐条讲解每个决策。

- **路由。** 命中规则锚点时不调模型就定下意图，并跳过改写和重排；锚点互相冲突时交给完整投票，问题不清楚时先追问。
- **副本与反馈。** 每类专家默认跑两个副本。监控跟踪成功率和延迟，做 Z-score 异常检测；成功率低于 90% 或变慢时给副本扣分，最多 0.9，把后面的请求引到更健康的副本。
- **协作与兜底。** 单个专家的回答逐句流出；并行专家的回答合并在一起，各自标明主处理还是辅助处理。专家失败时由通用 Agent 接手，流式输出到一半时例外，这一轮如实报告没有答完。
- **工具循环。** 设 `AI_HELPDESK_TOOL_LOOP=on` 后（这里的员工实例就是这样），专家可以通过 function calling 自己调 `knowledge_search` 和 `order_data_query`：每个专家每轮最多 3 次模型调用、1 次委派，身份由服务端注入，报价不许查库，和预取相同的查询直接复用，墙钟上限按最慢的工具推算，一次委派超时之后不再委派。失败作为工具结果交还模型；每次调用都作为进度流出，学习模式逐条列出结局和耗时。
- **一轮的事件日志。** 设 `AI_HELPDESK_EVENT_LOG=full` 后，每一轮按顺序记成事件追加进 Postgres：意图、预取、路由、按用途分的每次模型调用和 token、每次工具调用、守卫替换、转人工和最终回答。写入在回答路径之外批量进行，原文比结构先到期清掉，两个实例的记录互不可见。刷新之后工作台靠它恢复那一轮的检查器和学习模式，离线脚本拿真实流量重放路由和答案守卫。
- **专家自己声明转人工。** 每个专家在回答末尾加一个标记，说明是否需要人工，这个标记会从流里摘掉；声明需要人工时，它的置信度会降到 0.70 的门槛以下。
- **记忆与技能。** 工作记忆在 Redis 里保留 24 小时，攒到 15 条时压缩成摘要；以前会话的摘要和用户画像也会带回上下文。每个专家从各自的 `SKILL.md` 加载 SOP，不用重启就能重新加载。

| 用 gpt-6-luna 跑一次      | 结果                                                       |
| ------------------------- | ---------------------------------------------------------- |
| 90 条标注消息上的意图投票 | 对 77 条（85.6%），macro-F1 0.798                          |
| LLM 评审的对话轮次        | 完成的 15 轮里通过 14 轮；16 轮中有 1 轮被上游连接错误打断 |

评审模型就是被测模型，用例都是中文（[评测记录](docs/evaluation/end-to-end.md)）。自带的意图、专家、SOP 和知识是一个制造商的客服场景；换到别的领域，要在代码里改意图和专家。

| 工具循环 A/B，gpt-6-luna，72 组成对       | 规则组 | 循环组 |
| ----------------------------------------- | -----: | -----: |
| 规则漏掉的数据到了专家手里（B 类 30 轮）  |      0 |     26 |
| 规则已经够用时的多余调用（A、D 类 18 轮） |      — |      1 |
| 编造数字或照做注入（E、F 类）             |      0 |      0 |
| 转人工（72 轮）                           |     46 |     39 |
| 每轮输入 token 均值                       |  2,692 |  4,915 |
| LLM 评审分（A–C 类）                      |  0.899 |  0.896 |

这是两轮里的第二轮：第一轮有两道打开门槛没过，中间改了什么见[评测记录](docs/evaluation/tool-loop-ab.md)（英文）。订单数据来自契约夹具，所以延迟里没有真实委派的 17–40 秒。

### Agent 间委派

员工实例上的数据类问题交给 [ai-sql-agent](https://github.com/heyemlee/ai-sql-agent)，它用自己的发布门禁衡量准确率。`SQL_AGENT_TRANSPORT=a2a` 时通过 `message/stream` 实时传回各个阶段；默认值 `query` 调用它的同步 HTTP `/query`。

- **流式加降级。** Agent Card 缓存 30 秒；每个阶段（找表、规划、写 SQL、查询、自修）按界面语言显示在工作台上。流打开之前收到 `-32004` 就改用 `message/send`；静默超时或超过总时长就结束调用。
- **每种传输都用同一套证据规则。** A2A 和 `/query` 的结果过同一道检查；部分成功时失败的那几步不能下结论，结果被截断时不能报总数。
- **一份契约。** 消费方测试跑 [a2a-contract](https://github.com/heyemlee/a2a-contract) 的 golden fixtures，钉在 `v1.12`；拿不到 fixture 时测试失败，而不是跳过。

### 检索

- **两路召回，按名次融合。** 稠密（pgvector）和关键词（`ts_rank_cd`，SQLite 上是 BM25）两路各取 10 个候选，按名次融合，而不是把量纲不同的分数硬加在一起；只有本地 n-gram 向量时，关键词结果在前。
- **改写、重排、补检。** 复杂问题改写成三个子查询。配了 cross-encoder `/rerank` API 时由它排序，依据够不够看分数阈值；没配时由 LLM 重排，并在同一次调用里判断依据够不够。不够就针对缺的部分再检索一轮。
- **一个向量化入口。** 知识库、情景记忆和意图投票共用一个 OpenAI 兼容的 `/embeddings` API，没配时用本地 n-gram 向量。API 失败时不会拿另一个向量空间的本地向量顶替；换模型时一边继续回答，一边把向量全部重算。

PostgreSQL 上按题型统计的前 3 命中数，只测融合检索这一步（不含改写、重排和生成回答）：

| 题型             |    题数 | 本地 n-gram 向量 | Qwen3-Embedding-0.6B |
| ---------------- | ------: | ---------------: | -------------------: |
| 用词贴近原文     |      35 |               31 |                   33 |
| 口语换说法       |      30 |               22 |                   28 |
| 型号与专有名词   |      11 |               11 |                   11 |
| 用英文问中文文档 |      22 |                7 |                   21 |
| 需要两篇文档     |       6 |                3 |                    4 |
| 新旧版本并存     |       4 |                3 |                    4 |
| **合计**         | **108** |           **77** |              **101** |

52 篇文档和 122 道题（其中 14 道没有答案）都是仿真的测试数据；即使用远端向量，6 道有新旧版本的题里仍有 4 道是旧版排在前面。记录：[本地向量](docs/evaluation/retrieval-ca-factory.md)、[远端向量](docs/evaluation/retrieval-remote-embedding.md)。

### 护栏与转人工

- **答案守卫。** 一句话同时有金额或时间、承诺动词、没有对冲词、也找不到出处，就会被替换成一句以报价或排期为准的话；日期不论怎么写，出处里有同一天就算有依据。员工能看到原句和拦下它的规则。
- **门槛与工单。** 意图 < 0.45、路由 < 0.35 或 Agent < 0.70 时转人工；这些门槛是经验信号，不是校准过的概率。工单有四个状态，通知经 outbox 投递并会重试。
- **故障时宁停不错。** Key 被拒后，同一份配置不再调用，直到有人解除；429 退避 5–60 秒，原因不明的 403 退避 30–600 秒，服务端给了 `retry-after` 就照办，最长 900 秒。

### 知识库、工单与管理

<p align="center">
  <img src="docs/screenshots/workspace.webp" width="960" alt="浏览一个知识分类，用一个问题检索知识库得到三个排好序的片段；打开一张工单并标为已分配；然后切换向量模型，进度条显示重算 312 个向量后完成切换" />
</p>

<p align="center"><sub>同一家虚构公司，同样是模拟的 API。</sub></p>

**知识库**

- 按分类浏览；换分类不影响检索。
- **按问题检索**显示对话链路会检索到的片段和分数。
- 可以导入 `.txt`、`.md` 或 JSON；相同的正文不会重复导入。

**工单**

- 按状态筛选；只提供后端允许的流转。
- 每张工单都有转人工的原因、完整对话和通知记录。
- 对话里的工单卡片链到工单，并跟着工单状态更新。

**管理与诊断**

- 模型连接测试通过才保存；Key 只能写入不能读出。
- LLM-as-Judge 评测，只有显式提升时才更新基线。
- 监控、SOP，以及能查看任一条回复的路由分数和策略拦截记录的检查器。

## 本地开发

```bash
cp .env.example .env                  # Compose 变量：数据库、Redis 和 Grafana 的密码
cp backend/.env.example backend/.env  # 设 APP_ENV=development、模型 Key 和 SQL_AGENT_TRANSPORT=a2a
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
.venv/bin/python scripts/run_trajectory_eval.py  # 工具循环对规则组，40 条轨迹用例；加 --yes 才调用模型
cd ../frontend && npm test                  # 工作台测试
```

CI 跑 Ruff、连 pgvector 的 1,074 个后端用例、227 个工作台用例、生产构建、A2A 契约用例，以及两个 Docker 镜像的构建。端到端评测和轨迹评测要花模型调用，所以只在手动触发时运行。

## 部署

```bash
docker compose up -d --build                    # 12 个服务；只有 80 端口的 Nginx 对外监听
scripts/docker-deploy.sh backup                 # 备份 Postgres、Redis、env 文件和 deploy/ 配置
scripts/docker-deploy.sh restore backups/<dir>  # 恢复并重启；恢复演练用时 17 秒
```

生产模式下，缺管理钥匙、用户签名密钥、对话保护或部署范围声明中的任何一项，服务都拒绝启动。系统按单租户设计，具体范围见[部署边界](docs/deployment-boundaries.zh-CN.md)。
