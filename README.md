# ai-helpdesk

多 Agent 客服工单编排系统。把一条工单从"进来"到"回复出去"的全过程拆成可治理的工程链路：
细粒度意图识别 → 领域打分路由 → 专家 Agent 协同 → 出站策略校验 → 置信度转人工，
并配套在线监控与 LLM-as-Judge 评测闭环。

后端 FastAPI，前端 React + TypeScript 控制台。**124 个后端测试 + 13 个前端测试**。

```
backend/    FastAPI · 意图识别 · Agent 编排 · RAG · 记忆 · 监控 · 评测
frontend/   React 18 + TS 控制台 · 路由检查器 / 监控面板 / 评测面板
```

## 一条请求的完整链路

```
用户请求
  └─ 意图识别：18 个细粒度意图
       ├─ 规则快通道命中 ──────────► 零网络调用直接定性
       └─ 否则三路融合：规则 10% + Embedding 20% + LLM 70% 加权投票
  └─ 记忆读取：Redis 工作记忆 + ChromaDB 情景记忆 + 用户画像
  └─ 按意图门控 RAG：业务问题才检索，闲聊不浪费
  └─ 领域打分路由
       ├─ 主 Agent：Technical / Billing / Order 三个专家中得分最高的
       ├─ 辅助 Agent：跨域工单里达标的其他专家，与主 Agent 并行
       ├─ 意图落到 OTHER 且置信度不足 → 先澄清追问
       └─ 专家执行失败 → 降级到 General 兜底重试
  └─ Agent 生成回复（注入按 Agent 类型隔离的 Markdown SOP，支持热加载）
  └─ 答案策略守卫：拦截无依据的价格 / 时效承诺
  └─ 置信度转人工：意图 / 路由 / Agent 自评三路各自对齐阈值
  └─ 写入记忆 · Monitor 采集 · Evaluator 回归检测
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

**Agent 多实例是监控闭环成立的前提。** 单实例时 `monitor_penalty` 只是个数字，不改变任何
决策。多实例后，Monitor 观测到的劣化会真正把流量倒给同类型的健康实例；同分时轮询而非
固定取第一个，否则冷启动阶段流量永远压在 0 号实例，闭环根本跑不起来。

**回归检测对比存档基线，不是上一次运行。** 基线只在显式 `/eval/promote` 时更新——
否则每跑完一次就覆盖，"相比基线退化"永远为空。

## 快速开始

```bash
cp .env.example .env                   # compose 变量替换：Redis 密码
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
| http://localhost:9090 | Prometheus |

本地开发（同时起 uvicorn 和 vite，Ctrl-C 一起退）：

```bash
./dev.sh
```

## 测试

```bash
cd backend && pytest && ruff check .
cd frontend && npm test && npm run typecheck
```

## 技术栈

FastAPI · Anthropic 兼容协议（可切 DeepSeek 等网关）· ChromaDB · Redis ·
React 18 · TypeScript · TanStack Query · Tailwind v4 · Vitest ·
Prometheus · Docker Compose · GitHub Actions
