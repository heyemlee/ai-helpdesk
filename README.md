# ai-helpdesk

多 Agent 客服工单编排系统。把一条工单从"进来"到"回复出去"的全过程拆成可治理的工程链路：
细粒度意图识别 → 领域打分路由 → 专家 Agent 协同 → 出站策略校验 → 置信度转人工，
并配套工单落库、对话审计、在线监控、全链路追踪与 LLM-as-Judge 评测闭环。

后端 FastAPI，前端 React + TypeScript 控制台。**179 个后端测试 + 13 个前端测试**
（含 CI 里跑在真实 pgvector 容器上的存储合同测试）。

```
backend/    FastAPI · 意图识别 · Agent 编排 · RAG · 记忆 · 鉴权限流 · 工单 · 监控 · 评测
frontend/   React 18 + TS 控制台 · 路由检查器 / 监控面板 / 评测面板
```

## 一条请求的完整链路

```
用户请求
  └─ 身份与限流：HMAC 用户令牌推导 user_id · 每用户每分钟限流（Redis 计数）
  └─ 意图识别：18 个细粒度意图
       ├─ 规则快通道命中 ──────────► 零网络调用直接定性
       └─ 否则三路融合：规则 10% + Embedding 20% + LLM 70% 加权投票
  └─ 记忆读取：Redis 工作记忆 + pgvector 情景记忆 + 用户画像
  └─ 按意图门控 RAG：业务问题才检索（pgvector 余弦检索 + 查询改写 + LLM 重排）
  └─ 领域打分路由
       ├─ 主 Agent：Technical / Billing / Order 三个专家中得分最高的
       ├─ 辅助 Agent：跨域工单里达标的其他专家，与主 Agent 并行
       ├─ 意图落到 OTHER 且置信度不足 → 先澄清追问
       └─ 专家执行失败 → 降级到 General 兜底重试
  └─ Agent 生成回复（注入按 Agent 类型隔离的 Markdown SOP，支持热加载）
  └─ 答案策略守卫：拦截无依据的价格 / 时效承诺
  └─ 置信度转人工：意图 / 路由 / Agent 自评三路各自对齐阈值
       └─ 转人工即落工单（四态状态机 + webhook 通知，/tickets 可查可流转）
  └─ 写入记忆 · 对话审计留档 · Monitor 采集 · Evaluator 回归检测
     （全程带 X-Request-ID 与 OTel trace，日志可跳转对应 trace）
```

## 几个值得说的工程决策

**规则快通道不只是省钱。** 高频高确定性工单（转人工、开发票、重复扣款）命中高精度锚点后
直接定性，跳过 Embedding 和 LLM 两路调用，上层还能据此跳过查询改写与重排。
业务请求的首字 LLM 往返从 3-4 次降到 1 次。

**答案策略是硬约束，不是 prompt 里的一句话。** SOP 写"禁止承诺到账时间"模型照样会违反。
守卫逐句扫描出站回复：金额或时效 + 承诺动词 + 无对冲词 + 在本轮检索结果和 SOP 里找不到出处，
四条同时成立才拦截并替换为保守表述。出处比对刻意不做"数字出现过就算"的宽松匹配——
知识库写"配送 3-5 个工作日"时，模型说"退款 3 个工作日到账"必须判为无依据。

**流式的粒度是句，不是 token。** 因为策略守卫必须看到完整句子才能判断。代价是首字延迟
变成首句，换来流式与非流式受同一套约束，不会出现"流式能说、非流式不能说"。

**user_id 从令牌推导，不信任请求体。** 否则任何人传 `user_id=alice` 就能把 Alice 的
历史摘要和画像注入自己的上下文。配置 `AI_HELPDESK_AUTH_SECRET` 后 `/chat` 只认
`X-User-Token`（HMAC 三段式，标准库实现）；请求体与令牌不符直接 401，暴露冒用意图
而不是静默覆盖。

**正确性状态进 Redis，性能缓存留在进程。** 熔断器状态、路由惩罚、限流计数多实例共享——
实例 A 熔断后实例 B 对同一工具的调用同样被拒绝，这才是"保护下游"的本意。
意图缓存和工具 TTL 缓存留在进程内：各存一份只影响命中率不影响正确性，
不值得为它们付出每次读缓存一趟 Redis 往返。

**存储收敛到一个 Postgres，但换嵌入后端会 fail fast。** 工单、审计、KB 向量、情景记忆、
画像统一进 pgvector（`<=>` 余弦检索）；向量列维度由当前 Embedder 决定并写进 DDL，
启动时校验现有表维度，不一致直接报错——静默把 256 维向量塞进 1536 维列产生的是
无意义的检索结果。未配置 `POSTGRES_DSN` 时退化为 SQLite 单文件，clone 下来零依赖能跑，
且两个后端跑同一组合同测试。

**审计日志和记忆是两个东西。** 记忆面向模型：为下一轮回答组装上下文，允许压缩、摘要、
遗忘（Redis 24h TTL）；审计面向人和合规：原文 append-only，工单里的 conv_id 能查回
完整对话，保留期由运维显式 `purge_before` 决定，服务不悄悄删数据。

**回归检测对比存档基线，不是上一次运行。** 基线只在显式 `/eval/promote` 时更新——
否则每跑完一次就覆盖，"相比基线退化"永远为空。

## 可观测性

`docker compose up` 后自带完整观测栈（11 个服务）：

```
指标   FastAPI /metrics ──► Prometheus（QPS · p95 · LLM token 花费 · 限流命中 + 4 条告警规则）
追踪   OTel SDK（FastAPI/httpx/Redis/asyncpg 自动埋点 + intent/rag/agent/memory 业务 span）
         ──► OTel Collector ──► Grafana Tempo
日志   LOG_FORMAT=json（带 request_id + trace_id）──► Promtail ──► Loki
面板   Grafana :3000 自动装配 3 数据源 + 8 面板 dashboard，日志里点 trace_id 直接跳 trace
```

压测脚本 `backend/scripts/load_test.py` 输出 RPS 与 P50/P95/P99，
`--fast-path-only` 控制 LLM 成本。

## 快速开始

```bash
cp .env.example .env                   # compose 变量替换：Redis/Postgres/Grafana 密码
cp backend/.env.example backend/.env   # 应用运行时配置：填入 ANTHROPIC_API_KEY
docker compose up -d --build
```

两份 `.env` 用途不同，别混：仓库根这份只用于 `docker-compose.yml` 里的 `${VAR}` 替换
（Compose 只认 compose 文件同目录的 `.env`）；`backend/.env` 通过 `env_file` 注入应用容器。
两份都不入库，各自的 `.env.example` 是模板。

| 地址 | 内容 |
|------|------|
| http://localhost:5174 | 控制台 |
| http://localhost:8000/docs | API 文档 |
| http://localhost:3000 | Grafana（指标 / 日志 / trace 一站式） |
| http://localhost:9090 | Prometheus |

生产加固开关（demo 默认全关，行为与旧版一致）：

```bash
AI_HELPDESK_AUTH_SECRET=<32B 随机串>      # 开启 HMAC 用户令牌，/auth/token 签发
AI_HELPDESK_RATE_LIMIT_PER_MINUTE=20      # 每用户每分钟限流
AI_HELPDESK_API_KEY=<管理密钥>            # 写操作端点鉴权
TICKET_WEBHOOK_URL=<值班通知地址>          # 转人工工单 webhook
```

本地开发（同时起 uvicorn 和 vite，Ctrl-C 一起退）：

```bash
./dev.sh
```

## 测试

```bash
cd backend && pytest && ruff check .
cd frontend && npm test && npm run typecheck
```

PG 合同测试本地默认跳过；起一个 pgvector 容器后设 `TEST_POSTGRES_DSN` 即可在本地跑，
CI 里通过 service container 始终执行。

## 技术栈

FastAPI · Anthropic 兼容协议（可切 DeepSeek 等网关）· PostgreSQL + pgvector · Redis ·
OpenTelemetry · Prometheus · Grafana（Tempo / Loki / Promtail）·
React 18 · TypeScript · TanStack Query · Tailwind v4 · Vitest ·
Docker Compose · GitHub Actions
