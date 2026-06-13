# ai-helpdesk 完整使用指南

本文档说明 ai-helpdesk 的部署、启动、API 调用、知识库使用、ChromaDB 数据查看、监控评测和常见排障。

ai-helpdesk 是一个企业级智能客服系统，核心链路为：

```text
用户请求
  -> FastAPI /chat
  -> MemoryManager 读取 Redis 工作记忆 + ChromaDB 情景记忆 + 用户画像
  -> IntentRecognizer 分到 18 类细粒度意图
       ├─ 规则通道命中（高精度锚点短语）→ 零网络调用直接返回
       └─ 否则三路融合：规则 10% + Embedding 20% + LLM 70% 加权投票
  -> AgentOrchestrator 按领域分打分
       ├─ 主 Agent：Technical / Billing / Order 三个专家中得分最高的
       ├─ 辅助 Agent：跨域工单里达到门槛的其他专家，与主 Agent 并行执行
       ├─ 意图落到 OTHER 且置信度不足 → 先澄清追问
       └─ 专家执行失败 → 降级到 GeneralAgent 重试
  -> Agent 生成回复（注入对应 agent 类型的 Markdown SOP，支持热加载）
  -> AnswerPolicy 出站校验：拦截无依据的价格/时效承诺
  -> 综合置信度（意图 / 路由 / Agent 自评取最弱一环）低于阈值 → 转人工
  -> 写入 Redis，并异步更新 ChromaDB 用户画像
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
├── docker-compose.yml                   # 全栈编排：backend / frontend / Redis / ChromaDB / Prometheus / Nginx
├── .env.example                         # compose 变量替换的模板（只有 REDIS_PASSWORD）
├── .env                                 # 由上一行复制而来，不入库
├── dev.sh                               # 本地开发：一条命令同时起 uvicorn 和 vite
├── .github/workflows/ci.yml             # 后端 pytest + ruff，前端 vitest + tsc，两个镜像构建
│
├── backend/                             # 本文档描述的部分
│   ├── api/main.py                      # FastAPI 入口，/chat /search /knowledge /monitor /eval
│   ├── core/intent_recognizer.py        # 18 类意图：规则快通道 + 三路融合
│   ├── core/answer_policy.py            # 出站策略守卫：拦截无依据的价格/时效承诺
│   ├── core/skill_loader.py             # Markdown SOP 热加载，按 agent 类型注入
│   ├── core/llm_utils.py                # LLM 调用封装
│   ├── agents/agent_orchestrator.py     # 三专家 + 兜底的领域分路由、主辅协作、转人工
│   ├── memory/conversation_memory.py    # Redis + ChromaDB 记忆管理
│   ├── mcp/tool_manager.py              # MCP 工具调用、查询改写、重排、熔断、缓存、降级
│   ├── mcp/knowledge_base.py            # ChromaDB RAG 知识库
│   ├── monitor/performance_monitor.py   # Agent/工具在线监控
│   ├── evaluation/evaluator.py          # 四维 LLM-as-Judge + 相对存档基线的回归检测
│   ├── skills/                          # 四份 SOP，与四个 Agent 一一对应
│   ├── tests/                           # pytest 用例（不依赖网络和外部服务）
│   ├── config/                          # nginx.conf + prometheus.yml
│   ├── data/demo_docs/                  # 演示知识库文档
│   ├── data/eval/baseline.json          # 评测存档基线，只在 /eval/promote 时更新
│   ├── docker-deploy.sh                 # 部署封装脚本：compose 生命周期 + Redis/Chroma 备份恢复
│   ├── Dockerfile
│   ├── requirements.txt
│   └── .env                             # 由 .env.example 复制而来
│
└── frontend/                            # React 18 + TypeScript 控制台
    ├── src/components/RoutingInspector.tsx  # 把每一轮的路由决策过程摊开显示
    ├── src/api/                         # client / hooks / types
    ├── src/hooks/useChatStream.ts       # 按句流式接收
    ├── Dockerfile
    └── package.json
```

### 命令在哪个目录执行

重组成 `backend/` + `frontend/` 之后，本文档里的命令分三类，执行位置不同：

| 命令 | 执行位置 | 例子 |
|------|----------|------|
| `docker compose *` | **仓库根** | `docker compose up -d --build` |
| `./dev.sh` | **仓库根** | 同时起前后端 |
| `pytest` / `ruff` / `python api/main.py` | `backend/` | `cd backend && pytest` |
| `npm *` | `frontend/` | `cd frontend && npm run dev` |

后文出现 `docker compose` 时若未特别说明，一律指在仓库根执行。

`backend/docker-deploy.sh` 是例外：它从脚本自身位置推导仓库根，**在任何目录执行都成立**。
内部所有 compose 调用都显式带 `-f <仓库根>/docker-compose.yml -p ai-helpdesk`，
不依赖工作目录，也不受仓库目录改名影响。

## 1.1 跑测试

测试全部用桩替换 LLM 调用，不需要 API Key、Redis 或 ChromaDB：

```bash
pip install -r requirements.txt && pytest
```

覆盖的核心行为：意图分类体系与规则快通道、三路融合与 LLM 故障容错、
领域分路由与主辅协作、低置信度澄清与转人工、专家失败降级、
出站策略拦截、以及相对存档基线的回归检测。

## 1.2 能力与代码位置对照

| 能力 | 实现位置 | 验证方式 |
|------|----------|----------|
| 18 类细粒度意图 | `core/intent_recognizer.py` `FINE_GRAINED_INTENTS` | `tests/test_intent_recognizer.py::test_exactly_18_fine_grained_intents` |
| 三路融合（规则 / Embedding / LLM） | `IntentRecognizer._vote`，权重 `_VOTE_WEIGHTS` | 响应里的 `intent_source_scores` 三项都非空 |
| 换供应商后 Embedding 一路不失效 | `_embed_text` 远端优先、本地 n-gram 兜底 | `test_embedding_signal_stays_enabled_with_third_party_base_url` |
| 零延迟规则通道 | `IntentRecognizer._fast_path`，锚点表 `_FAST_PATH_RULES` | `test_fast_path_makes_zero_network_calls`；响应里 `intent_strategy=fast_path` |
| 快通道联动省掉查询改写和重排 | `api/main.py::_build_knowledge_context(fast_path=True)` | 对比两条通道的 `latency_ms` |
| 三个专家 Agent + 领域分路由 | `AgentOrchestrator._domain_scores` / `_route_decision` | `test_domain_score_picks_primary_agent` |
| 跨域工单提名主 + 辅 Agent | `RoutingDecision.supporting_agents`、`run_parallel` | `test_cross_domain_promotes_supporting_agent` |
| 低置信度澄清 | `AgentOrchestrator._needs_clarification` | `test_low_confidence_other_intent_asks_for_clarification` |
| 置信度转人工 | `_handoff_signals`，三路各自对齐阈值 | `test_low_intent_confidence_triggers_handoff` |
| 专家失败降级到通用 Agent | `AgentOrchestrator._execute` | `test_specialist_failure_falls_back_to_general` |
| 按 Agent 类型注入的热加载 SOP | `core/skill_loader.py` + `skills/*/SKILL.md` | `POST /skills/reload` 后 `GET /skills` |
| 拦截无依据的价格/时效承诺 | `core/answer_policy.py` | `tests/test_answer_policy.py`；响应里的 `policy_violations` |
| 四维 LLM-as-Judge | `evaluation/evaluator.py::LLMJudge` | `test_judge_prompt_names_all_four_dimensions` |
| 相对存档基线的回归检测 | `EndToEndEvaluator._detect_regressions`，基线需显式提升 | `test_baseline_is_not_overwritten_on_every_run` |

## 2. 环境准备

### 2.1 必需依赖

- Docker
- Docker Compose
- Anthropic API Key，或兼容 Anthropic 协议的第三方 API Key

### 2.2 配置 `.env`

项目有**两份** `.env`，分工不同，配错地方会静默失效：

| 文件 | 作用 | 谁在读 |
|------|------|--------|
| `<仓库根>/.env` | `docker-compose.yml` 里 `${VAR}` 的替换值 | Docker Compose 自己 |
| `backend/.env` | 应用运行时配置（API Key、模型、Chroma 地址等） | 容器内的应用，经 `env_file` 注入 |

Compose 做变量替换时**只读 compose 文件同目录的 `.env`**，也就是仓库根那份。
把 `REDIS_PASSWORD` 写进 `backend/.env` 不会影响 Redis 容器的启动密码——
它会安静地回退到 compose 里的内置默认值 `aihelpdesk123`。

两份都复制一遍：

```bash
cd <仓库根> && cp .env.example .env
cd backend  && cp .env.example .env
```

`backend/.env` 最少需要配置：

```env
ANTHROPIC_API_KEY=your_api_key
```

如果使用 DeepSeek 这类 Anthropic 兼容接口，可以配置：

```env
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_MODEL=deepseek-v4-pro
ANTHROPIC_API_KEY=your_deepseek_key
```

Docker Compose 场景下，Redis 和 ChromaDB 的连接由 `docker-compose.yml` 覆盖为容器内地址。通常不需要手动改：

```env
REDIS_PASSWORD=aihelpdesk123
CHROMA_HOST=localhost
CHROMA_PORT=8001
```

### 2.3 全栈部署和 run 开发模式的区别

ai-helpdesk 常用两种 Docker 启动方式：`docker compose up` 全栈部署，以及 `docker run` 开发模式。两者最大的区别是：**全栈部署会同时启动应用和依赖服务；run 开发模式通常只手动运行一个应用容器，依赖服务需要提前启动**。

| 对比项 | Docker Compose 全栈部署 | Docker run 开发模式 |
|--------|--------------------------|----------------------|
| 启动命令 | `docker compose up -d --build` | `docker run ... ai-helpdesk ...` |
| 启动内容 | ai-helpdesk、Redis、ChromaDB、Prometheus、Nginx | 只启动你指定的单个容器 |
| Redis/ChromaDB | 自动启动并加入同一网络 | 必须先执行 `docker compose up -d redis chromadb` |
| 容器网络 | Compose 自动创建并管理 | 需要手动指定 `--network ai-helpdesk_ai-helpdesk-network` |
| 服务名解析 | 应用可直接访问 `redis`、`chromadb` | 只有加入同一网络后才可访问 `redis`、`chromadb` |
| 代码更新 | 通常需要 rebuild 或重启服务 | 挂载 `-v "$(pwd):/workspace"` 后，代码修改可直接生效，重启容器即可 |
| 适合场景 | 演示、联调、完整部署、HTTP API 服务 | 本地开发、调试 CLI、临时覆盖环境变量 |
| 常见问题 | API Key 或依赖健康检查失败 | 忘记启动 Redis/ChromaDB，导致 `redis:6379 Name or service not known` |

选择建议：

- 想完整体验 HTTP API、Swagger、Nginx、Prometheus：用 **Docker Compose 全栈部署**。
- 想调试源码或 CLI，并且希望本地改代码后快速重跑：用 **Docker run 开发模式**。
- 如果只是跑 CLI，最省心的方式是 `docker compose run --rm ai-helpdesk python api/main.py --cli`，它会自动使用 Compose 网络。

## 2.5 前端和后端一起启动

前端（Vue 3 + Vite）和后端（FastAPI）可以一起跑，三种方式按场景选：

### 方式 A：一条 compose 起全栈（推荐用于演示）

前端已经作为 `frontend` 服务并入 `docker-compose.yml`（仓库根），和 Redis / ChromaDB /
Prometheus / 后端在同一个网络里：

```bash
docker compose up -d --build   # 在仓库根执行
```

| 地址 | 内容 |
|------|------|
| http://localhost:5174 | 前端控制台 |
| http://localhost:8000 | 后端 API（Swagger 在 /docs） |
| http://localhost:80 | Nginx 反代的后端 API |
| http://localhost:9090 | Prometheus |

前端容器里的 Nginx 把 `/api/` 反代到 `ai-helpdesk:8000`，走 Docker 内网，
不依赖 `host.docker.internal`，也不会因为宿主机端口占用而失效。
后端地址由 `API_UPSTREAM` 环境变量注入，改 compose 就能指到别处。

前端镜像是多阶段构建（`node:20-alpine` 里跑 `npm run build`，产物拷进 `nginx:1.27-alpine`），
**不需要**先在本机执行 `npm run build`。

### 方式 B：本地一条命令起前后端（推荐用于开发）

仓库根目录的 `dev.sh` 会同时拉起 uvicorn 和 vite，Ctrl-C 一起收掉：

```bash
./dev.sh
```

打开 http://localhost:5173 即可，Vite 的 proxy 会把 `/api` 转发到 `:8000`。
脚本会先检查解释器是否装齐依赖，没装齐会直接打印修复命令。
Redis 和 ChromaDB 仍需单独起：`docker compose up -d redis chromadb`。

### 方式 C：单进程单端口（后端直接托管前端）

FastAPI 启动时会自动挂载前端构建产物，一个进程同时提供页面和 API：

```bash
cd frontend && npm run build
cd ../backend && python -m uvicorn api.main:app --port 8000
```

然后 http://localhost:8000 既是页面也是 API。查找顺序：
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

启动后的端口：

| 服务 | 容器名 | 宿主机端口 | 容器内端口 | 用途 |
|------|--------|------------|------------|------|
| ai-helpdesk API | `ai-helpdesk-app` | `8000` | `8000` | 主 API 服务 |
| Nginx | `ai-helpdesk-nginx` | `80` | `80` | 反向代理 |
| ChromaDB | `ai-helpdesk-chromadb` | `8001` | `8000` | 向量数据库 |
| Redis | `ai-helpdesk-redis` | `6379` | `6379` | 工作记忆 |
| Prometheus | `ai-helpdesk-prometheus` | `9090` | `9090` | 监控数据 |

健康检查：

```bash
curl http://localhost:8000/health
```

Swagger 文档：

```text
http://localhost:8000/docs
```

也可以通过 Nginx 访问：

```bash
curl http://localhost/health
```

## 4. Docker Run 开发模式

开发时可以只用 Compose 启动依赖，然后用 `docker run` 挂载当前代码目录。

先启动 Redis 和 ChromaDB：

```bash
docker compose up -d redis chromadb
```

构建镜像：

```bash
docker compose build --no-cache ai-helpdesk
```

启动 HTTP 服务：

```bash
docker run -it --rm \
  --network ai-helpdesk_ai-helpdesk-network \
  -p 8000:8000 \
  -e ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic" \
  -e ANTHROPIC_API_KEY="your_key" \
  -e ANTHROPIC_MODEL="deepseek-v4-pro" \
  -e REDIS_URL="redis://:aihelpdesk123@redis:6379/0" \
  -e CHROMA_HOST="chromadb" \
  -e CHROMA_PORT="8000" \
  -e CHROMA_PERSIST_DIRECTORY="/workspace/data/chroma" \
  -v "$(pwd):/workspace" \
  -w /workspace \
  ai-helpdesk
```

CLI 交互模式：

```bash
docker run -it --rm \
  --network ai-helpdesk_ai-helpdesk-network \
  -e ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic" \
  -e ANTHROPIC_API_KEY="your_key" \
  -e ANTHROPIC_MODEL="deepseek-v4-pro" \
  -e REDIS_URL="redis://:aihelpdesk123@redis:6379/0" \
  -e CHROMA_HOST="chromadb" \
  -e CHROMA_PORT="8000" \
  -v "$(pwd):/workspace" \
  -w /workspace \
  ai-helpdesk \
  python api/main.py --cli
```

## 5. Swagger 和接口总览

ai-helpdesk 基于 FastAPI 构建，启动 HTTP 服务后可以直接在浏览器访问 Swagger UI 调用接口。

本地 Swagger 地址：

```text
http://localhost:8000/docs
```

如果使用 Nginx 反向代理：

```text
http://localhost/docs
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
| `POST` | `/search` | JSON Body | 执行知识库检索优化链路：查询改写、并行召回、合并去重、LLM 重排 | 测试 RAG 检索 |
| `GET` | `/skills` | 无 | 查看当前加载的 Skills、匹配关键词和解析错误 | 确认动态能力是否生效 |
| `POST` | `/skills/reload` | 无 | 运行时重新扫描 Skill 目录 | 修改业务规则后热加载 |
| `POST` | `/knowledge/add` | JSON Body | 批量导入文档到 ChromaDB 知识库 | 程序化导入文档 |
| `POST` | `/knowledge/upload` | Form File | 上传 `.txt`、`.md`、`.json` 文件导入知识库 | 手动上传知识库文件 |
| `GET` | `/knowledge/stats` | 无 | 查看知识库文档片段总数 | 确认知识库是否有数据 |
| `POST` | `/eval/run` | 无 | 运行内置意图识别和端到端对话评测 | 四维 LLM-as-Judge + 回归检测 |
| `GET` | `/eval/baseline` | 无 | 查看当前对比基线 | 回归结论都是相对这份数据得出的 |
| `POST` | `/eval/promote` | 无 | 把最近一次评测提升为新基线 | 人工确认分数可接受后调用 |
| `GET` | `/docs` | 浏览器访问 | Swagger UI | 浏览和调试所有接口 |

### 5.2 Skills 动态能力加载

ai-helpdesk 支持从目录加载 Skills，用来把业务流程、客服话术、排障 SOP 等规则动态注入 Agent。

默认配置：

```env
AI_HELPDESK_SKILLS_DIR=./skills
AI_HELPDESK_SKILLS_MAX_PROMPT_CHARS=5000
```

推荐结构：

```text
skills/refund/SKILL.md
skills/customer_support/SKILL.md
```

`SKILL.md` 示例：

```markdown
---
name: 退款处理流程
description: 退款场景的客服处理规则
keywords: 退款,退费,refund
agents: billing,general
enabled: true
---

# 退款处理流程

- 先确认订单号和支付方式。
- 涉及实际退款操作时转人工审核。
```

查看加载结果：

```bash
curl http://localhost:8000/skills
```

修改 Skill 文件后热加载：

```bash
curl -X POST http://localhost:8000/skills/reload
```

### 5.3 `/health`

用途：确认服务是否初始化完成。

```bash
curl http://localhost:8000/health
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

### 5.4 `/chat`

用途：主对话接口。

请求体：

```json
{
  "message": "我要退款",
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
| `latency_ms` | 端到端耗时 |

### 5.5 `/search`

用途：测试 MCP 工具调用和 RAG 检索优化。

Query 参数：

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `query` | 是 | 无 | 用户检索问题 |
| `top_k` | 否 | `5` | 返回结果数量 |

示例：

```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "退款多久到账", "top_k": 3}'
```

### 5.6 `/knowledge/add`

用途：通过 JSON 批量导入知识库。

请求体：

```json
{
  "documents": [
    {
      "title": "退款政策",
      "content": "用户在购买后 7 天内可以申请无理由退款..."
    }
  ]
}
```

### 5.7 `/knowledge/upload`

用途：上传文件导入知识库。

支持格式：

| 格式 | 说明 |
|------|------|
| `.txt` | 整个文件作为一篇文档 |
| `.md` | 整个文件作为一篇文档 |
| `.json` | JSON 数组，格式为 `[{ "title": "...", "content": "..." }]` |

示例：

```bash
curl -X POST http://localhost:8000/knowledge/upload \
  -F "file=@data/demo_docs/sample_knowledge.json"
```

### 5.8 `/knowledge/stats`

用途：查看知识库片段数量。

```bash
curl http://localhost:8000/knowledge/stats
```

### 5.9 `/monitor`

用途：查看 Agent 和工具在线指标。

```bash
curl http://localhost:8000/monitor
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
curl -X POST http://localhost:8000/eval/run
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

## 6. 使用项目

### 6.1 主对话接口

请求：

```bash
curl -X POST http://localhost:8000/chat \
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
  "response": "请提供订单号，我可以帮您查询订单状态和物流进度。",
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
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "订单号是 A123456",
    "user_id": "user_001",
    "conv_id": "session_001"
  }'
```

系统会从 Redis 读取当前会话最近消息，并从 ChromaDB 读取相关历史和用户画像，拼成上下文传给 Agent。

### 6.3 技术问题示例

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "应用登录一直报 401 错误",
    "user_id": "user_tech",
    "conv_id": "tech_001"
  }'
```

预期会路由到 `technical` Agent。

### 6.4 账单问题示例

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "为什么这个月重复扣款了？我要退款",
    "user_id": "user_bill",
    "conv_id": "bill_001"
  }'
```

预期会路由到 `billing` Agent。

### 6.5 复合问题示例

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "登录报错 401，而且这个月还重复扣款了",
    "user_id": "user_mix",
    "conv_id": "mix_001"
  }'
```

这类问题会触发多 Agent 并行协作，由技术 Agent 和账单 Agent 分别处理后合并回复。

## 7. 知识库使用

ai-helpdesk 的知识库由 `mcp/knowledge_base.py` 管理，底层使用 ChromaDB collection：

```text
knowledge_base
```

首次启动时，如果知识库为空，会自动导入默认客服文档，包括退款政策、订单查询、账户安全、技术故障排查、会员积分、配送说明。

### 7.1 查看知识库统计

```bash
curl http://localhost:8000/knowledge/stats
```

响应示例：

```json
{
  "total_chunks": 18
}
```

### 7.2 批量导入文档

```bash
curl -X POST http://localhost:8000/knowledge/add \
  -H "Content-Type: application/json" \
  -d '{
    "documents": [
      {
        "title": "退换货政策",
        "content": "用户在购买后 7 天内可以申请无理由退货，审核通过后 5-7 个工作日退款。"
      },
      {
        "title": "会员权益",
        "content": "金卡会员享受 9 折优惠，生日当月可获得双倍积分。"
      }
    ]
  }'
```

系统会把长文档切成 500 字左右的片段，并写入 ChromaDB。

### 7.3 上传文件导入知识库

上传 Markdown：

```bash
curl -X POST http://localhost:8000/knowledge/upload \
  -F "file=@data/demo_docs/troubleshooting.md"
```

上传 JSON：

```bash
curl -X POST http://localhost:8000/knowledge/upload \
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
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "退款需要多久到账", "top_k": 3}'
```

响应示例：

```json
{
  "query": "退款需要多久到账",
  "results": [
    {
      "title": "退款政策",
      "content": "审核通过后，款项将在 5-7 个工作日内退回原支付账户。",
      "score": 0.82,
      "chunk": 0
    }
  ],
  "reranked": true
}
```

`/search` 使用的是完整检索优化链路：

```text
原始查询
  -> LLM 查询改写成多个角度
  -> 多个子查询并行召回 ChromaDB
  -> 合并去重
  -> LLM 重排
  -> 返回 Top-K
```

## 8. ChromaDB 在项目中的用途

ai-helpdesk 使用了三个 ChromaDB collection：

| Collection | 模块 | 作用 |
|------------|------|------|
| `knowledge_base` | `mcp/knowledge_base.py` | RAG 知识库文档片段 |
| `episodic` | `memory/conversation_memory.py` | 压缩后的历史对话摘要 |
| `user_profile` | `memory/conversation_memory.py` | 用户画像，包含偏好和关键实体 |

数据写入时机：

| 数据 | 写入时机 |
|------|----------|
| `knowledge_base` | 启动时自动导入默认文档，或调用 `/knowledge/add`、`/knowledge/upload` |
| `episodic` | 当前会话工作记忆超过阈值后自动压缩并写入 |
| `user_profile` | 每次 `/chat` 回复后异步提炼并更新 |

## 9. 在 Docker 中查看 ChromaDB 内容

Compose 中 ChromaDB 容器名是：

```text
ai-helpdesk-chromadb
```

宿主机访问端口是：

```text
http://localhost:8001
```

容器内部端口是：

```text
http://localhost:8000
```

### 9.1 查看 ChromaDB 是否存活

宿主机执行：

```bash
curl http://localhost:8001/api/v1/heartbeat
```

容器内执行：

```bash
docker exec -it ai-helpdesk-chromadb curl http://localhost:8000/api/v1/heartbeat
```

### 9.2 查看所有 collection

```bash
curl http://localhost:8001/api/v1/collections
```

如果 ChromaDB 版本返回 tenant/database 相关错误，可以使用 Python 客户端查看，见下一节。

### 9.3 用 Python 客户端查看 collections

进入应用容器：

```bash
docker exec -it ai-helpdesk-app bash
```

在容器里执行：

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
print("heartbeat:", client.heartbeat())

collections = client.list_collections()
print("collections:")
for c in collections:
    print("-", c.name, "count=", c.count())
PY
```

预期可以看到：

```text
collections:
- knowledge_base count= ...
- episodic count= ...
- user_profile count= ...
```

### 9.4 查看 `knowledge_base` 文档内容

```bash
docker exec -it ai-helpdesk-app bash
```

执行：

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("knowledge_base")

data = col.get(limit=10, include=["documents", "metadatas"])
for i, doc_id in enumerate(data["ids"]):
    print("=" * 80)
    print("id:", doc_id)
    print("metadata:", data["metadatas"][i])
    print("document:", data["documents"][i][:500])
PY
```

### 9.5 查询 `knowledge_base`

```bash
docker exec -it ai-helpdesk-app bash
```

执行：

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("knowledge_base")

result = col.query(
    query_texts=["退款多久到账"],
    n_results=3,
    include=["documents", "metadatas", "distances"],
)

for doc, meta, dist in zip(
    result["documents"][0],
    result["metadatas"][0],
    result["distances"][0],
):
    print("=" * 80)
    print("title:", meta.get("title"))
    print("distance:", dist)
    print("content:", doc[:300])
PY
```

### 9.6 查看用户画像 `user_profile`

先多调用几次 `/chat`，让系统异步生成用户画像：

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "我经常咨询会员积分和退款问题，回答请简洁一点", "user_id": "profile_user", "conv_id": "profile_session"}'
```

等待几秒后查看：

```bash
docker exec -it ai-helpdesk-app bash
```

```bash
python - <<'PY'
import json
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("user_profile")

data = col.get(
    where={"user_id": "profile_user"},
    include=["documents", "metadatas"],
)

for i, doc in enumerate(data["documents"]):
    print("=" * 80)
    print("metadata:", data["metadatas"][i])
    print(json.dumps(json.loads(doc), ensure_ascii=False, indent=2))
PY
```

### 9.7 查看情景记忆 `episodic`

情景记忆只有在当前会话消息数量达到压缩阈值后才会写入。默认阈值在 `MemoryManager.COMPRESS_AT` 中，目前是 15 条消息。

可以连续发送多条消息触发压缩：

```bash
for i in $(seq 1 16); do
  curl -s -X POST http://localhost:8000/chat \
    -H "Content-Type: application/json" \
    -d "{\"message\": \"这是第 $i 条测试消息，我想咨询退款和订单问题\", \"user_id\": \"episodic_user\", \"conv_id\": \"episodic_session\"}" > /dev/null
done
```

查看情景记忆：

```bash
docker exec -it ai-helpdesk-app bash
```

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("episodic")

data = col.get(
    where={"user_id": "episodic_user"},
    include=["documents", "metadatas"],
)

for i, doc in enumerate(data["documents"]):
    print("=" * 80)
    print("metadata:", data["metadatas"][i])
    print("summary:", doc)
PY
```

### 9.8 查看 ChromaDB 持久化文件

ChromaDB 的持久化卷在 Compose 中定义为：

```yaml
volumes:
  chromadb-data:
```

查看 Docker volume：

```bash
docker volume ls | grep chromadb
docker volume inspect ai-helpdesk_chromadb-data
```

查看容器内数据目录：

```bash
docker exec -it ai-helpdesk-chromadb sh
ls -lah /chroma/chroma
find /chroma/chroma -maxdepth 2 -type f | head
```

注意：不建议直接修改这些底层文件。查看和管理数据应优先使用 ChromaDB API 或 Python 客户端。

### 9.9 清空 ChromaDB 数据

谨慎操作。停止服务并删除 volume：

```bash
docker compose down
docker volume rm ai-helpdesk_chromadb-data
docker compose up -d --build
```

如果只想删除某个 collection，可以用 Python 客户端：

```bash
docker exec -it ai-helpdesk-app bash
```

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
client.delete_collection("knowledge_base")
print("deleted knowledge_base")
PY
```

删除后重启应用，`KnowledgeBase` 会在 collection 为空时重新导入默认文档。

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

工作记忆压缩发生在 `memory/conversation_memory.py` 中。默认配置：

```text
WORKING_MAX = 20
COMPRESS_AT = 15
```

当同一个 `user_id + conv_id` 的工作记忆达到 15 条消息时，系统会：

```text
旧消息 -> LLM 摘要 -> Redis summary
旧消息摘要 -> ChromaDB episodic
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
- 压缩后 Redis 工作记忆列表只保留最近 5 条；更早的内容会以摘要形式进入 Redis summary 和 ChromaDB `episodic`。

### 11.3 查看 ChromaDB 中的情景记忆摘要

如果是全栈部署，应用容器名通常是：

```text
ai-helpdesk-app
```

进入应用容器：

```bash
docker exec -it ai-helpdesk-app bash
```

如果你是用 `docker run --rm` 跑 CLI，容器名可能是随机的。先查看：

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Networks}}\t{{.Status}}'
```

进入对应容器：

```bash
docker exec -it <容器名> bash
```

执行 Python 脚本查询 `episodic`：

```bash
python - <<'PY'
import chromadb

user_id = "cli_user"
conv_id = "5a076f2b-b607-4339-9e9f-f0399862d366"

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("episodic")

data = col.get(
    where={"user_id": user_id},
    include=["documents", "metadatas"],
)

for i, doc in enumerate(data["documents"]):
    meta = data["metadatas"][i]
    if meta.get("conv_id") == conv_id:
        print("=" * 80)
        print("metadata:", meta)
        print("summary:", doc)
        print("full_text_preview:", meta.get("full_text"))
PY
```

字段说明：

| 字段 | 含义 |
|------|------|
| `documents[i]` | LLM 生成的历史对话摘要 |
| `metadata.user_id` | 用户 ID |
| `metadata.conv_id` | 会话 ID |
| `metadata.ts` | 写入时间 |
| `metadata.full_text` | 被压缩的原始旧消息前 500 字预览 |

### 11.4 如果只想看某个用户的所有情景记忆

```bash
docker exec -it ai-helpdesk-app bash
```

```bash
python - <<'PY'
import chromadb

user_id = "cli_user"

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("episodic")

data = col.get(
    where={"user_id": user_id},
    include=["documents", "metadatas"],
)

for i, doc in enumerate(data["documents"]):
    print("=" * 80)
    print("metadata:", data["metadatas"][i])
    print("summary:", doc)
PY
```

### 11.5 Redis summary 和 ChromaDB episodic 的区别

| 位置 | 保存内容 | 用途 |
|------|----------|------|
| Redis `summary:{user_id}:{conv_id}` | 当前会话压缩摘要 | 下一次同会话请求直接拼入 prompt |
| ChromaDB `episodic` | 压缩摘要 + metadata | 跨会话按语义检索相关历史 |
| Redis `wm:{user_id}:{conv_id}` | 最近 5 条消息 | 保持当前对话连贯性 |

## 12. Monitor 在线监控

查看监控摘要：

```bash
curl http://localhost:8000/monitor
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
curl -X POST http://localhost:8000/eval/run
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
- ChromaDB 是否健康
- 应用容器是否正在反复重启

### 15.2 ChromaDB 连接失败

查看 ChromaDB 状态：

```bash
docker compose ps chromadb
docker compose logs -f chromadb
curl http://localhost:8001/api/v1/heartbeat
```

应用容器内测试：

```bash
docker exec -it ai-helpdesk-app bash
python - <<'PY'
import chromadb
client = chromadb.HttpClient(host="chromadb", port=8000)
print(client.heartbeat())
PY
```

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
curl http://localhost:8000/knowledge/stats
```

如果是 0，可以重新导入演示文档：

```bash
curl -X POST http://localhost:8000/knowledge/upload \
  -F "file=@data/demo_docs/sample_knowledge.json"
```

再测试：

```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "API如何接入", "top_k": 3}'
```

### 15.5 用户画像查不到

用户画像是异步更新的，并且依赖 LLM 调用成功。排查步骤：

1. 先调用 `/chat`，使用固定 `user_id`
2. 等待几秒
3. 查看 `docker compose logs -f ai-helpdesk` 是否出现 `用户画像已更新`
4. 使用第 8.6 节的 Python 脚本查询 `user_profile`

### 15.6 情景记忆查不到

情景记忆不是每次对话都写入。只有当前会话消息数达到压缩阈值后才写入。默认阈值：

```text
MemoryManager.COMPRESS_AT = 15
```

连续发 16 条以上消息后再查看 `episodic`。

## 16. 推荐验证流程

完整验证可以按这个顺序执行：

```bash
# 1. 启动
docker compose up -d --build

# 2. 健康检查
curl http://localhost:8000/health

# 3. 主对话
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好，我想了解退款政策", "user_id": "demo_user", "conv_id": "demo_conv"}'

# 4. 知识库统计
curl http://localhost:8000/knowledge/stats

# 5. 导入演示知识库
curl -X POST http://localhost:8000/knowledge/upload \
  -F "file=@data/demo_docs/sample_knowledge.json"

# 6. 检索
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "ai-helpdesk如何接入API", "top_k": 3}'

# 7. 监控
curl http://localhost:8000/monitor

# 8. Skills
curl http://localhost:8000/skills

# 9. 评测
curl -X POST http://localhost:8000/eval/run
```
