# ai-helpdesk 完整使用指南

本文档说明 ai-helpdesk 的部署、启动、API 调用、知识库使用、Postgres/pgvector 数据查看、监控评测和常见排障。

ai-helpdesk 是一个企业级智能客服系统，核心链路为：

```text
用户请求
  -> FastAPI /chat
  -> MemoryManager 读取 Redis 工作记忆 + pgvector 情景记忆 + 用户画像
  -> IntentRecognizer 分到 18 类细粒度意图
       ├─ 规则通道命中（高精度锚点短语）→ 零网络调用直接返回
       └─ 否则三路融合：规则 10% + Embedding 20% + LLM 70% 加权投票
  -> AgentOrchestrator 按领域分打分
       ├─ 主 Agent：Technical / Billing / Order 三个专家中得分最高的
       ├─ 辅助 Agent：跨域工单里达到门槛的其他专家，与主 Agent 并行执行
       ├─ 意图落到 OTHER 且置信度不足 → 先澄清追问
       └─ 专家执行失败 → 降级到 GeneralAgent 重试
          （模型服务不可用时不降级、不转人工、不建工单：回一句"没有作答"，见 5.3）
  -> Agent 生成回复（注入对应 agent 类型的 Markdown SOP，支持热加载）
  -> AnswerPolicy 出站校验：拦截无依据的价格/时效承诺
  -> 综合置信度（意图 / 路由 / Agent 自评取最弱一环）低于阈值 → 转人工
  -> 写入 Redis，并异步更新 Postgres 用户画像
```

一次业务类 `/chat` 请求在首字响应前的 LLM 往返次数：

| 通道 | 意图识别 | 查询改写 | 结果重排 | Agent 回复 | 合计 |
|------|----------|----------|----------|------------|------|
| 三路融合 | 1 | 1 | 0-1 | 1 | **3-4** |
| 规则快通道 | 0 | 0 | 0 | 1 | **1** |

用户画像更新走后台任务，不计入首字延迟。规则通道的收益不只是省钱：
它把高频高确定性工单的首字延迟压到只剩一次 LLM 往返。

## 1. 项目结构

```text
ai-helpdesk/                             # 仓库根
├── docker-compose.yml                   # 全栈编排（12 个服务）：员工 / 客户两个后端实例、frontend、Redis、Postgres(pgvector)、观测栈、Nginx
├── .env.example                         # compose 变量替换的模板（REDIS_PASSWORD、AI_HELPDESK_PORT）
├── .env                                 # 由上一行复制而来，不入库
├── .github/workflows/ci.yml             # 后端 pytest + ruff，前端 vitest + tsc，契约用例，两个镜像构建
│
├── backend/                             # 本文档描述的部分
│   ├── app/                             # Python 包：服务本体（uvicorn app.api.main:app）
│   │   ├── api/main.py                  # FastAPI 入口，/chat /search /knowledge /monitor /eval
│   │   ├── api/model_admin.py           # /admin/model：模型连接的查看、测试、保存、恢复
│   │   ├── core/intent_recognizer.py    # 18 类意图：规则快通道 + 三路融合
│   │   ├── core/answer_policy.py        # 出站策略守卫：拦截无依据的价格/时效承诺
│   │   ├── core/skill_loader.py         # Markdown SOP 热加载，按 agent 类型注入
│   │   ├── core/llm_utils.py            # LLM 调用封装
│   │   ├── core/openai_compat.py        # OpenAI 兼容端点的翻译层（含丢掉 Schema 的中转站）
│   │   ├── core/embedder.py             # 文本 → 向量：远端 /embeddings 或本地 n-gram，两种不混用
│   │   ├── core/model_settings.py       # 控制台保存的模型连接：存哪、怎么校验
│   │   ├── core/model_runtime.py        # 运行中换连接：对话立刻换，向量先重建再换，两个实例保持一致
│   │   ├── agents/agent_orchestrator.py # 三专家 + 兜底的领域分路由、主辅协作、转人工
│   │   ├── agents/tool_loop.py          # 作答阶段的有边界工具循环（可选，默认关）
│   │   ├── events/                      # 一轮的事件日志：记录器、批量写入、存储、保留期、投影、重放
│   │   ├── memory/conversation_memory.py  # Redis + Postgres(pgvector) 记忆管理
│   │   ├── tools/tool_manager.py        # 工具调用、查询改写、重排、熔断、缓存、降级
│   │   ├── tools/knowledge_base.py      # Postgres(pgvector) RAG 知识库：混合检索、文档与分类
│   │   ├── storage/                     # pgvector / SQLite 存储实现、对话审计日志
│   │   ├── tickets/                     # 工单存储与转人工通知
│   │   ├── monitor/performance_monitor.py # Agent/工具在线监控
│   │   └── evaluation/evaluator.py      # 四维 LLM-as-Judge + 相对存档基线的回归检测
│   ├── skills/                          # 四份 SOP，与四个 Agent 一一对应（运行时热加载的数据，不是代码）
│   ├── tests/                           # pytest 用例（不依赖网络和外部服务）
│   ├── scripts/                         # 检索评测、端到端评测、轨迹评测、压测、向量重建（reembed.py）、
│   │                                    # 给老文档补分类（assign_categories.py）、事件日志运维和重放；用后端 venv 跑
│   ├── data/demo_docs/                  # 演示知识库文档，含 46 篇仿真工厂资料（检索评测语料）
│   ├── data/eval/baseline.json          # 评测存档基线，只在 /eval/promote 时更新
│   ├── data/eval/retrieval_cases.json   # 检索评测用例（122 条），和上面的仿真语料一起改
│   ├── data/runtime/                    # 控制台保存的模型连接（含明文 Key，不入库、不进镜像）
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── .env                             # 由 .env.example 复制而来
│   └── .env.customer                    # 客户实例专用，叠加在 .env 之上；由 .env.customer.example 复制而来，不入库
│
├── frontend/                            # React 18 + TypeScript 控制台
│   ├── src/components/RoutingInspector.tsx  # 把每一轮的路由决策过程摊开显示
│   ├── src/api/                         # client / hooks / types
│   ├── src/hooks/useChatStream.ts       # 按句流式接收
│   ├── Dockerfile
│   └── package.json
│
├── deploy/                              # 基础设施配置，由 compose 只读挂载；应用代码不读它
│   ├── nginx/nginx.conf
│   ├── prometheus/                      # prometheus.yml + alerts/
│   ├── grafana/                         # provisioning/ + dashboards/
│   └── loki/ tempo/ otel/ promtail/
│
├── scripts/                             # 仓库级脚本，从任何目录执行都成立
│   ├── dev.sh                           # 本地开发：一条命令同时起 uvicorn 和 vite
│   ├── docker-deploy.sh                 # 部署封装：compose 生命周期 + Redis/Postgres 备份恢复
│   └── ci-local.sh                      # 在本机跑一遍 CI 的全部门禁
│
└── docs/
    ├── architecture.md                  # Mermaid 架构图
    ├── code-guide.md                    # 代码导读：每个关键取舍在哪个文件、为什么
    ├── auth-token-integration.md
    ├── deployment-boundaries*.md        # 单租户边界、员工工作台与客户入口的分界
    ├── evaluation/                      # 实测记录：检索（冒烟 8 条、仿真工厂 122 条、远端 Embedding）、端到端、工具循环 A/B
    ├── integrations/                    # 交给兄弟仓库（a2a-contract、ai-sql-agent）的需求与反馈
    └── ops/                             # 上线检查单、恢复演练记录
```

### 命令在哪个目录执行

重组成 `backend/` + `frontend/` 之后，本文档里的命令分三类，执行位置不同：

| 命令 | 执行位置 | 例子 |
|------|----------|------|
| `docker compose *` | **仓库根** | `docker compose up -d --build` |
| `scripts/dev.sh` | **仓库根** | 同时起前后端 |
| `pytest` / `ruff` / `python -m app.api.main` | `backend/` | `cd backend && pytest` |
| `npm *` | `frontend/` | `cd frontend && npm run dev` |

后文出现 `docker compose` 时若未特别说明，一律指在仓库根执行。

`scripts/docker-deploy.sh` 是例外：它从脚本自身位置推导仓库根，**在任何目录执行都成立**。
内部所有 compose 调用都显式带 `-f <仓库根>/docker-compose.yml -p ai-helpdesk`，
不依赖工作目录，也不受仓库目录改名影响。

## 1.1 跑测试

测试全部用桩替换 LLM 调用，不发真实网络请求，不需要真的 Key、Redis 或 Postgres(pgvector)。
只有一点：`ANTHROPIC_API_KEY` 要有个占位值（CI 用 `test-key`），模型连接那组用例会读它，不设有 14 条失败：

```bash
pip install -r requirements.txt && ANTHROPIC_API_KEY=test-key pytest
```

覆盖的核心行为：意图分类体系与规则快通道、三路融合与 LLM 故障容错、
领域分路由与主辅协作、低置信度澄清与转人工、专家失败降级、
出站策略拦截、相对存档基线的回归检测，以及模型连接的测试 / 保存 / 跨实例跟随、
换向量模型时的整批重建、知识库分类。

PG 合同用例要 `TEST_POSTGRES_DSN`（一个装得上 vector 扩展的空库），A2A 契约用例要
`A2A_CONTRACT_DIR`（a2a-contract 的 `v1.12` 检出），没配就跳过。在不含 `.env`
的干净副本里实测（代码同 `4346f3e`）：都不配时 1344 通过、29 跳过（契约相关 19 条、PG 9 条，
另有 1 条要真的绑定 `0.0.0.0` 端口，在那次的沙箱里跳过）；配上一次性 pgvector 容器和契约
`v1.12`、不在沙箱里跑时 1466 通过、0 跳过（带 PG 时还会多收集一批 `[postgres]` 参数化用例）。

## 1.2 能力与代码位置对照

| 能力 | 实现位置 | 验证方式 |
|------|----------|----------|
| 18 类细粒度意图 | `app/core/intent_recognizer.py` `FINE_GRAINED_INTENTS` | `tests/test_intent_recognizer.py::test_exactly_18_fine_grained_intents` |
| 三路融合（规则 / Embedding / LLM） | `IntentRecognizer._vote`，权重 `_VOTE_WEIGHTS` | 响应里的 `intent_source_scores` 三项都非空 |
| 换供应商后 Embedding 一路不失效 | `_embed_texts` 远端优先、本地 n-gram 兜底，消息和模板始终在同一个向量空间里比 | `test_embedding_signal_stays_enabled_with_third_party_base_url`、`test_intent_never_compares_vectors_from_two_spaces` |
| 零延迟规则通道 | `IntentRecognizer._fast_path`，锚点表 `_FAST_PATH_RULES` | `test_fast_path_makes_zero_network_calls`；响应里 `intent_strategy=fast_path` |
| 快通道联动省掉查询改写和重排 | `app/tools/turn_tools.py::Prefetch.knowledge_context(fast_path=True)` | 对比两条通道的 `latency_ms` |
| 三个专家 Agent + 领域分路由 | `AgentOrchestrator._domain_scores` / `_route_decision` | `test_domain_score_picks_primary_agent` |
| 跨域工单提名主 + 辅 Agent | `RoutingDecision.supporting_agents`、`run_parallel` | `test_cross_domain_promotes_supporting_agent` |
| 低置信度澄清 | `AgentOrchestrator._needs_clarification` | `test_low_confidence_other_intent_asks_for_clarification` |
| 置信度转人工 | `_handoff_signals`，三路各自对齐阈值 | `test_low_intent_confidence_triggers_handoff` |
| 专家失败降级到通用 Agent | `AgentOrchestrator._execute` | `test_specialist_failure_falls_back_to_general` |
| 模型凭证不可用保护：被拒之后同一份配置不再出网；没答上来不算转人工 | `app/core/llm_guard.py`（套在 `create_llm_client` 上）；`AgentOrchestrator._unavailable_result` | `tests/test_llm_guard.py`、`tests/test_model_outage.py`（全部离线） |
| 按 Agent 类型注入的热加载 SOP | `app/core/skill_loader.py` + `skills/*/SKILL.md` | `POST /skills/reload` 后 `GET /skills` |
| 拦截无依据的价格/时效承诺（日期换了写法也按同一天认出处） | `app/core/answer_policy.py` | `tests/test_answer_policy.py`；响应里的 `policy_violations` |
| 作答阶段的有边界工具循环（可选，默认关） | `app/agents/tool_loop.py`（预算、去重、准入、墙钟上限）、`app/tools/turn_tools.py`（两个工具、报价策略门、预取委派超时后不再委派） | `tests/test_tool_loop.py`、`tests/test_agent_tool_loop.py`；响应里的 `trajectory` |
| OpenAI 兼容端点的工具调用 | `app/core/openai_compat.py`：`tools` / `tool_choice`、`tool_use` / `tool_result` 和 `tool_calls` 互译，流式分片按 index 拼 | `tests/test_openai_compat.py` |
| 一轮的事件日志：每个决定、每次模型和工具调用按顺序落库，原文和结构分开保留 | `app/events/`（记录器、批量写入、按实例隔离的存储、保留期、投影、重放）；模型调用在 `core/llm_guard.GuardedLLMClient` 那一层记 | `tests/test_turn_events.py`、`test_turn_projection.py`、`test_turn_model_calls.py`、`test_turn_replay.py`；`GET /turns/{turn_id}` |
| 轨迹评测：规则组对循环组 | `app/evaluation/trajectory.py`、`scripts/run_trajectory_eval.py`（订单数据读 a2a-contract 夹具） | `tests/test_trajectory_eval.py`；记录见 `docs/evaluation/tool-loop-ab.md` |
| 四维 LLM-as-Judge | `app/evaluation/evaluator.py::LLMJudge` | `test_judge_prompt_names_all_four_dimensions` |
| 相对存档基线的回归检测 | `EndToEndEvaluator._detect_regressions`，基线需显式提升 | `test_baseline_is_not_overwritten_on_every_run` |
| 本地向量时关键词优先、真模型时 RRF | `app/tools/knowledge_base.py::fuse_candidates`（`lexical_first_fuse` / `rrf_fuse`） | `test_local_vectors_do_not_push_the_keyword_winner_out_of_the_top_three`；`scripts/eval_retrieval.py` |
| 远端 Embedding 出故障不拿本地向量顶上 | `app/core/embedder.py`（抛 `EmbeddingUnavailable`，冷却后重试） | `test_remote_failure_raises_instead_of_substituting_local_vectors`、`test_search_falls_back_to_keywords_when_embedding_is_down` |
| 库里的向量不是当前模型算的就拒绝启动 | `storage_meta` 里的 `embedding_signature`（`check_embedding_signature`） | `test_startup_refuses_vectors_from_another_model` |
| 控制台换对话模型，不用重启 | `app/api/model_admin.py`、`ModelRuntime.apply_chat` | `test_saving_chat_applies_it_to_every_component_without_restart` |
| 换向量模型：先整批重建，再切换 | `ModelRuntime._switch`、`replace_embeddings` | `test_saving_embedding_rebuilds_vectors_then_switches`、`test_a_failed_switch_changes_nothing` |
| 客户实例跟随员工控制台保存的连接 | `ModelRuntime.sync` / `watch` | `test_the_other_instance_follows_chat_and_embedding` |
| 中转站丢掉结构化输出的 Schema 时照样可用 | `app/core/openai_compat.py` `_Messages.parse` | `test_schema_goes_into_the_prompt_when_the_gateway_drops_it` |
| 知识库分类（只管浏览，不影响检索） | `knowledge_base.CATEGORIES`、`set_document_category` | `test_documents_are_filed_and_moved_through_the_api`、`test_a_wrong_category_imports_nothing` |

## 2. 环境准备

### 2.1 必需依赖

- Docker
- Docker Compose
- Anthropic API Key，或兼容 Anthropic 协议的第三方 API Key

### 2.2 配置 `.env`

项目有**三份** env 文件，分工不同，配错地方会静默失效：

| 文件 | 作用 | 谁在读 |
|------|------|--------|
| `<仓库根>/.env` | `docker-compose.yml` 里 `${VAR}` 的替换值 | Docker Compose 自己 |
| `backend/.env` | 应用运行时配置（API Key、模型、Postgres DSN 等） | 两个实例的应用，经 `env_file` 注入 |
| `backend/.env.customer` | 客户实例专用，叠加在 `backend/.env` 之上；目前只放它自己的 `AI_HELPDESK_API_KEY` | 只有客户实例（`ai-helpdesk-customer`），经 `env_file` 注入 |

Compose 做变量替换时**只读 compose 文件同目录的 `.env`**，也就是仓库根那份。
把 `REDIS_PASSWORD` 写进 `backend/.env` 不会影响 Redis 容器的启动密码——
它会安静地回退到 compose 里的内置默认值 `aihelpdesk123`。

三份都复制一遍。`backend/.env.customer` 列在客户实例的 `env_file` 里，缺了它 compose 起不来；
里面那把钥匙保护客户实例的工单、对话、知识库和评测接口，用
`python -c "import secrets; print(secrets.token_hex(32))"` 生成：

```bash
cd <仓库根> && cp .env.example .env
cd backend  && cp .env.example .env && cp .env.customer.example .env.customer
```

`backend/.env` 最少需要配置：

```env
ANTHROPIC_API_KEY=your_api_key
```

#### 选用哪个模型

`LLM_PROVIDER` 决定走哪套协议，两组配置可以同时留在 `.env` 里，切换只改这一行：

```env
# 走 Anthropic 协议（默认）
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx
ANTHROPIC_BASE_URL=https://your-gateway.example.com          # 留空则走 Anthropic 官方端点
ANTHROPIC_MODEL=claude-sonnet-5
```

```env
# 走 OpenAI 兼容协议（/chat/completions）
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-xxxxxxxx
OPENAI_BASE_URL=https://your-gateway.example.com/v1          # 只写到主机时会自动补 /v1；带了路径的原样用
OPENAI_MODEL=gpt-5.6-luna
```

模型名以网关支持的清单为准（`GET $OPENAI_BASE_URL/models` 可查）。

业务代码只写了一套 Anthropic 形状的调用（`client.messages.create/stream`），
OpenAI 那条路由 `app/core/openai_compat.py` 翻译成 `/chat/completions`，system 位置、
`max_tokens`、`stop_reason`、`usage` 计量、流式增量和工具调用逐项对齐。所以换供应商是改
一行环境变量的事，五处调用点（编排器 / 意图识别 / 工具管理 / 记忆 / 评测）
一行都不用动，两条链路也不会因为改一处漏一处而产生行为差异。

兼容层顺带处理几种网关差异：effort 翻译成 `reasoning_effort`，模型不认就不再传；
`max_tokens` / `max_completion_tokens` 被拒时换名重试；有的中转站把结构化输出的
JSON Schema 丢掉、模型照常写一段文字，这时把 Schema 写进提示词、对回答逐条校验，
并记住这个端点和模型，之后直接走这条路（校验不过照样报错）；接口回了 200 却不是 JSON
（多半是地址少了 `/v1`，打到了网页上）时报 `GatewayResponseError`。回复本身的思考深度由
`AI_HELPDESK_ANSWER_EFFORT` 控制，不设就不传。

#### 在控制台切换模型连接（不重启）

「管理与诊断 → 模型连接」可以在运行中换对话模型和向量模型，对应接口是 `/admin/model`
（见 5.1）。每组是「当前模型 + 下拉框 + 保存」，接口地址和 Key 收在「连接设置」里；下拉框的候选
来自 `POST /admin/model/{chat,embedding}/models`（只问接口有哪些模型，不保存、不花 token），
读不到就手动输入。要点：

- 要设 `AI_HELPDESK_MODEL_SETTINGS_PATH`（compose 里是 `/app/data/runtime/model_settings.json`，
  宿主机目录 `backend/data/runtime/`）。没设时页面只读，只认 `.env`。
- 页面保存的那一组压过 `.env`，直到点「恢复为服务端配置」。文件里是明文 Key（权限 0600），
  没有任何接口把它读回去（状态里只有「配没配」和末四位）；`backend/.dockerignore` 排除了这个目录。
- 只接受本机打开的控制台发来的修改：`Host` 得是本机的名字，请求带了 `Origin` 的话它也得是本机；
  配了 `AI_HELPDESK_API_KEY` 时还要带 `X-API-Key`。Key 留空表示沿用现在那把，只在协议和主机都没换时允许。
- 对话模型：先发两条极短的真实请求（普通回复、结构化输出），测不通不保存（页面上可以「仍然保存」，
  接口是 `force: true`）；保存后编排器、Agent、意图识别、工具管理、记忆、评测立刻换用新客户端。
- 向量模型：先测一次拿到实际维度；保存后后台用新模型重算全部知识片段和情景记忆的向量，
  在存储层一个事务里整批换上，再切换并写配置。重算期间检索照常用旧模型，文档导入返回 409；
  换上之前失败，库和配置都不动；换上之后配置文件写不进去，向量已经是新模型的，文件可写后自动补写。
  恢复为 `.env` 的向量配置同样要重建（`.env` 配的正好是现在这个模型时不用，只删掉保存的那一组）。
- 面向客户的实例设了 `AI_HELPDESK_MODEL_SETTINGS_EDITABLE=false`，每隔
  `AI_HELPDESK_MODEL_SETTINGS_WATCH_S`（默认 5）秒看一次文件：对话直接跟上，向量要等库里
  `storage_meta` 记的向量来源已经是新模型才换。

停机时也可以用 `python scripts/reembed.py`（只打印计划）/ `--apply` 重建向量，见 7.6。

#### 让专家自己调工具（可选，默认关）

默认由规则在专家运行之前决定查不查知识库、查不查业务数据。打开工具循环后，专家读完这些资料还缺东西时，
可以通过模型的 function calling 自己再调 `knowledge_search`；接了业务数据的实例上，除通用 Agent 外还能调 `order_data_query`。
模型能填的只有查询词，身份由服务端注入；报价意图不许查库；和预取相同的查询直接复用；规则预取那次委派已经
超时或不可用时，模型不会再委派。

```env
AI_HELPDESK_TOOL_LOOP=on
# AI_HELPDESK_TOOL_LOOP_MAX_CALLS=3          # 每个专家每轮的模型调用次数，最后一次必须作答
# AI_HELPDESK_TOOL_LOOP_MAX_TOOL_CALLS=4     # 每轮的工具调用次数，复用本轮已有的查询不算
# AI_HELPDESK_TOOL_LOOP_MAX_DELEGATIONS=1    # 模型发起的 order_data_query 次数
# AI_HELPDESK_TOOL_LOOP_DEADLINE_S=          # 不设时按最慢工具推算：超时 + 15 秒作答 + 20 秒前导，至少 75 秒
# AI_HELPDESK_TOOL_LOOP_TOOLS=               # 按专家覆盖白名单，如 technical=knowledge_search（不让它查业务数据）
```

启动日志会写出实际用的墙钟上限；显式配得太小、查库塞不进去时会告警。模型网关不支持工具调用时，这一轮退回
单次生成，`/health` 的 `tool_loop.tools_rejected` 变成 `true`。打开之前可以先用
`scripts/run_trajectory_eval.py` 在 40 条轨迹用例上比较规则组和循环组（加 `--yes` 才调模型，先打印调用量估算）。

#### 记下每一轮的经过（可选，默认关）

每一轮从开始到结束发生的事按顺序记成事件，追加进 Postgres 的 `turn_events` 表：读记忆、意图投票、预取门控和
两路规则预取、路由、每次模型调用（意图、改写、重排、作答、摘要、画像，带用途和 token）、工具循环里的每次工具调用、
守卫替换、转人工和工单、最终回答，以及没答完、模型不可用、客户端中途断开。写入在回答路径之外攒批进行，写不进去
只丢日志、不影响回答。没有 Postgres 时存进程内最近 200 轮，重启就没了。

```env
AI_HELPDESK_EVENT_LOG=full                   # full：结构和原文；meta：只记结构；off：一条都不记（turn_id 照样返回）
AI_HELPDESK_INSTANCE=staff                   # staff 或 customer；日志打开而它没设时，日志不开
# AI_HELPDESK_EVENT_LOG_BODY_DAYS=30         # 多少天后清空原文；0 不清。compose 给客户实例配 14
# AI_HELPDESK_EVENT_LOG_DAYS=180             # 多少天后删除整行；0 不删。服务每天自己清一次
# AI_HELPDESK_EVENT_LOG_READ=on              # 读接口开不开；客户实例设 off
```

每条事件分成结构（`data`：枚举、数字、id、哈希）和原文（`body`）两半，原文先到期清空。读接口要员工钥匙、只读本实例的行：
`GET /turns/{turn_id}` 返回和当轮 `meta` / `routing` / `result` 一样形状的投影，工作台刷新后靠它恢复检查器和学习模式、
工单详情靠它打开"转人工那一轮的经过"。运维脚本 `scripts/turn_events.py`（`show` / `check` / `forget`）和重放脚本
`scripts/replay_turns.py`（`route` / `guard` / `export` 不调模型，`answer` 要 `--yes`）在本机跑，见
[docs/ops/turn-event-log.md](../docs/ops/turn-event-log.md)。

Docker Compose 场景下，Redis 和 Postgres(pgvector) 的连接由 `docker-compose.yml` 覆盖为容器内地址。通常不需要手动改：

```env
REDIS_URL=redis://localhost:6379/0
# POSTGRES_DSN=postgresql://aihelpdesk:aihelpdesk123@127.0.0.1:5433/aihelpdesk   # 不配则回退 SQLite
```

#### 上线前必须过的安全配置（生产模式会强制校验）

把 `APP_ENV` 设成 `production` 之后，下面五项**缺任何一项进程都拒绝启动**
（`app/core/preflight.py`，用例在 `tests/test_preflight.py`）：

| 变量 | 漏配的后果 |
|------|------------|
| `AI_HELPDESK_API_KEY` | 写操作/管理端点无鉴权。**更严重的是 `/auth/token`**：它的鉴权依赖就是这把 key，而请求体里的 `user_id` 由调用方指定——漏配时未认证请求可以为**任意用户**签发有效令牌，再拿它去读那个用户的记忆上下文 |
| `AI_HELPDESK_AUTH_SECRET` | `/chat` 的 `user_id` 取自请求体，改一个 id 就能读到别人的记忆 |
| `AI_HELPDESK_PROTECT_CHAT` | `/chat` 不鉴权，而它每次都真实调用 LLM |
| `AI_HELPDESK_TOKEN_ISSUER_CIDRS` | `/auth/token` 对任何直连对端开放。必须填网关网段（校验的是直连对端，不是 `X-Forwarded-For`）；`0.0.0.0/0` 这类通配同样拒绝启动 |
| `AI_HELPDESK_DEPLOYMENT_SCOPE` | 没人声明过这套部署是 `internal_single_tenant` 还是 `multi_tenant`。故意不给默认值；声明单租户却开着 `SQL_AGENT_IMPERSONATE=true` 也会被拒 |

为什么做成启动失败而不是运行时兜底：从前的写法是"未配置就放行"，
它把**运维漏配一个环境变量**翻译成了**这个端点不需要鉴权**。
这类缺陷没有任何运行时症状，只有出事那天才会被发现，所以判定点必须在部署那一刻。

本地联调不受影响：`APP_ENV` 不是 `production` 时这条校验不生效。
如果本地启动突然报"安全配置不完整"，是 `backend/.env` 里的 `APP_ENV=production`
（模板里的默认值）跟着复制过来了，改成 `development` 即可。

`/auth/token` 另外单独收紧了两层：

1. **永不放行**的管理鉴权：没配 `AI_HELPDESK_API_KEY` 时一律 503，不看 `APP_ENV`；
2. **来源白名单** `AI_HELPDESK_TOKEN_ISSUER_CIDRS`：生产模式下必须配，
   且不能是 `0.0.0.0/0`（允许一切等于没有约束，而配置文件看起来是有的），
   否则**拒绝启动**；每次请求校验**直连对端**，不看 `X-Forwarded-For`
   （那是个请求头，任何人都能写）。

"只暴露给网关"因此不再是一句约定——约定不会在配错的时候报错，断言会。

**但这两层都只是过渡形态。** 管理 key 证明的是"调用方是我们的基础设施"，
不是"这个用户登录了"：任何能调到这个端点的组件都能为任意 `user_id` 拿到令牌。
真正该签发令牌的是登录系统，而**本仓库里不存在那个组件**。
现状、风险边界与对接方案（谁签发 / `user_id` 取哪个 claim / 生命周期怎么对齐 /
迁移期怎么走）写在 [`docs/auth-token-integration.md`](../docs/auth-token-integration.md)。

#### 产品边界：单租户，业务数据没有按客户隔离

**部署前必须明确：本系统按单租户设计，不能对外表述为多租户隔离。**

隔离到位的是**记忆**：对话记忆、情景记忆、画像、限流都按服务端推导的可信
`user_id` 分隔；发给下游的会话标识也由这个身份派生
（`app/tools/sql_agent_client.py` 的 `_downstream_session_id`），
所以一个客户无法指定另一个客户的下游会话。

**没有隔离的是业务数据。** 订单/生产/库存/账款的答案来自 `ai-sql-agent`，
默认所有查询都以同一个服务身份发出（`SQL_AGENT_USER_ID`），
能查到什么由**那把 key 的角色策略**决定，不由提问的客户决定。
打开 `SQL_AGENT_IMPERSONATE` 是把可信的 `user_id` 传给下游——**传身份，不等于隔离了数据**。

"按客户隔离业务数据"是三件互不代表的事（只读核对过 `ai-sql-agent` 当时的代码，
没有重新联调；细节和依据见 [docs/deployment-boundaries.zh-CN.md](../docs/deployment-boundaries.zh-CN.md)）：

- **服务权限 ∩ 最终用户权限**：下游代码里已经有（按最终用户再解析策略并取交集，没声明的用户全拒绝），
  但要下游 key 标 `can_impersonate`、下游配置有 `users` 段、本系统开 `SQL_AGENT_IMPERSONATE`
  三者同时成立才生效。本系统这一侧没有启用，也没有复测——早先那次"两个客户身份、
  可见数据相同"的实测是在下游加上这一步之前做的。
- **客户身份接入**：没有。令牌里只有 `user_id`，没接登录系统，也没有客户到下游角色/租户的映射。
- **行级隔离**：没有启用。下游有行范围的模型和生成 RLS 的工具，但要在真实业务库（仅 PostgreSQL）
  上执行过才算数。

因此：

1. 下游那把 key 的策略按**能接触到本客服系统的最不受信任的访客**来配；
2. 不要把一个实例放在多个客户的数据前面并称之为隔离——那需要上面三项**同时**成立并复测过，
   完成其中一项不算；今天只有第一项存在于下游的代码里；
3. 首期只面向同权限内部人员是可以的，但这条边界要写进对外材料，不要含糊过去。

### 2.3 全栈部署和 run 开发模式的区别

ai-helpdesk 常用两种 Docker 启动方式：`docker compose up` 全栈部署，以及 `docker run` 开发模式。两者最大的区别是：**全栈部署会同时启动应用和依赖服务；run 开发模式通常只手动运行一个应用容器，依赖服务需要提前启动**。

| 对比项 | Docker Compose 全栈部署 | Docker run 开发模式 |
|--------|--------------------------|----------------------|
| 启动命令 | `docker compose up -d --build` | `docker run ... ai-helpdesk ...` |
| 启动内容 | ai-helpdesk、Redis、Postgres(pgvector)、Prometheus、Nginx | 只启动你指定的单个容器 |
| Redis/Postgres(pgvector) | 自动启动并加入同一网络 | 必须先执行 `docker compose up -d redis postgres` |
| 容器网络 | Compose 自动创建并管理 | 需要手动指定 `--network ai-helpdesk_ai-helpdesk-network` |
| 服务名解析 | 应用可直接访问 `redis`、`postgres` | 只有加入同一网络后才可访问 `redis`、`postgres` |
| 代码更新 | 通常需要 rebuild 或重启服务 | 挂载 `-v "$(pwd):/workspace"` 后，代码修改可直接生效，重启容器即可 |
| 适合场景 | 演示、联调、完整部署、HTTP API 服务 | 本地开发、调试 CLI、临时覆盖环境变量 |
| 常见问题 | API Key 或依赖健康检查失败 | 忘记启动 Redis/Postgres(pgvector)，导致 `redis:6379 Name or service not known` |

选择建议：

- 想完整体验 HTTP API、Swagger、Nginx、Prometheus：用 **Docker Compose 全栈部署**。
- 想调试源码或 CLI，并且希望本地改代码后快速重跑：用 **Docker run 开发模式**。
- 如果只是跑 CLI，最省心的方式是 `docker compose run --rm ai-helpdesk python -m app.api.main --cli`，它会自动使用 Compose 网络。

## 2.5 前端和后端一起启动

前端（React 18 + Vite）和后端（FastAPI）可以一起跑，三种方式按场景选：

### 方式 A：一条 compose 起全栈（推荐用于演示）

前端已经作为 `frontend` 服务并入 `docker-compose.yml`（仓库根），和 Redis / Postgres(pgvector) /
Prometheus / 后端在同一个网络里：

```bash
docker compose up -d --build   # 在仓库根执行
```

| 地址 | 内容 |
|------|------|
| http://localhost:5174 | 前端控制台 |
| http://localhost:8080 | 员工实例的后端 API（Swagger 在 /docs；宿主机端口由 `.env` 的 `AI_HELPDESK_PORT` 决定，默认 8080） |
| http://localhost:8081 | 客户实例的后端 API（宿主机端口由 `AI_HELPDESK_CUSTOMER_PORT` 决定；只读模型设置、不接 SQL Agent、关跨会话记忆） |
| http://localhost:80 | Nginx 反代，落在客户实例上 |
| http://localhost:3000 | Grafana（指标、日志、链路） |
| http://localhost:9090 | Prometheus |

前端容器里的 Nginx 把 `/api/` 反代到 `ai-helpdesk:8000`，走 Docker 内网，
不依赖 `host.docker.internal`，也不会因为宿主机端口占用而失效。
后端地址由 `API_UPSTREAM` 环境变量注入，改 compose 就能指到别处。

前端镜像是多阶段构建（`node:24-alpine` 里跑 `npm run build`，产物拷进 `nginx:1.27-alpine`），
**不需要**先在本机执行 `npm run build`。

### 方式 B：本地一条命令起前后端（推荐用于开发）

仓库根目录的 `dev.sh` 会同时拉起 uvicorn 和 vite，Ctrl-C 一起收掉：

```bash
scripts/dev.sh
```

打开 http://localhost:5173 即可，Vite 的 proxy 会把 `/api` 转发到后端端口（`backend/.env` 的 `API_PORT`，按 `.env.example` 默认是 8080）。
脚本会先检查解释器是否装齐依赖，没装齐会直接打印修复命令。
Redis 和 Postgres(pgvector) 仍需单独起：`docker compose up -d redis postgres`。

### 方式 C：单进程单端口（后端直接托管前端）

FastAPI 启动时会自动挂载前端构建产物，一个进程同时提供页面和 API：

```bash
cd frontend && npm run build
cd ../backend && python -m uvicorn app.api.main:app --port 8080
```

然后 http://localhost:8080 既是页面也是 API。查找顺序：
`AI_HELPDESK_FRONTEND_DIST` 环境变量 → `backend/static/` → `../frontend/dist/`。
三个都找不到就只提供 API，不影响纯后端部署。

静态资源挂载在所有 API 路由注册**之后**，因此 `/chat`、`/search`、`/docs` 等接口
始终优先匹配，只有未命中的路径才会回落到前端页面。

## 3. Docker Compose 全栈部署

推荐使用此方式启动完整服务。

```bash
docker compose up -d --build
```

查看服务状态：

```bash
docker compose ps
```

查看应用日志：

```bash
docker compose logs -f ai-helpdesk
```

看到 ai-helpdesk 启动日志并且健康检查通过后，服务可用。

启动后的端口（除 Nginx 外，宿主机端口都只绑 `127.0.0.1`，同一网络里的其他设备访问不到）：

| 服务 | 容器名 | 宿主机端口 | 容器内端口 | 用途 |
|------|--------|------------|------------|------|
| ai-helpdesk 员工实例 | `ai-helpdesk-app` | `8080`（`AI_HELPDESK_PORT`） | `8000` | 员工用的 API：接 SQL Agent、开跨会话记忆、没有 API key |
| ai-helpdesk 客户实例 | `ai-helpdesk-customer` | `8081`（`AI_HELPDESK_CUSTOMER_PORT`） | `8000` | 面向客户的 API：只读模型设置、不接 SQL Agent、关跨会话记忆；在 `backend/.env.customer` 里配了 `AI_HELPDESK_API_KEY` 后，工单/对话/知识库等端点要 `X-API-Key` |
| 前端控制台 | `ai-helpdesk-frontend` | `5174` | `80` | Vite 构建 + Nginx，`/api/` 反代到员工实例 |
| Nginx | `ai-helpdesk-nginx` | `80`（对外入口） | `80` | 反向代理到**客户实例**，不是员工实例 |
| Postgres(pgvector) | `ai-helpdesk-postgres` | `5433` | `5432` | 工单、审计、知识库、记忆、画像、事件日志都在这里（见第 8 节）；5432 被同机另一个 compose 项目占用，所以宿主机侧用 5433 |
| Redis | `ai-helpdesk-redis` | `6379` | `6379` | 工作记忆、限流、熔断共享 |
| Prometheus | `ai-helpdesk-prometheus` | `9090` | `9090` | 监控数据 |
| Grafana | `ai-helpdesk-grafana` | `3000` | `3000` | 指标、日志、trace 统一面板 |
| OTel Collector | `ai-helpdesk-otel` | `4318` | `4318` | OTLP HTTP，后端上报 trace |
| Tempo / Loki / Promtail | `ai-helpdesk-tempo` / `-loki` / `-promtail` | 无 | — | trace 存储 / 日志存储 / 日志采集，只在内网 |

宿主机的 `8000` 没有占用给这个项目：它留给同机的 ai-sql-agent，容器内的 `8000` 才是应用端口。
应用容器之间一律走 Docker 内网（`postgres:5432`、`redis:6379`），不经过上表的宿主机端口。

健康检查：

```bash
curl http://localhost:8080/health   # 员工实例
curl http://localhost:8081/health   # 客户实例
```

Swagger 文档：

```text
http://localhost:8080/docs
```

也可以通过 Nginx 访问（落在客户实例上）：

```bash
curl http://localhost/health
```

## 4. Docker Run 开发模式

开发时可以只用 Compose 启动依赖，然后用 `docker run` 挂载当前代码目录。

先启动 Redis 和 Postgres(pgvector)：

```bash
docker compose up -d redis postgres
```

构建镜像：

```bash
docker compose build --no-cache ai-helpdesk
```

启动 HTTP 服务：

```bash
docker run -it --rm \
  --network ai-helpdesk_ai-helpdesk-network \
  -p 8080:8000 \
  -e LLM_PROVIDER="anthropic" \
  -e ANTHROPIC_BASE_URL="https://your-gateway.example.com" \
  -e ANTHROPIC_API_KEY="your_key" \
  -e ANTHROPIC_MODEL="claude-sonnet-5" \
  -e REDIS_URL="redis://:aihelpdesk123@redis:6379/0" \
  -e POSTGRES_DSN="postgresql://aihelpdesk:aihelpdesk123@postgres:5432/aihelpdesk" \
  -v "$(pwd):/workspace" \
  -w /workspace \
  ai-helpdesk
```

CLI 交互模式：

```bash
docker run -it --rm \
  --network ai-helpdesk_ai-helpdesk-network \
  -e LLM_PROVIDER="anthropic" \
  -e ANTHROPIC_BASE_URL="https://your-gateway.example.com" \
  -e ANTHROPIC_API_KEY="your_key" \
  -e ANTHROPIC_MODEL="claude-sonnet-5" \
  -e REDIS_URL="redis://:aihelpdesk123@redis:6379/0" \
  -e POSTGRES_DSN="postgresql://aihelpdesk:aihelpdesk123@postgres:5432/aihelpdesk" \
  -v "$(pwd):/workspace" \
  -w /workspace \
  ai-helpdesk \
  python -m app.api.main --cli
```

## 5. Swagger 和接口总览

ai-helpdesk 基于 FastAPI 构建，启动 HTTP 服务后可以直接在浏览器访问 Swagger UI 调用接口。

本地 Swagger 地址（下文示例统一用 8080：`backend/.env.example` 的 `API_PORT` 和
compose 的 `AI_HELPDESK_PORT` 默认都是 8080，8000 留给同机的 ai-sql-agent；
自己改了端口就相应替换）：

```text
http://localhost:8080/docs
```

80 端口的 Nginx 是客户入口，只放行 `/chat`、`/chat/stream` 和 `/health`，经它打开 `/docs` 是 404。
客户实例自己的 Swagger 在本机：

```text
http://localhost:8081/docs
```

打开 Swagger 后，可以点击任意接口右侧的 **Try it out**，填写参数后点 **Execute** 直接调用本地服务。常用调试顺序：

```text
1. GET /health                确认服务是否就绪
2. POST /chat                 测试主对话链路
3. GET /knowledge/stats       查看知识库是否已有数据
4. POST /knowledge/upload     上传演示知识库文件
5. POST /search               测试知识库检索、查询改写和重排
6. GET /monitor               查看 Agent 和工具运行指标
7. GET /skills                查看已加载 Skills
8. POST /skills/reload        重新加载 Skills
9. POST /eval/run             运行端到端评测
```

### 5.1 接口总览

| 方法 | 路径 | 参数位置 | 作用 | 适合场景 |
|------|------|----------|------|----------|
| `GET` | `/health` | 无 | 健康检查，返回服务状态和 Agent 统计 | 启动后确认服务可用 |
| `POST` | `/chat` | JSON Body | 主对话接口，完成记忆读取、意图识别、Agent 路由、回复生成、记忆写入 | 业务主链路 |
| `GET` | `/monitor` | 无 | 查看 Agent/工具统计、告警和优化建议 | 观察在线表现 |
| `POST` | `/search` | JSON Body | 执行知识库检索优化链路：查询改写、并行召回、合并去重、LLM 重排；命中片段带 `doc_id` / `doc_title`。配了 `AI_HELPDESK_API_KEY` 时需要 `X-API-Key` | 测试 RAG 检索 |
| `GET` | `/skills` | 无 | 查看当前加载的 Skills、匹配关键词和解析错误 | 确认动态能力是否生效 |
| `GET` | `/sops` | 无 | SOP（即加载的 Skills）全文，只读；需要 `X-API-Key` | 工作台的 SOP 只读入口 |
| `POST` | `/skills/reload` | 无 | 运行时重新扫描 Skill 目录；同时解除模型凭证不可用保护 | 修改业务规则后热加载；凭证或额度修好后不重启恢复 |
| `POST` | `/knowledge/add` | JSON Body | 批量导入文档到 Postgres(pgvector) 知识库；每篇可带 `category` | 程序化导入文档 |
| `POST` | `/knowledge/upload` | Form File（+ `category`） | 上传 `.txt`、`.md`、`.json` 文件导入知识库；表单里的 `category` 是这次导入的分类 | 手动上传知识库文件 |
| `GET` | `/knowledge/stats` | 无 | 文档数、片段数，以及其中由历史分片归档的文档数 | 确认知识库是否有数据 |
| `GET` | `/knowledge/documents` | Query `q` / `category` / `limit` / `offset` | 文档列表；`q` 按空白拆词，每个词都要出现在标题或正文里；`category` 只看一类（`uncategorized` 是未分类）；响应里的 `categories` 是每类满足 `q` 的篇数。需要 `X-API-Key` | 浏览知识库里有什么 |
| `GET` | `/knowledge/documents/{doc_id}` | Path | 单篇文档的正文、来源、导入时间、分类和检索片段。需要 `X-API-Key` | 阅读原文、核对回答依据 |
| `GET` | `/knowledge/categories` | 无 | 固定的 7 个分类（按客户从咨询到售后的顺序）、每类篇数和未分类篇数。需要 `X-API-Key` | 导入时选分类、看各类有多少 |
| `PATCH` | `/knowledge/documents/{doc_id}` | JSON `{"category": "..."}` | 改一篇文档的分类（空串是未分类）。只改分类，分片、向量和检索不受影响。需要 `X-API-Key` | 整理知识库 |
| `GET` | `/admin/model` | 无 | 对话和向量两组连接的现状（不含 Key）、能不能改（`editable`，不能时 `reason` 说明原因）、向量切换进度（`switch`）。需要 `X-API-Key` | 控制台「模型连接」页 |
| `POST` | `/admin/model/chat/models` · `/admin/model/embedding/models` | JSON Body | 这个连接提供哪些模型，给页面的下拉框；不保存、不花 token | 选模型 |
| `POST` | `/admin/model/chat/test` · `/admin/model/embedding/test` | JSON Body | 用提交的连接发极短的真实请求（对话：普通回复 + 结构化输出；向量：算一个向量并报回实际维度），不保存 | 保存前先试 |
| `PUT` | `/admin/model/chat` · `/admin/model/embedding` | JSON Body | 先测，测不通不保存（对话可带 `force: true` 硬存）。对话立刻生效；向量返回 202，后台重建全部向量后才切换，期间导入返回 409 | 不重启换模型 |
| `DELETE` | `/admin/model/chat` · `/admin/model/embedding` | 无 | 恢复为 `.env` 的配置；向量同样要重建（返回 202） | 撤销控制台保存的连接 |
| `POST` | `/eval/run` | 无 | 运行内置意图识别和端到端对话评测 | 四维 LLM-as-Judge + 回归检测 |
| `GET` | `/eval/baseline` | 无 | 查看当前对比基线 | 回归结论都是相对这份数据得出的 |
| `POST` | `/eval/promote` | 无 | 把最近一次评测提升为新基线 | 人工确认分数可接受后调用 |
| `GET` | `/turns/{turn_id}` | Path | 一轮的经过：`{turn_id, complete, redacted, meta, routing, result}`，三个对象和当轮流式的同名事件一样。超出保留期、或当时日志没开时 404；`AI_HELPDESK_EVENT_LOG_READ=off` 时一律 404。需要 `X-API-Key` | 刷新后恢复检查器和学习模式、工单打开转人工那一轮 |
| `GET` | `/turns/{turn_id}/events` | Path | 这一轮的原始事件，按 seq 排好。需要 `X-API-Key` | 排障、看时间都花在哪 |
| `GET` | `/conversations/{conv_id}/turns` | Query `limit` | 一个会话的各轮（新的在前）：结局、作答 Agent、意图、转人工、工单、耗时、模型调用数、完整不完整。需要 `X-API-Key` | 看一个会话每一轮怎么走的 |
| `GET` | `/docs` | 浏览器访问 | Swagger UI | 浏览和调试所有接口 |

`/admin/model` 下除 `GET` 外都只认本机控制台发来的请求，客户实例上一律只读，细节见 2.2「在控制台切换模型连接」。

### 5.2 Skills 动态能力加载

ai-helpdesk 支持从目录加载 Skills，用来把业务流程、客服话术、排障 SOP 等规则动态注入 Agent。

默认配置：

```env
AI_HELPDESK_SKILLS_DIR=./skills
AI_HELPDESK_SKILLS_MAX_PROMPT_CHARS=5000
```

推荐结构：

```text
skills/order_logistics/SKILL.md
skills/general_customer_service/SKILL.md
```

`SKILL.md` 示例：

```markdown
---
name: 补件处理流程
description: 缺件漏发场景的客服处理规则
keywords: 补件,缺件,少发,漏发,missing part
agents: order,general
enabled: true
---

# 补件处理流程

- 先确认订单号、柜号和装箱单编号。
- 补件到货日期以工厂排产为准，不在登记时承诺日期。
```

查看加载结果：

```bash
curl http://localhost:8080/skills
```

修改 Skill 文件后热加载：

```bash
curl -X POST http://localhost:8080/skills/reload
```

### 5.3 `/health`

用途：确认服务是否初始化完成。

```bash
curl http://localhost:8080/health
```

响应示例：

```json
{
  "status": "ok",
  "agents": {
    "general_0": {
      "total": 0,
      "success_rate": 1.0,
      "avg_ms": 0.0,
      "monitor_penalty": 0.0,
      "routing_score": 1.0
    }
  }
}
```

响应里的 `llm` 一项除了供应商和模型名，还带模型服务此刻可不可用：

| 字段 | 说明 |
|------|------|
| `available` | `false` 表示这份模型配置（供应商 + 地址 + 凭证）正被保护挡着，模型调用不再出网 |
| `unavailable_reason` | `credential_rejected`（401）、`permission_denied`（403 且错误体明说不被允许）、`quota_exhausted`（402 或错误体明说额度用尽）、`rate_limited`（普通 429）、`forbidden`（403 但没说原因） |
| `retry_after_s` | 有限退避还剩几秒；`null` 表示不会自己恢复 |
| `blocked_calls` | 保护期间挡下、没有发出去的模型调用数 |

读的是进程里记下的状态，这个接口本身不调模型。前三种原因要等人处理：修好凭证或额度后
**重启后端**，或调一次受保护的 `POST /skills/reload`，下一条真实请求会重新出网去试。
`rate_limited` / `forbidden` 是有限退避（5s 起、最长 60s / 30s 起、最长 600s，服务端给了
`retry-after` 就听它的），到期自动放行。**没有定时探测。** 状态只在单个进程里，
多实例各记各的。

保护期间 `/chat` 和 `/chat/stream` 照常返回 200，但 `service_unavailable=true`、
`handoff=false`、不建工单，`agent_type` 是 `system`，`response` 是一句"没有作答"的提示。
客户明确要求人工（"转人工" / "talk to a person"）走规则通道，这时仍然照常转人工并建单。
"不建工单"只限模型或基础设施故障；置信度阈值、策略拦截这些原有的升级条件没有变。

响应里的 `tool_loop` 一项：`enabled` 是工具循环开没开（`AI_HELPDESK_TOOL_LOOP`）；`tools_rejected` 为 `true`
说明模型网关明确拒绝过工具调用，专家因此退回了单次生成，开关开着、实际没在用。

响应里的 `event_log` 一项是事件日志的状态：`mode`（`full` / `meta` / `off`）、`instance`、`read`（读接口开没开）、
`store`（`postgres` 或 `memory`）、`queue_depth`、`written`、`dropped`（按原因分的在 `dropped_by_reason`）、
`last_error`（只有异常类名：这个接口在客户实例上是匿名可读的）和保留期（`retention`：两个天数和上次清理的时间、清了多少）。
配了要开、却没开成（比如 `AI_HELPDESK_INSTANCE` 没设）时，`disabled_reason` 写明原因。

已知限制和尚未完成的验证（真实中继下的表现、两项目联调等）记在
[docs/ops/model-outage-handling.md](../docs/ops/model-outage-handling.md)。

### 5.4 `/chat`

用途：主对话接口。

请求体：

```json
{
  "message": "少发了一块门板，补件怎么走？",
  "user_id": "user_001",
  "conv_id": "session_001"
}
```

字段说明：

| 字段 | 必填 | 说明 |
|------|------|------|
| `message` | 是 | 用户输入 |
| `user_id` | 否 | 用户 ID，默认 `anonymous` |
| `conv_id` | 否 | 会话 ID，不传则自动生成 |

`user_id`、`conv_id` 会原样进审计、工单和事件日志：含 NUL、控制字符（`\x00`–`\x1f`、`\x7f`）或孤立代理字符，
或超过 200 个字的，`/chat` 和 `/chat/stream` 都直接回 422，这一轮不开始。正常客户端用 UUID，不受影响。

返回字段：

| 字段 | 说明 |
|------|------|
| `conv_id` | 会话 ID |
| `response` | Agent 回复（已经过出站策略校验） |
| `intent` | 18 类细粒度意图中的一个，或兜底的 `other` |
| `intent_group` | 意图所属大类 |
| `intent_strategy` | `fast_path`（纯规则，零 LLM 调用）或 `ensemble`（三路融合） |
| `intent_confidence` | 意图置信度 |
| `intent_source_scores` | 三路各自的得分：`rules` / `embedding` / `llm` |
| `intent_latency_ms` | 意图识别耗时，可直接对比两条通道的差距 |
| `primary_agent` | 领域打分选出的主 Agent |
| `supporting_agents` | 跨域工单里并行执行的辅助 Agent |
| `routing_reason` | 路由依据，含各专家的领域分 |
| `routing_confidence` | 主 Agent 的领域分 |
| `answer_confidence` | 意图 / 路由 / Agent 自评三者取最弱一环 |
| `needs_clarification` | 是否因低置信度触发了澄清追问 |
| `handoff` / `handoff_reason` | 是否转人工及原因 |
| `policy_violations` | 被拦截的无依据价格/时效承诺（含原句和证据） |
| `agent_type` | 实际产出文本的 Agent |
| `escalated` | 是否触发升级 |
| `knowledge_used` | 本轮是否引用了知识库检索结果 |
| `trajectory` | 工具循环打开时才有：这一轮的工具调用，从规则预取的两条开始，每条有查询词、结局、是否复用和耗时 |
| `answer_usage` | 工具循环打开时才有：作答阶段的模型调用次数和 token；被要求直接作答时带 `forced`，退回单次生成时带 `fallback` |
| `latency_ms` | 端到端耗时 |
| `turn_id` | 这一轮的 id，服务端生成；事件日志、审计和工单都按它指回这一轮（`GET /turns/{turn_id}`）。日志关着时也有，流式在 `meta` 和 `result` 里 |

### 5.5 `/search`

用途：测试 工具调用和 RAG 检索优化。

Query 参数：

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `query` | 是 | 无 | 用户检索问题 |
| `top_k` | 否 | `5` | 返回结果数量 |

示例：

```bash
curl -X POST http://localhost:8080/search \
  -H "Content-Type: application/json" \
  -d '{"query": "补件多久能到", "top_k": 3}'
```

### 5.6 `/knowledge/add`

用途：通过 JSON 批量导入知识库。

请求体（`category` 可省，省了是未分类；写 key、中文名、英文名都认，见 `GET /knowledge/categories`）：

```json
{
  "documents": [
    {
      "title": "补件流程",
      "content": "缺件需在验收单注明并提供订单号、柜号和装箱单编号...",
      "category": "delivery"
    }
  ]
}
```

分类只用来整理和浏览，检索不看它。任何一篇的分类写错了，整批都不导入，返回 400。

### 5.7 `/knowledge/upload`

用途：上传文件导入知识库。

支持格式：

| 格式 | 说明 |
|------|------|
| `.txt` | 整个文件作为一篇文档 |
| `.md` | 整个文件作为一篇文档 |
| `.json` | JSON 数组，格式为 `[{ "title": "...", "content": "...", "category": "..." }]`（`category` 可省） |

表单里可以再带一个 `category`：txt / md 就落在这一类；JSON 里每篇自己写了分类的用自己的，没写的用它。

示例：

```bash
curl -X POST http://localhost:8080/knowledge/upload \
  -F "file=@data/demo_docs/sample_knowledge.json"

curl -X POST http://localhost:8080/knowledge/upload \
  -F "file=@data/demo_docs/troubleshooting.md" -F "category=installation"
```

加分类之前导入的文档都在"未分类"里。仓库自带的资料每篇都写着分类，可以照着补：

```bash
python scripts/assign_categories.py            # 只打印计划：只认一字不差的标题，只动未分类的
python scripts/assign_categories.py --apply    # 照计划归类（走接口，不用停服务，不调用模型）
```

### 5.8 `/knowledge/stats`

用途：查看知识库片段数量。

```bash
curl http://localhost:8080/knowledge/stats
```

### 5.9 `/monitor`

用途：查看 Agent 和工具在线指标。

```bash
curl http://localhost:8080/monitor
```

返回内容包括：

| 字段 | 说明 |
|------|------|
| `agent_stats` | Agent 调用次数、成功率、延迟、routing_score |
| `tool_stats` | 工具调用次数、成功率、延迟、熔断状态 |
| `active_alerts` | 最近告警 |
| `suggestions` | 优化建议 |

### 5.10 `/eval/run`

用途：运行内置评测。

```bash
curl -X POST http://localhost:8080/eval/run
```

返回内容包括：

| 字段 | 说明 |
|------|------|
| `pass_rate` | 评测通过率 |
| `total` | 评测项总数 |
| `passed` | 通过项数量 |
| `avg_scores` | 平均评分 |
| `regressions` | 回归检测结果 |
| `recommendations` | 优化建议 |
| `results` | 每条评测结果 |
| `incomplete` / `incomplete_reason` / `planned` | 模型服务不可用时评测会中止：`results` 是中止前完成的那些，`planned` 是本来要跑的项数。未完成的一轮不做回归对比、不写基线，也不能提升（`/eval/promote` 返回 409）；已完成的结果另存在基线旁边的 `baseline.incomplete.json` |

## 6. 使用项目

### 6.1 主对话接口

请求：

```bash
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "我的订单什么时候到？",
    "user_id": "user_001",
    "conv_id": "session_001"
  }'
```

响应示例：

```json
{
  "conv_id": "session_001",
  "response": "请提供订单号，我可以帮您查询订单的生产进度和送货安排。",
  "intent": "order_status",
  "intent_group": "query",
  "intent_strategy": "fast_path",
  "intent_confidence": 0.82,
  "intent_source_scores": {"rules": 0.82, "embedding": 0.0, "llm": 0.0},
  "intent_latency_ms": 0.21,
  "primary_agent": "order",
  "supporting_agents": [],
  "routing_reason": "intent=order_status, group=query, primary=order, supporting=none, scores=[order=0.99, general=0.10, technical=0.00, billing=0.00]",
  "routing_confidence": 0.99,
  "answer_confidence": 0.82,
  "needs_clarification": false,
  "handoff": false,
  "handoff_reason": "",
  "policy_violations": [],
  "agent_type": "order",
  "escalated": false,
  "knowledge_used": true,
  "latency_ms": 1234.5
}
```

字段说明：

| 字段 | 含义 |
|------|------|
| `message` | 用户输入 |
| `user_id` | 用户唯一标识，用于隔离记忆和用户画像 |
| `conv_id` | 会话 ID，相同 `conv_id` 表示同一轮多轮对话 |
| `intent` | 识别出的意图（18 类细粒度意图之一） |
| `intent_strategy` | 走的是规则快通道还是三路融合 |
| `primary_agent` / `supporting_agents` | 主 Agent 与跨域协作的辅助 Agent |
| `answer_confidence` | 综合置信度，低于阈值会转人工 |
| `policy_violations` | 被出站策略拦下的无依据承诺 |
| `agent_type` | 实际处理请求的 Agent |
| `escalated` | 是否触发升级/转人工 |
| `latency_ms` | 端到端延迟 |

### 6.2 多轮对话

多轮对话只需要保持同一个 `user_id` 和 `conv_id`。

```bash
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "订单号是 A123456",
    "user_id": "user_001",
    "conv_id": "session_001"
  }'
```

系统会从 Redis 读取当前会话最近消息，并从 Postgres(pgvector) 读取相关历史和用户画像，拼成上下文传给 Agent。

### 6.3 产品与安装问题示例

```bash
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "柜门关不严，铰链怎么调？",
    "user_id": "user_tech",
    "conv_id": "tech_001"
  }'
```

预期会路由到 `technical` Agent。

### 6.4 报价与账款问题示例

```bash
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "定金付了怎么还显示未付？账单也对不上",
    "user_id": "user_bill",
    "conv_id": "bill_001"
  }'
```

预期会路由到 `billing` Agent。

### 6.5 复合问题示例

```bash
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "门板有划痕，而且尾款还多收了",
    "user_id": "user_mix",
    "conv_id": "mix_001"
  }'
```

这类问题会触发多 Agent 并行协作，由产品与安装 Agent 和报价与账款 Agent 分别处理后合并回复。

## 7. 知识库使用

ai-helpdesk 的知识库由 `app/tools/knowledge_base.py` 管理，底层是 Postgres(pgvector) 里的两张表：
`kb_documents`（文档原文、来源、分类）和 `kb_chunks`（检索用的片段和向量），表的说明见第 8 节，用 psql 查看见第 9 节。

首次启动时，如果知识库为空，会自动导入默认业务文档，包括定制流程与节点、交期说明、报价构成与变更加价、缺件漏发与补件流程、安装与常见现场问题、保修范围与除外情形。

### 7.1 查看知识库统计

```bash
curl http://localhost:8080/knowledge/stats
```

响应示例（数字随库而变）：

```json
{
  "total_documents": 6,
  "legacy_documents": 0,
  "total_chunks": 6
}
```

`legacy_documents` 是其中由加文档表之前的历史分片归档来的篇数，其余都是导入时存下原文的。

### 7.2 批量导入文档

```bash
curl -X POST http://localhost:8080/knowledge/add \
  -H "Content-Type: application/json" \
  -d '{
    "documents": [
      {
        "title": "改单规则",
        "content": "未排产的变更只收材料差价；已开料的变更按材料损失加重新加工费计算。"
      },
      {
        "title": "五金升级",
        "content": "阻尼铰链和全展滑轨可选升级，具体加价以正式报价单为准。"
      }
    ]
  }'
```

系统会把长文档切成 500 字左右的片段，并写入 Postgres(pgvector)。

### 7.3 上传文件导入知识库

上传 Markdown：

```bash
curl -X POST http://localhost:8080/knowledge/upload \
  -F "file=@data/demo_docs/troubleshooting.md"
```

上传 JSON：

```bash
curl -X POST http://localhost:8080/knowledge/upload \
  -F "file=@data/demo_docs/sample_knowledge.json"
```

JSON 格式必须是数组：

```json
[
  {
    "title": "文档标题",
    "content": "文档内容"
  }
]
```

### 7.4 检索知识库

```bash
curl -X POST http://localhost:8080/search \
  -H "Content-Type: application/json" \
  -d '{"query": "补件需要多久发出", "top_k": 3}'
```

响应示例（数字和 id 是示意）：

```json
{
  "query": "补件需要多久发出",
  "results": [
    {
      "title": "缺件漏发与补件流程",
      "content": "收货时须按装箱单当场清点。发现缺件、漏发或错发，需在验收单上注明……",
      "score": 0.82,
      "chunk": 0,
      "retrieval": "hybrid",
      "doc_id": "doc_…",
      "chunk_id": "…",
      "doc_title": "缺件漏发与补件流程"
    }
  ],
  "reranked": true,
  "corrected": false,
  "evidence_ok": true
}
```

`score` 是余弦相似度；Embedding 暂时不可用、只走了关键词那一路时是 `null`。`retrieval` 说明这条由哪一路召回：
`dense`、`lexical` 或 `hybrid`。`corrected` 表示有没有触发过自纠错（补检一轮），`evidence_ok` 是最终的证据充分性判定，
这一轮没判成（模型调用失败）时是 `null`。

`/search` 使用的是完整检索优化链路：

```text
原始查询
  -> LLM 查询改写成多个角度（原始查询也保留）
  -> 每个子查询稠密 + 关键词两路并行召回
       接了真正的 Embedding 模型：RRF 融合名次
       本地 n-gram 向量：关键词结果在前，向量只补位
  -> 合并去重
  -> 重排（配了 cross-encoder 用它；否则 LLM 重排，看每条的标题和最多 600 字正文，顺带判断证据够不够）
  -> 证据不够：围绕缺口再改写、召回、重排一轮（只补一轮）
  -> 返回 Top-K
```

证据够不够：LLM 重排顺带给出判定；用 cross-encoder 或候选太少没调模型时，看前几条里最高的余弦分是否达到
`RAG_EVIDENCE_MIN_SCORE`（默认 0.5）。远端 Embedding 配了但暂时不可用时，检索只走关键词一路，不拿本地向量
顶上；冷却期（默认 30 秒）过后自动重试。为什么本地向量不参与 RRF、实测数字是多少，见
[docs/evaluation/retrieval-ca-factory.md](../docs/evaluation/retrieval-ca-factory.md)。

### 7.5 按分类整理

文档可以归到 7 个固定分类之一（`GET /knowledge/categories` 列出 key、中文名、英文名和篇数，
顺序是客户从咨询到售后），也可以不分类。分类存在 `kb_documents.category`，老库启动时原地加列，
已有文档都是未分类。

- 导入：`/knowledge/add` 每篇带 `category`，`/knowledge/upload` 表单带 `category`（见 5.6、5.7）。
  key、中文名、英文名都认；一批里有一个分类不认识，整批不导入、返回 400；重复导入已有的文档不会改它的分类。
- 浏览：`GET /knowledge/documents?category=delivery`，`category=uncategorized` 看未分类的。
- 改分类：`PATCH /knowledge/documents/{doc_id}`，body `{"category": "installation"}`，空串是未分类。
- 分类只用来整理和浏览：检索、回答都不看它，改分类不碰分片和向量。
- 老文档补分类：`python scripts/assign_categories.py`（只打印计划）/ `--apply`。默认连员工实例
  `http://127.0.0.1:8080`（`--api` 改地址，`--api-key` 或 `AI_HELPDESK_API_KEY` 带钥匙），
  只认和仓库自带资料一字不差的标题、只动未分类的文档；`--corpus` 可以再加一份带分类的 JSON。
  走的是接口，不用停服务，也不调用模型。

### 7.6 换 Embedding 模型

不同模型的向量不在一个空间里，换模型必须重建库里的向量。库里 `storage_meta` 记着向量是哪个
模型算的，和当前配置对不上时服务拒绝启动，并提示下面的命令。

- 不停服务：在控制台「模型连接」保存向量模型，见 2.2「在控制台切换模型连接」。
- 停服务：改好 `backend/.env` 的 `EMBEDDING_*`（`EMBEDDING_DIM` 要等于模型实际输出维度），
  停掉应用实例，然后

  ```bash
  python scripts/reembed.py           # 打印现状和计划，并向 Embedding 要一个向量确认能通
  python scripts/reembed.py --apply   # 先算完全部新向量，再整批换上
  ```

  不加 `--apply` 也会向 Embedding 发一次真实请求（配了远端的话）。控制台保存过向量模型时，脚本用保存的那一组。
  默认连服务用的库，也可以 `--dsn` / `--sqlite` 指定。只换向量，文档、分片、关键词索引、记忆摘要都不动。
- 换完用 `python scripts/eval_retrieval.py --embedding env` 看检索效果（它会按 `EMBEDDING_*` 真的调用远端），
  并按它打印的分数分布重新定 `RAG_EVIDENCE_MIN_SCORE`。

有的模型要求查询前加一段指令（如 Qwen3-Embedding），用 `EMBEDDING_QUERY_PREFIX` 配；它只加在
查询上，改它不用重建。

## 8. Postgres(pgvector) 在项目中的用途

配置了 `POSTGRES_DSN`（全栈 compose 里已经配好）时，工单、对话审计、知识库、记忆、用户画像和事件日志都收敛进同一个 Postgres 库，
没有单独的向量数据库服务。向量用 pgvector 的 `vector(N)` 列存，`N` 在建表时由当前 Embedding 决定。
表定义在 `app/storage/pg.py`；读写封装在 `app/storage/vector_store.py`（知识库、记忆、画像）、
`app/tickets/ticket_store.py`（工单）、`app/storage/conversation_log.py`（对话审计）和 `app/events/store.py`（事件日志）。

ai-helpdesk 使用 11 张表：

| 表 | 模块 | 作用 |
|----|------|------|
| `kb_documents` | `app/tools/knowledge_base.py` | 知识库文档（人读的单位）：原文、来源、内容哈希、`category` 分类、片段数 |
| `kb_chunks` | `app/tools/knowledge_base.py` | 知识库片段（检索的单位）：片段文本、`embedding` 稠密向量、`tokens` 关键词索引列，`doc_id` 指回文档 |
| `memory_episodic` | `app/memory/conversation_memory.py` | 情景记忆：压缩后的历史对话摘要、原文前 500 字、向量 |
| `memory_profile` | `app/memory/conversation_memory.py` | 用户画像：每个 `user_id` 一行，`profile` 是 JSON 文本，包含偏好和关键实体 |
| `storage_meta` | `app/storage/vector_store.py` | 存储层自己的元数据，目前只有 `embedding_signature`：库里的向量是哪个模型、多少维算的（`模型名/维度`） |
| `tickets` | `app/tickets/ticket_store.py` | 转人工工单 |
| `ticket_turns` | `app/tickets/ticket_store.py` | 工单是转人工的哪一轮：`ticket_id` → `turn_id`。单独成表而不在 `tickets` 上加列：两个实例共用一个库，旧镜像按 `SELECT *` 读 `tickets`；老工单没有行 |
| `ticket_notifications` | `app/tickets/ticket_store.py`（投递循环在 `notifier.py`） | 工单通知的待发送记录（outbox），`ticket_id` 唯一，同一个工单只入队一次 |
| `conversation_log` | `app/storage/conversation_log.py` | 对话审计原文，append-only，工单里的 `conv_id` 在这里能查回完整对话 |
| `conversation_log_turns` | `app/storage/conversation_log.py` | 审计记录是哪一轮、哪个实例写的：`seq` → `turn_id`、`instance`；单独成表的理由同 `ticket_turns` |
| `turn_events` | `app/events/` | 一轮的事件日志，主键 `turn_id + seq`；两个实例共用，按 `instance` 分开。怎么开、怎么读见 2.2「记下每一轮的经过」和 [docs/ops/turn-event-log.md](../docs/ops/turn-event-log.md) |

数据写入时机：

| 数据 | 写入时机 |
|------|----------|
| `kb_documents` / `kb_chunks` | 知识库为空时启动自动导入内置示例（`source_type = seed`），或调用 `/knowledge/add`、`/knowledge/upload`；改分类只改 `kb_documents.category` |
| `memory_episodic` | 当前会话工作记忆达到压缩阈值（`MemoryManager.COMPRESS_AT`，15 条）后自动压缩并写入 |
| `memory_profile` | 每次 `/chat` 回复后异步提炼并更新 |
| `storage_meta` | 启动时校验向量来源；没有记录就记下当前 Embedding；换模型后用控制台切换或 `scripts/reembed.py --apply` 重建才会更新 |
| `tickets` / `ticket_turns` | 对话转人工时创建工单，并记下是哪一轮 |
| `ticket_notifications` | 配置了 `TICKET_WEBHOOK_URL` 才写：创建工单时先落一条待发送记录再投递，后台补投循环负责重试；没配置时转人工只落 `tickets` |
| `conversation_log` / `conversation_log_turns` | 每次 `/chat` 追加用户消息和回复 |
| `turn_events` | 事件日志开着（`AI_HELPDESK_EVENT_LOG` 是 `full` 或 `meta`，并设了 `AI_HELPDESK_INSTANCE`）时，每一轮的经过按序追加；原文和整行到期后由服务清理 |

注意：

- 员工实例和客户实例**共用同一个库**。客户实例设置了 `ENABLE_CROSS_SESSION_MEMORY=false`，它不读也不写
  `memory_episodic` 和 `memory_profile`，所以这两张表里只有员工实例产生的数据。
- 没配置 `POSTGRES_DSN` 时（本地直跑、单测）回退到 SQLite 文件（`backend/data/vector_store.db`、`backend/data/tickets.db`、
  `backend/data/conversations.db`），这时没有 pgvector，也就没有下面的 psql 可查。表名和字段基本一致，只有两处不同：
  工单和审计的 `turn_id`（以及审计的 `instance`）是 `tickets`、`conversation_log` 上的列，没有 `ticket_turns` 和
  `conversation_log_turns`；没有 `turn_events`，事件只存进程内最近 200 轮，重启就没了。

## 9. 在 Docker 中查看 Postgres(pgvector) 内容

Compose 中 Postgres(pgvector) 容器名是：

```text
ai-helpdesk-postgres
```

宿主机访问端口是 `127.0.0.1:5433`，容器内部端口是 `5432`。默认账号、密码和库名都是 `aihelpdesk`
（compose 里 `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` 可以覆盖，下文命令里的 `-U`、`-d` 相应替换）。

查看数据最直接的方式是用容器里自带的 `psql`，不需要装任何客户端，也不需要进应用容器。

### 9.1 查看 Postgres(pgvector) 是否存活

```bash
docker compose ps postgres
docker exec ai-helpdesk-postgres pg_isready -U aihelpdesk -d aihelpdesk
```

输出 `accepting connections` 就是正常的。

### 9.2 进入 psql，查看有哪些表

```bash
docker exec -it ai-helpdesk-postgres psql -U aihelpdesk -d aihelpdesk
```

进入后：

```sql
\dt                 -- 列出表，应该看到第 8 节的 11 张表
\d kb_chunks        -- 看表结构；embedding 列的类型是 vector(N)，N 是当前 Embedding 的维度
```

各表行数（`turn_events` 在事件日志关着时是 0）：

```sql
SELECT 'kb_documents' AS t, count(*) FROM kb_documents
UNION ALL SELECT 'kb_chunks', count(*) FROM kb_chunks
UNION ALL SELECT 'memory_episodic', count(*) FROM memory_episodic
UNION ALL SELECT 'memory_profile', count(*) FROM memory_profile
UNION ALL SELECT 'tickets', count(*) FROM tickets
UNION ALL SELECT 'ticket_notifications', count(*) FROM ticket_notifications
UNION ALL SELECT 'conversation_log', count(*) FROM conversation_log
UNION ALL SELECT 'turn_events', count(*) FROM turn_events;
```

只跑一条语句、不进交互界面时用 `-c`：

```bash
docker exec ai-helpdesk-postgres psql -U aihelpdesk -d aihelpdesk -c "SELECT count(*) FROM kb_documents;"
```

宿主机装了 `psql` 的话也可以直接连映射出来的端口：

```bash
psql "postgresql://aihelpdesk:aihelpdesk123@127.0.0.1:5433/aihelpdesk"
```

表里的时间字段（`created_at`、`ts`、`ingested_at`、`next_attempt_at` 等）都是 Unix 秒，用 `to_timestamp(...)` 转成可读时间
（`turn_events.ts` 同理，它的 `t_ms` 是距这一轮开始的毫秒数）。
查询时不要 `SELECT *` 带出 `embedding` 列：它是几百到上千个浮点数，只会刷屏。

### 9.3 查看知识库文档 `kb_documents`

```sql
SELECT id, title, category, origin, source_type, source_name, chunk_count,
       to_timestamp(created_at) AS created
FROM kb_documents
ORDER BY created_at DESC
LIMIT 10;
```

字段说明：

| 字段 | 含义 |
|------|------|
| `category` | 分类；空串是未分类 |
| `origin` | 原文的可信程度：`original` 是导入时存下的原文，`legacy` 是从升级前的片段拼回来的，`legacy_partial` 是历史片段不全、只拼出找得到的那几片 |
| `source_type` | 来源类型：`seed`（内置示例）、`manual`（工作台粘贴导入）、`upload`（上传文件）、`legacy`（加文档表之前导入、来源没记） |
| `chunk_count` | 这篇文档切成了几个片段 |

每个分类有多少篇：

```sql
SELECT category, count(*) FROM kb_documents GROUP BY category ORDER BY count(*) DESC;
```

### 9.4 查看知识库片段 `kb_chunks` 和向量来源

```sql
SELECT id, doc_id, title, chunk_index, total_chunks, left(content, 80) AS preview
FROM kb_chunks
ORDER BY doc_id, chunk_index
LIMIT 10;
```

`doc_id` 为空串的是升级前写入、还没归档到文档的老片段，启动时会由 `KnowledgeBase.ensure_documents` 归档。

库里的向量是哪个 Embedding 模型算的：

```sql
SELECT * FROM storage_meta;
```

`embedding_signature` 的值是 `模型名/维度`（本地向量是 `local_ngram/维度`）。它和当前 `EMBEDDING_*` 配置对不上时服务会拒绝启动，
处理方法见 7.6 节。

### 9.5 在库里按关键词找内容

psql 里算不出查询向量，所以语义检索只能走 `POST /search`；排查"某段话到底进没进库"时，直接用字面匹配：

```sql
SELECT d.id, d.title, d.category
FROM kb_documents d
WHERE d.content ILIKE '%补件%'
LIMIT 10;
```

### 9.6 查看用户画像 `memory_profile`

先多调用几次 `/chat`，让系统异步生成用户画像（调用员工实例；客户实例不写画像）：

```bash
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "我经常咨询交期和补件问题，回答请简洁一点", "user_id": "profile_user", "conv_id": "profile_session"}'
```

等待几秒后查看：

```sql
SELECT user_id, conv_id, to_timestamp(ts) AS updated, jsonb_pretty(profile::jsonb) AS profile
FROM memory_profile
WHERE user_id = 'profile_user';
```

每个 `user_id` 只有一行，后续更新会覆盖它。

### 9.7 查看情景记忆 `memory_episodic`

情景记忆只有在当前会话消息数量达到压缩阈值后才会写入。默认阈值在 `MemoryManager.COMPRESS_AT` 中，目前是 15 条消息。

可以连续发送多条消息触发压缩：

```bash
for i in $(seq 1 16); do
  curl -s -X POST http://localhost:8080/chat \
    -H "Content-Type: application/json" \
    -d "{\"message\": \"这是第 $i 条测试消息，我想咨询交期和补件问题\", \"user_id\": \"episodic_user\", \"conv_id\": \"episodic_session\"}" > /dev/null
done
```

查看情景记忆：

```sql
SELECT id, conv_id, to_timestamp(ts) AS written, summary, full_text
FROM memory_episodic
WHERE user_id = 'episodic_user'
ORDER BY ts;
```

### 9.8 查看工单、通知和对话审计

最近的工单：

```sql
SELECT id, conv_id, user_id, status, urgency, reason, to_timestamp(created_at) AS created
FROM tickets
ORDER BY created_at DESC
LIMIT 10;
```

工单是转人工的哪一轮记在旁边的 `ticket_turns`（`ticket_id`、`turn_id`），老工单没有行。

工单通知的投递状态（`pending` 是待发送或等待重试，`attempts` 是已试次数，失败原因在 `last_error`）：

```sql
SELECT ticket_id, status, attempts, to_timestamp(next_attempt_at) AS next_attempt, last_error
FROM ticket_notifications
ORDER BY created_at DESC
LIMIT 10;
```

某个会话的完整对话（用工单里的 `conv_id`）：

```sql
SELECT seq, role, agent_type, to_timestamp(created_at) AS at, content
FROM conversation_log
WHERE conv_id = 'episodic_session'
ORDER BY seq;
```

每条记录是哪一轮、哪个实例写的在旁边的 `conversation_log_turns`（`seq`、`turn_id`、`instance`）。两个实例共用这张表，
客户自己挑的 `conv_id` 可能撞上员工的会话，要区分就按 `seq` 关联过去看 `instance`。

### 9.9 查看 Postgres(pgvector) 持久化文件

Postgres(pgvector) 的持久化卷在 Compose 中定义为：

```yaml
volumes:
  postgres-data:
```

查看 Docker volume：

```bash
docker volume ls | grep postgres
docker volume inspect ai-helpdesk_postgres-data
```

卷名前缀是 compose 项目名（默认取仓库目录名），以 `docker volume ls` 的输出为准。

查看容器内数据目录：

```bash
docker exec -it ai-helpdesk-postgres sh
ls -lah /var/lib/postgresql/data
```

注意：不要直接修改这些底层文件。查看和管理数据用上面的 psql；备份和恢复见 `scripts/docker-deploy.sh` 与 `docs/ops/`。

### 9.10 清空 Postgres(pgvector) 数据

谨慎操作：员工实例和客户实例共用这个库，下面的操作对两边同时生效，且不可撤销。

整库清空（停止服务并删除 volume）：

```bash
docker compose down
docker volume rm ai-helpdesk_postgres-data
docker compose up -d --build
```

只清空知识库，保留工单、记忆和对话审计（两张表必须一起清，否则会留下没有片段的文档或没有文档的片段）：

```sql
TRUNCATE kb_chunks, kb_documents;
```

清空后重启应用，`KnowledgeBase` 会在知识库为空时重新导入内置示例文档；你自己导入过的文档不会回来，需要重新导入。
`storage_meta` 里的向量来源不受影响，只要 Embedding 配置没换就不需要重建。

## 10. Redis 工作记忆查看

Redis 容器名：

```text
ai-helpdesk-redis
```

进入 Redis：

```bash
docker exec -it ai-helpdesk-redis redis-cli -a aihelpdesk123
```

查看 key：

```redis
KEYS *
```

工作记忆 key 格式：

```text
wm:{user_id}:{conv_id}
```

会话摘要 key 格式：

```text
summary:{user_id}:{conv_id}
```

查看某个会话最近消息：

```redis
LRANGE wm:user_001:session_001 0 -1
```

查看 TTL：

```redis
TTL wm:user_001:session_001
```

默认 TTL 是 24 小时。

## 11. 查看工作记忆压缩内容

工作记忆压缩发生在 `app/memory/conversation_memory.py` 中。默认配置：

```text
WORKING_MAX = 20
COMPRESS_AT = 15
```

当同一个 `user_id + conv_id` 的工作记忆达到 15 条消息时，系统会：

```text
旧消息 -> LLM 摘要 -> Redis summary
旧消息摘要 -> Postgres(pgvector) memory_episodic 表
最近 5 条消息 -> 继续保留在 Redis wm 列表
```

日志示例：

```text
工作记忆压缩完成: cli_user/5a076f2b-b607-4339-9e9f-f0399862d366，摘要 19 字
```

其中：

```text
user_id = cli_user
conv_id = 5a076f2b-b607-4339-9e9f-f0399862d366
```

### 11.1 查看 Redis 中的会话摘要

进入 Redis：

```bash
docker exec -it ai-helpdesk-redis redis-cli -a aihelpdesk123
```

查询摘要：

```redis
GET summary:cli_user:5a076f2b-b607-4339-9e9f-f0399862d366
```

一条命令快速查看：

```bash
docker exec -it ai-helpdesk-redis redis-cli -a aihelpdesk123 \
  GET summary:cli_user:5a076f2b-b607-4339-9e9f-f0399862d366
```

### 11.2 查看压缩后仍保留的最近 5 条工作记忆

进入 Redis 后执行：

```redis
LRANGE wm:cli_user:5a076f2b-b607-4339-9e9f-f0399862d366 0 -1
```

一条命令快速查看：

```bash
docker exec -it ai-helpdesk-redis redis-cli -a aihelpdesk123 \
  LRANGE wm:cli_user:5a076f2b-b607-4339-9e9f-f0399862d366 0 -1
```

说明：

- Redis 使用 `LPUSH` 写入，最新消息在列表前面。
- 代码读取时会 `reversed(raws)` 还原时间顺序。
- 压缩后 Redis 工作记忆列表只保留最近 5 条；更早的内容会以摘要形式进入 Redis summary 和 Postgres(pgvector) 的 `memory_episodic` 表。

### 11.3 查看 Postgres(pgvector) 中的情景记忆摘要

如果是全栈部署，Postgres 容器名是 `ai-helpdesk-postgres`，不需要进应用容器，直接用 psql：

```bash
docker exec -it ai-helpdesk-postgres psql -U aihelpdesk -d aihelpdesk
```

如果你是用 `docker run --rm` 跑 CLI，它和全栈 compose 共用同一个 Postgres（要先 `docker compose up -d redis postgres`），
数据一样在这里查。查某个会话的情景记忆：

```sql
SELECT id, to_timestamp(ts) AS written, summary, full_text
FROM memory_episodic
WHERE user_id = 'cli_user'
  AND conv_id = '5a076f2b-b607-4339-9e9f-f0399862d366'
ORDER BY ts;
```

字段说明：

| 字段 | 含义 |
|------|------|
| `summary` | LLM 生成的历史对话摘要 |
| `user_id` | 用户 ID |
| `conv_id` | 会话 ID |
| `ts` | 写入时间（Unix 秒，`to_timestamp(ts)` 转可读时间） |
| `full_text` | 被压缩的原始旧消息前 500 字预览 |
| `embedding` | 摘要的向量，跨会话语义检索用；查询时别带出来 |

### 11.4 如果只想看某个用户的所有情景记忆

```sql
SELECT conv_id, to_timestamp(ts) AS written, summary
FROM memory_episodic
WHERE user_id = 'cli_user'
ORDER BY ts;
```

查不到时先确认写入的是员工实例：客户实例关了跨会话记忆（`ENABLE_CROSS_SESSION_MEMORY=false`），不读也不写这张表。

### 11.5 Redis summary 和 Postgres(pgvector) memory_episodic 的区别

| 位置 | 保存内容 | 用途 |
|------|----------|------|
| Redis `summary:{user_id}:{conv_id}` | 当前会话压缩摘要 | 下一次同会话请求直接拼入 prompt |
| Postgres(pgvector) `memory_episodic` 表 | 压缩摘要 + 原文前 500 字 + 向量 | 跨会话按语义检索相关历史 |
| Redis `wm:{user_id}:{conv_id}` | 最近 5 条消息 | 保持当前对话连贯性 |

## 12. Monitor 在线监控

查看监控摘要：

```bash
curl http://localhost:8080/monitor
```

响应包含：

```json
{
  "agent_stats": {
    "general_0": {
      "total": 10,
      "success_rate": 1.0,
      "avg_ms": 1200.3,
      "monitor_penalty": 0.0,
      "routing_score": 0.836
    }
  },
  "tool_stats": {
    "knowledge_search": {
      "total": 5,
      "success_rate": 1.0,
      "avg_latency_ms": 80.2,
      "consecutive_fails": 0,
      "circuit_state": "closed"
    }
  },
  "active_alerts": [],
  "suggestions": []
}
```

指标含义：

| 指标 | 含义 |
|------|------|
| `total` | 调用次数 |
| `success_rate` | 成功率 |
| `avg_ms` / `avg_latency_ms` | 平均延迟 |
| `routing_score` | Agent 路由评分 |
| `monitor_penalty` | Monitor 根据在线表现写回的降权系数 |
| `consecutive_fails` | 工具连续失败次数 |
| `circuit_state` | 工具熔断器状态，可能是 `closed`、`open`、`half_open` |

Prometheus 页面：

```text
http://localhost:9090
```

## 13. 运行端到端评测

```bash
curl -X POST http://localhost:8080/eval/run
```

评测内容：

1. 意图识别准确率和 Macro-F1
2. 调用 Orchestrator 生成真实回复
3. LLM-as-Judge 从相关性、准确性、完整性、有用性打分
4. 与上一次评测结果做回归检测
5. 生成优化建议

响应示例：

```json
{
  "pass_rate": 0.83,
  "total": 5,
  "passed": 4,
  "avg_scores": {
    "intent_accuracy": 0.875,
    "relevance": 0.88,
    "accuracy": 0.82,
    "completeness": 0.79,
    "helpfulness": 0.85
  },
  "regressions": [],
  "recommendations": [
    "意图识别准确率 < 90%：增加 Few-shot 示例，或对低 F1 的意图类别补充训练数据"
  ],
  "results": []
}
```

## 14. 停止、重启和清理

停止服务：

```bash
docker compose stop
```

重启服务：

```bash
docker compose restart ai-helpdesk
```

停止并删除容器，但保留数据卷：

```bash
docker compose down
```

停止并删除容器和数据卷：

```bash
docker compose down -v
```

重新构建并启动：

```bash
docker compose up -d --build
```

## 15. 常见问题

### 15.1 `/health` 返回 503

查看应用日志：

```bash
docker compose logs -f ai-helpdesk
```

重点检查：

- `.env` 是否配置 `ANTHROPIC_API_KEY`
- Redis 是否健康
- Postgres(pgvector) 是否健康
- 应用容器是否正在反复重启

### 15.2 Postgres(pgvector) 连接失败

查看 Postgres(pgvector) 状态：

```bash
docker compose ps postgres
docker compose logs -f postgres
docker exec ai-helpdesk-postgres pg_isready -U aihelpdesk -d aihelpdesk
```

用 psql 确认账号、库名能连上、表都在：

```bash
docker exec ai-helpdesk-postgres psql -U aihelpdesk -d aihelpdesk -c "\dt"
```

应用启动时会连 `POSTGRES_DSN`，连不上就不会起来。compose 已经把应用容器里的 `POSTGRES_DSN` 固定成
`postgresql://aihelpdesk:…@postgres:5432/aihelpdesk`（走 Docker 内网，端口是容器内的 `5432`，不是宿主机映射的 `5433`）。
下面几种情况最常见：

- 应用日志里是 `Name or service not known`：应用容器不在 `ai-helpdesk_ai-helpdesk-network` 网络里（`docker run` 忘了 `--network`）。
- 日志里是向量维度或 `embedding_signature` 报错：不是连接问题，是换过 Embedding 模型后向量没重建，见 7.6 节。
- 本地直跑（不在容器里）要连宿主机映射的端口：`POSTGRES_DSN=postgresql://aihelpdesk:aihelpdesk123@127.0.0.1:5433/aihelpdesk`。

### 15.3 Redis 认证失败

确认 `.env` 和 `docker-compose.yml` 中使用的密码一致。默认密码是：

```text
aihelpdesk123
```

测试连接：

```bash
docker exec -it ai-helpdesk-redis redis-cli -a aihelpdesk123 ping
```

### 15.4 `/search` 没有结果

先确认知识库中有数据：

```bash
curl http://localhost:8080/knowledge/stats
```

如果是 0，可以重新导入演示文档：

```bash
curl -X POST http://localhost:8080/knowledge/upload \
  -F "file=@data/demo_docs/sample_knowledge.json"
```

再测试：

```bash
curl -X POST http://localhost:8080/search \
  -H "Content-Type: application/json" \
  -d '{"query": "API如何接入", "top_k": 3}'
```

### 15.5 用户画像查不到

用户画像是异步更新的，并且依赖 LLM 调用成功。排查步骤：

1. 先调用 `/chat`，使用固定 `user_id`
2. 等待几秒
3. 查看 `docker compose logs -f ai-helpdesk` 是否出现 `用户画像已更新`
4. 用 9.6 节的 SQL 查询 `memory_profile` 表（只有员工实例写画像，客户实例不写）

### 15.6 情景记忆查不到

情景记忆不是每次对话都写入。只有当前会话消息数达到压缩阈值后才写入。默认阈值：

```text
MemoryManager.COMPRESS_AT = 15
```

连续发 16 条以上消息后再用 9.7 节的 SQL 查看 `memory_episodic` 表。同样只有员工实例会写。

## 16. 推荐验证流程

完整验证可以按这个顺序执行：

```bash
# 1. 启动
docker compose up -d --build

# 2. 健康检查
curl http://localhost:8080/health

# 3. 主对话
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好，我想了解保修范围", "user_id": "demo_user", "conv_id": "demo_conv"}'

# 4. 知识库统计
curl http://localhost:8080/knowledge/stats

# 5. 导入演示知识库
curl -X POST http://localhost:8080/knowledge/upload \
  -F "file=@data/demo_docs/sample_knowledge.json"

# 6. 检索
curl -X POST http://localhost:8080/search \
  -H "Content-Type: application/json" \
  -d '{"query": "ai-helpdesk如何接入API", "top_k": 3}'

# 7. 监控
curl http://localhost:8080/monitor

# 8. Skills
curl http://localhost:8080/skills

# 9. 评测
curl -X POST http://localhost:8080/eval/run
```
