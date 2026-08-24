# 架构图

README 使用手写的动态图 [architecture.svg](architecture.svg)；[Archify 总览图](diagrams/README.md) 保留可编辑 JSON、交互 HTML 和静态 SVG。
本页以 GitHub 原生支持的 Mermaid 补充请求链路、路由、存储与部署细节；修改架构时需同步更新两处。

## 1. 总体架构

```mermaid
flowchart LR
    U[员工] --> FE[员工工作台<br/>React + TS]
    C[客户端] --> NG[Nginx :80]
    FE -->|/api/* · SSE| APP[员工实例<br/>FastAPI]
    NG --> CUS[客户实例<br/>同一镜像]
    APP & CUS --> AUTH[HMAC 令牌校验 + 每用户限流]

    APP & CUS --> R[(Redis<br/>工作记忆 · 限流计数<br/>熔断与路由扣分共享状态)]
    APP & CUS --> PG[(PostgreSQL + pgvector<br/>知识向量 / 情景记忆 / 画像<br/>工单 / 审计日志)]
    APP & CUS --> LLM[[对话模型<br/>Anthropic / OpenAI 两套协议<br/>控制台可在线切换]]
    APP & CUS --> EMB[[Embedding / Rerank API<br/>没配时用本地向量 / LLM 重排]]
    APP -->|A2A message/stream<br/>或 HTTP /query| SQL[[ai-sql-agent<br/>规划 · 写 SQL · 执行 · 自修]]

    APP & CUS -->|/metrics| P[Prometheus]
    APP & CUS -->|OTLP trace| OC[OTel Collector] --> TP[Tempo]
    APP & CUS -->|JSON 日志| PT[Promtail] --> LK[Loki]
    P & TP & LK --> GF[Grafana]

    classDef store fill:#1e293b,stroke:#475569,color:#e2e8f0
    class R,PG store
```

客户实例和员工实例是同一个镜像，区别有两处：不委派 SQL Agent（`SQL_AGENT_URL` 留空），关掉跨会话记忆（它的 user_id 取自请求体）。委派走哪条传输由 `SQL_AGENT_TRANSPORT` 决定：`a2a` 走 `message/stream`，把下游每个阶段实时转给工作台；默认的 `query` 调同步的 `POST /query`，没有进度事件。

对外接口：`/chat`、`/chat/stream`、`/auth/token`、`/search`、`/skills`、`/sops`、
`/knowledge/*`、`/tickets`、`/conversations/{conv_id}`、`/eval/run`、`/eval/promote`、`/admin/model*`、`/health`、`/monitor`、`/metrics`。

## 2. `/chat` 主链路

```mermaid
flowchart TD
    A[请求进入] --> A2[HMAC 令牌推导 user_id<br/>每用户限流]
    A2 --> B[读取三级记忆]
    B --> C{意图识别}
    C -->|规则锚点命中且不冲突| D[快通道<br/>零模型调用]
    C -->|否则| E[三路投票<br/>规则 10% + Embedding 20% + LLM 70%]
    D --> F[按意图和实体决定这一轮用哪些工具<br/>纯规则判断]
    E --> F
    F --> G[知识检索]
    F --> W[委派 SQL Agent 查业务数据<br/>A2A 或 /query]
    G -->|快通道| G1[直接检索<br/>跳过改写与重排]
    G -->|完整链路| G2[改写成三个子查询 → 并行召回 → 去重<br/>→ cross-encoder 重排，没配时 LLM 重排]
    G2 --> G3{依据够吗<br/>LLM 判定或分数阈值}
    G3 -->|不够| G4[针对缺口再检索一轮]
    W --> W1[证据判定<br/>success/partial 且无风险、无降级才算证据]
    G1 & G3 & G4 & W1 --> J[领域打分路由]
    J --> K{置信度够吗}
    K -->|意图落到 OTHER 且置信度低| L[澄清追问]
    K -->|够| M[主专家 + 辅助专家并行执行<br/>按路由分选副本，注入各自的 SOP<br/>工具循环打开时可自己补查，每轮最多 3 次模型调用]
    M --> N[答案策略守卫逐句检查<br/>无依据的价格/时效承诺替换成安全说法]
    N --> O{专家声明要人工？<br/>三路置信度都达标？}
    O -->|要人工 / 不达标| P[转人工 → 建工单<br/>四态状态机 + outbox 通知]
    O -->|否| Q[返回回复]
    Q --> S[写入记忆 · 异步更新画像]
    S --> S2[对话审计原文留档]
    S2 --> T[Monitor 采集 → 路由扣分写回]
```

知识检索和委派查库在专家运行之前就并发跑完，查不查由规则决定。工具循环（`AI_HELPDESK_TOOL_LOOP`，默认关）打开后，专家读完这些资料还缺东西时，可以通过 function calling 自己再调同样的两个工具：每个专家每轮最多 3 次模型调用、1 次由模型发起的委派，模型只填查询词、身份由服务端注入，报价意图不许查库，和预取相同的查询直接复用，墙钟上限按最慢工具的超时推算，规则预取那次委派已经超时就不再委派；工具的任何失败都作为结果交还模型。关着时每一轮的模型调用次数可预期。开放式的循环（规划 SQL、出错自修）在 SQL Agent 里，由钉住版本的契约约束。模型服务不可用时这一轮返回"没有作答"，不建工单。

轨迹 A/B（[评测记录](evaluation/tool-loop-ab.md)）第二轮 10 道打开门槛全过：规则漏掉数据的 B 类问题从 0/30 轮拿到数据变成 26/30，没有越权调用、没有编造数字，所以员工实例上打开了循环，客户实例保持关闭。

## 3. Agent 阵容与路由

```mermaid
flowchart TD
    I[意图 + 实体 + 关键词] --> S[领域打分]

    S --> T["产品与安装 Agent<br/>TechnicalAgent"]
    S --> B["报价与账款 Agent<br/>BillingAgent"]
    S --> O["订单履约 Agent<br/>OrderAgent"]
    S -.三个专家都不达标.-> G["展厅接待 Agent<br/>GeneralAgent（兜底）"]

    T -.执行失败.-> G
    B -.执行失败.-> G
    O -.执行失败.-> G

    S -.明确要求人工 / CRITICAL.-> H["运营升级通道<br/>不是 LLM Agent"]

    subgraph SK[按 Agent 类型隔离注入的 SOP]
        SK1[technical_support/SKILL.md]
        SK2[billing_support/SKILL.md]
        SK3[order_logistics/SKILL.md]
        SK4[general_customer_service/SKILL.md]
    end
    SK1 -.-> T
    SK2 -.-> B
    SK3 -.-> O
    SK4 -.-> G
```

要点：

- **三个专家按领域分竞争主 Agent**，General 只拿固定基准分，不参与专家域竞争。
- 跨域问题里分数不低于 0.45、且不低于主专家 55% 的其他专家升为**辅助 Agent**，与主 Agent 并行执行后合并结果。并行的结果一次性返回，单个专家的回答才逐句流式输出。
- 每种类型默认起两个实例（`AI_HELPDESK_AGENT_REPLICAS`）。路由分 = (成功率 × 0.7 + 延迟分 × 0.3) × (1 − 监控扣分)；成功率低于 90% 或变慢时 Monitor 扣分，最多 0.9，流量自动倒向同类型的健康实例，同分时轮询。
- 专家在回答末尾用 `[[handoff:yes|no]]` 声明要不要人工，这个标记在流里就被摘掉；声明要人工时自评置信度降到 0.65，低于 0.70 的门槛。标记缺失时退回关键词判断。
- 流式输出到一半失败时不降级重跑（已经发出去的内容收不回来），如实报告没有答完。
- Skill 按 `agents` 和 `keywords` 匹配，避免不同领域的规则互相污染。

## 4. 数据与存储

```mermaid
flowchart LR
    subgraph Redis
        WM[工作记忆<br/>当前会话最近消息<br/>TTL 24h]
        SUM[会话摘要<br/>压缩后写入]
        CB[熔断状态 · 路由惩罚<br/>限流计数（多实例共享）]
    end
    subgraph PG[PostgreSQL + pgvector]
        KB[(kb_chunks<br/>RAG 知识库切片向量)]
        EP[(episodic<br/>历史会话摘要)]
        UP[(user_profile<br/>用户画像，每用户单条)]
        TK[(tickets<br/>人工工单，四态)]
        CL[(conversation_log<br/>审计原文 append-only)]
    end
    subgraph 文件
        SK[skills/*/SKILL.md<br/>可热加载]
        BL[data/eval/baseline.json<br/>评测基线]
    end

    WM -->|满 15 条触发 LLM 摘要压缩| SUM
    SUM --> EP
```

## 5. 监控与评测闭环

```mermaid
flowchart LR
    AG[Agent 实例] -->|成功率 / 延迟| MON[Monitor]
    TL[工具] -->|熔断状态 / 连续失败| MON
    MON -->|Z-score 异常检测| AL[告警]
    MON -->|monitor_penalty| RT[路由评分]
    RT -->|绕开劣化实例| AG

    EV[/eval/run/] --> ORC[真实调用编排器]
    ORC --> JG[LLM-as-Judge<br/>相关性/准确性/完整性/有用性]
    JG --> CMP{对比存档基线}
    CMP -->|退化超阈值| REG[回归项]
    CMP -->|显式 promote 才更新| BL[(baseline.json)]
```

**基线只在显式 `/eval/promote` 时更新**——每跑完一次就覆盖的话，
"相比基线退化"永远为空。

**工具循环另有一套轨迹评测**（`scripts/run_trajectory_eval.py`）：40 条用例，每条只做一次准备阶段，
规则组和循环组从同一个请求分别作答，订单数据走读 a2a-contract 夹具的桩。打分是确定性规则
（资料到没到手、该调的调了没有、参数对不对、多余调用、编造数字），可选再加 LLM 评审，
按 6 道门槛决定开不开。线上由 Prometheus 的 `agent_loop_*` 指标看每轮的模型调用次数、
每个工具的结局、被要求直接作答和退回单次生成的次数。

## 6. 部署结构

```mermaid
flowchart TD
    subgraph compose[docker compose up]
        FE[ai-helpdesk-frontend<br/>127.0.0.1:5174]
        APP[ai-helpdesk-app 员工实例<br/>127.0.0.1:8080→8000]
        CUS[ai-helpdesk-customer 客户实例<br/>127.0.0.1:8081→8000]
        NG[ai-helpdesk-nginx<br/>:80]
        R[redis]
        PG[postgres<br/>pgvector 扩展]
        P[prometheus<br/>:9090]
        OC[otel-collector]
        TP[tempo]
        LK[loki]
        PT[promtail]
        GF[grafana<br/>:3000]
    end
    FE -->|/api/ 走内网| APP
    NG --> CUS
    APP & CUS --> R
    APP & CUS --> PG
    APP & CUS --> OC --> TP
    APP -.->|host.docker.internal:8000| SQL[ai-sql-agent<br/>同机另一个服务]
    PT --> LK
    P & TP & LK --> GF
```

共 12 个服务。只有 Nginx 的 80 端口对外，其余端口只绑本机：员工实例和工作台背后没有 API key，不能直接开放给其他机器。两个实例共用 Redis 和 PostgreSQL，并挂同一个模型设置目录：员工工作台保存模型连接后，客户实例几秒内跟上。观测栈（Collector / Tempo / Loki / Promtail / Grafana）与业务无耦合，不需要时删掉对应 service 即可，应用侧只是少了 trace 上报目标。

挂载 `./backend/skills`（热加载 SOP）、`./backend/data/eval`（评测基线）、`./backend/data/runtime`（模型设置）、
`./backend/logs`（供 Promtail 采集）。

前端容器的 Nginx 用变量 + `resolver` **在请求时**解析后端地址而不是启动时：
否则后端还没就绪，前端容器会直接 `[emerg] host not found` 起不来，
后端重启换 IP 后又会一直打到旧地址。
