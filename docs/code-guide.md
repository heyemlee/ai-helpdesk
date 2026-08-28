# 代码导读

这份文档只给**位置和取舍理由**，不内嵌代码片段。

早期版本贴了 28KB 的代码进来，结果是每次改实现文档就腐烂一次，读者拿文档对照
真实代码只会发现处处对不上——那比没有文档更糟。下面每条都指向文件和符号名，
用 IDE 跳过去看的永远是当前实现。

## 1. 意图识别 · `backend/app/core/intent_recognizer.py`

| 看什么 | 符号 |
|---|---|
| 18 个细粒度意图 | `IntentCategory`、`FINE_GRAINED_INTENTS` |
| 零延迟规则通道 | `IntentRecognizer._fast_path`、`_FAST_PATH_RULES` |
| 三路加权投票 | `IntentRecognizer._vote`、`_VOTE_WEIGHTS` |
| Embedding 一路 | `_embedding_recognize`、`_embed_text`、`_local_embedding` |
| 实体提取 | `_extract_entities`、`_ITEM_CODE_RE` |

**为什么快通道要求"锚点不冲突"**：`_fast_path` 在命中多个不同意图的锚点时会
主动放弃，交给完整融合。"门板有划痕而且还少发了一块"这类复合诉求，规则没有能力
判断哪个是主诉求，硬判会错得很自信。

**为什么 Embedding 不能用 base_url 是否为空来开关**：早期版本这么写，配了
Anthropic 协议网关就等于永久关掉 Embedding，"三路融合"实际只剩两路。现在这一路
始终参与投票，配了 OpenAI 兼容端点走远端向量，没配就退化为本地字符 n-gram
向量——换供应商是改环境变量的事，链路在任何配置下都成立。

**为什么错误码正则要收紧**：`[A-Z][A-Z0-9_-]{2,16}` 会把任意大写单词当错误码，
"I NEED HELP" 命中 NEED / HELP 之后给技术域加分，直接导致误路由。

## 2. 答案策略守卫 · `backend/app/core/answer_policy.py`

| 看什么 | 符号 |
|---|---|
| 四条判定 | `AnswerPolicy._check_sentence` |
| 出处比对 | `AnswerPolicy._is_grounded` |
| 日期按同一天比对 | `_DATE_FORMS`、`_dates_in`（`_is_grounded` 里先比日期） |
| 流式版本 | `PolicyStreamGuard`；工具循环里查回的出处当场加进去（`add_sources`） |

**为什么要四条同时成立**（金额或时效 + 承诺动词 + 无对冲词 + 无出处）：
少任何一条都会误伤。只看"有数字"会把"订单号 12345"当承诺；不看对冲词会把
SOP 要求的"通常一周左右，以工厂排产为准"也拦下来。

**为什么出处比对不做宽松匹配**：`_is_grounded` 要求数字和单位在出处里**连在一起**
出现。知识库写"补件 7 个工作日发出"时，模型说"4 个工作日就能送到"必须判为
无依据——数字跨条目张冠李戴，正是这类承诺出事的主要方式。

**为什么日期按同一天比**：业务系统给的是 2026-10-28，模型常写成"10 月 28 日"。按字面比，
有出处的日期会被当成无依据，整句换成"不能承诺具体日期"，客户反而看不到系统里的日期。
两边都写了年份时年份也要一致，月和日必须来自出处里的同一个日期。

**为什么是纯规则**：守卫在每次回复后同步执行。用 LLM 判断会让每次回复多一次
往返，把流式的首字延迟也拖垮。

## 3. Agent 编排 · `backend/app/agents/agent_orchestrator.py`

| 看什么 | 符号 |
|---|---|
| 领域打分 | `AgentOrchestrator._domain_scores` |
| 主辅决策 | `_route_decision`、`RoutingDecision` |
| 转人工判定 | `_handoff_signals`、`_handoff_result` |
| 兜底降级 | `_execute` |
| 性能路由 | `_best_agent` |
| 流式 | `run_stream`、`BaseAgent.stream` |
| 工具循环（可选） | `BaseAgent._stream_with_tools`（非流式的 `_handle_with_tools` 也走它）、`app/agents/tool_loop.py::run_tool_loop` |
| 一次工具调用的去向 | `tool_loop._plan_call`：工具在不在 → 参数校验 → 策略门 `refuse` → 去重 → 已知查不成 `skip` → 预算 |
| 墙钟上限 | `tool_loop.deadline_for`、`deadline_warnings` |
| 两个工具和规则预取 | `app/tools/turn_tools.py`：`KnowledgeSearchTool`、`OrderDataQueryTool`、`seed_from_prefetch` |

**为什么领域分要截断到 [0,1]**：辅助 Agent 的门槛之一是"达到主 Agent 分数的
一定比例"。不截断的话主 Agent 分数可以无限累加，跨域工单里的第二个域永远够不到
门槛，多 Agent 协作就成了永不执行的死代码。

**为什么 Agent 自评不问模型**："你有多确信"是不可校准的自述。`_self_confidence`
改用可观测信号折算：触发了几处策略拦截、有没有明说处理不了。

**为什么转人工由编排器直接产出文案**：转人工是确定动作，不需要再花一次模型调用
去生成一段"我帮您转人工"。`AgentType.HANDOFF` 因此不在 Agent 池里。早期版本把
它当占位放进路由表，结果 `primary_agent=ESCALATION` 但池里没有实例，静默降级到
GENERAL，响应里 `agent_type` 和 `primary_agent` 自相矛盾。

**为什么同分要轮询**：`_best_agent` 在分数打平时轮询而不是固定取第一个。冷启动
阶段所有实例分数都是 1.0，固定取第一个会让流量永远压在 0 号实例，其他实例的
统计永远是空的，Monitor 的反馈也就永远不产生任何路由差异。

**为什么非流式也走流式那条路**：带工具时中途会冒出新的出处、还要作废调工具前没写完的半句。
两条路径共用一份处理，"流式和非流式给出不同回答"在结构上就不可能。

**为什么墙钟上限按最慢工具的超时推算**：慢工具开跑前要剩下"它的超时 + 15 秒作答"。原来固定 75 秒，
`SQL_AGENT_TIMEOUT_S=90` 时这个条件永远满足不了：试跑里模型写对了查询词，每次都被预算拒绝。

**为什么规则预取的委派超时之后模型不再委派**：同一轮里再委派多半还要等满一次超时，一轮拖到三分多钟
还是查不到。只看 unavailable：failed、empty 是下游答了，换个查询词可能就对了。

**为什么流式失败不重跑**：`run_stream` 里 Agent 流式失败后不像非流式那样降级到
General 重试——已经吐出去的半截回答收不回来，重跑会让用户看到两段互相矛盾的内容。

**为什么没答上来不算转人工**：所有 Agent 都没答上来（流式失败、非流式连兜底也失败、
模型凭证被拒）时，结果是 `service_unavailable`：一句"没有作答、没有建工单"的提示，
`handoff=false`。转人工的意思是"判断过了，这件事需要人来处理"，会落工单；没答上来
是没能判断。两者混在一起，模型停机时每一条提问都会开一张业务工单。客户明确要求
人工走的是规则通道，不经模型，停机时照常转、照常建单。模型凭证被明确拒绝之后，
同一份配置的调用不再出网（`app/core/llm_guard.py`），专家被拒也不再拿同一把凭证
去试通用 Agent。

## 4. 工具治理 · `backend/app/tools/tool_manager.py`

| 看什么 | 符号 |
|---|---|
| 调用生命周期 | `ToolManager.call` |
| 检索优化链路 | `search_with_rewrite`、`rewrite_query`、`_rerank` |
| 熔断 | `CircuitBreaker` |
| 降级 | `_fallback_result` |

**为什么 `_rerank` 返回二元组**：结果数不足 `top_k` 时它原样返回，此时不能标记
`reranked=True`。早期版本外层无条件标记，这个字段一直在说谎。

## 5. 三级记忆 · `backend/app/memory/conversation_memory.py`

| 看什么 | 符号 |
|---|---|
| 写入与压缩 | `add_message`、`_compress` |
| 情景记忆检索 | `_search_episodic` |
| 用户画像 | `update_profile`、`_get_profile` |

**为什么画像是每用户单条覆盖写**：早期版本按会话写多条，读取时取第一条，而当时的
向量库不保证返回顺序——docstring 写着"取最新一条"，代码做不到，可能返回半年前的画像。
现在向量存储换成 pgvector，画像按 user_id 主键 upsert，语义和实现一致。

**为什么同步向量调用要包线程池**：向量库客户端是同步实现，直接 await 不了。
不包的话一次检索会阻塞整个事件循环，并发请求全部排队。见 `asyncio.to_thread` 的调用点。

## 6. 评测 · `backend/app/evaluation/evaluator.py`

| 看什么 | 符号 |
|---|---|
| 四维打分 | `LLMJudge.judge`、`QualityScores` |
| 意图指标 | `IntentEvaluator.evaluate` |
| 回归检测 | `EndToEndEvaluator._detect_regressions` |
| 基线管理 | `_load_baseline`、`promote_baseline` |
| 工具循环的轨迹评测 | `app/evaluation/trajectory.py`：`TrajectoryHarness`、`grade`、`summarize`；脚本 `scripts/run_trajectory_eval.py` |

**为什么轨迹评测成对跑**：每条用例只做一次准备阶段，规则组和循环组从同一个请求作答，差异只来自循环，
意图投票这一步的随机性不会混进对比。打分是确定性规则（资料到没到手、该调的调了没有、多余调用、
编造），看轨迹就能判；LLM 评审只管回答质量那一道门槛。

**为什么基线不能自动更新**：早期版本每跑完一次就把报告写回基线文件，且优先和
本进程上一次运行比较。结果是"相比基线退化"永远为空——你每次都在和刚刚的自己比。

## 7. API 层 · `backend/app/api/main.py`

| 看什么 | 符号 |
|---|---|
| 共用准备阶段 | `_prepare_request`（实现在 `app/agents/turn_prep.py::prepare_turn`，两路预取在 `app/tools/turn_tools.py`） |
| 流式端点 | `chat_stream`、`_sse` |
| 鉴权 | `require_api_key`、`protect_chat` |
| 知识门控 | `_should_use_knowledge` |
| 前端挂载 | `_mount_frontend` |

**为什么流式和非流式共用 `_prepare_request`**：两条链路一旦各自构造上下文就会
漂移，最终表现为"同一个问题流式和非流式给出不同答案"。

**为什么静态资源挂载写在文件末尾**：Starlette 按注册顺序匹配，挂在 `/` 的
`StaticFiles` 会吃掉所有未匹配路径。提前挂载会遮蔽 `/chat` 等接口。

## 8. Skills · `backend/app/core/skill_loader.py`

| 看什么 | 符号 |
|---|---|
| 发现与解析 | `SkillManager._discover_files`、`_split_front_matter` |
| 匹配注入 | `Skill.matches`、`SkillManager.prompt_for` |
| 热加载 | `SkillManager.reload` ← `POST /skills/reload` |

front matter 刻意不用 PyYAML 解析——为一个只有五个字段的轻量配置格式增加运行时
依赖不划算。

## 9. 身份与限流 · `backend/app/core/auth.py`

| 看什么 | 符号 |
|---|---|
| 令牌签发与校验 | `TokenAuth.issue`、`TokenAuth.verify`、`_sign` |
| 每用户限流 | `RateLimiter` |

**为什么 user_id 不能信任请求体**：系统会把用户的历史摘要和长期画像注入上下文。
如果 user_id 由客户端指定，传 `user_id=alice` 就能读到别人的记忆——这是记忆能力
天然引入的越权读取面，必须在入口堵死。

**为什么不符即 401 而不是静默用令牌覆盖**：静默覆盖会让一次冒用尝试变成一次正常
请求，日志里什么都看不到。报错才能把攻击意图暴露出来。

**为什么用 `hmac.compare_digest`**：`==` 比较字符串会按字节短路，泄露"前几位对了"
这个信息，构成时序侧信道。

## 10. 多实例共享状态 · `backend/app/core/shared_state.py`

| 看什么 | 符号 |
|---|---|
| 共享读写与降级 | `SharedState`、`_degrade` |
| 分布式熔断 | `DistributedCircuitBreaker` |

**哪些状态必须共享，哪些刻意不共享**：熔断开合、路由惩罚、限流计数属于正确性
状态——实例 A 熔断某工具后实例 B 也该拒绝，这才是"保护下游"；限流按实例各算一份
等于额度被放大 N 倍。而意图缓存和工具 TTL 缓存属于性能优化，各存一份只影响命中率
不影响正确性，不值得为它们付出每次读缓存一趟 Redis 往返。

**为什么 Redis 不可用时降级而不是抛错**：可观测性和限流的降级损失，远小于因为
Redis 抖动导致整个服务不可用。

## 11. 存储层 · `backend/app/storage/`

| 看什么 | 符号 |
|---|---|
| 连接与建表 | `pg.py` |
| 双后端向量存储 | `vector_store.py` 的 `PgStore` / `SqliteStore` |
| 余弦检索 | `PgStore.kb_search`（`<=>` 运算符） |
| 审计留档 | `conversation_log.py` |
| 向量化 | `backend/app/core/embedder.py` |

**为什么维度不一致要 fail fast**：向量列维度建表时就固定了，而维度由当前 Embedder
决定（远端 1536 维、本地降级 256 维）。换了嵌入后端却不改表，数据库不会报错，
但检索结果完全无意义——这是最难排查的一类故障，应该在启动时就炸掉。

**为什么保留 SQLite 后端**：clone 下来不起任何容器就能跑通全链路。两个后端跑同一组
合同测试，保证行为一致；PG 合同测试在 CI 里通过 service container 始终执行。

**审计日志和记忆为什么是两个东西**：记忆面向模型，为下一轮组装上下文，允许压缩、
摘要、遗忘（24h TTL）；审计面向人和合规，原文 append-only，保留期由运维显式
`purge_before` 决定，服务不悄悄删数据。

## 12. 工单 · `backend/app/tickets/ticket_store.py`

| 看什么 | 符号 |
|---|---|
| 四态状态机 | `TicketStatus`、`TicketStore.transition` |
| 建单与通知 | `create`、webhook 触发 |
| 查询流转 | `POST /chat` 转人工路径、`GET /tickets`、`PATCH /tickets/{id}` |

**为什么转人工必须落库**：早期版本转人工只打一条 warning 日志，业务上等于把用户
丢在半路。现在建单带会话 ID，人工接手时能回查完整对话审计。

## 13. 可观测性 · `backend/app/core/telemetry.py`、`backend/app/core/metrics.py`

| 看什么 | 符号 |
|---|---|
| 自动埋点 | `telemetry.py` 对 FastAPI / httpx / redis / asyncpg 的 instrument |
| 业务 span | 意图识别、RAG 检索、Agent 执行、记忆读写四处 |
| 指标 | `metrics.py`：QPS、p95、LLM token 计量（含流式）、限流命中 |
| 日志关联 | `request_context.py` 注入 request_id / trace_id |

**为什么日志要带 trace_id**：只有指标时，你知道 p95 变慢了但不知道慢在哪一段。
日志里带上 trace_id 之后，Grafana 里从一条慢请求日志能直接跳到 Tempo 看完整调用链，
看清意图识别、检索、生成三段各花了多少时间。

**为什么流式也要计 token**：流式响应的 usage 在最后一个事件里，早期版本只在非流式
路径计量，导致成本看板系统性低估。

## 14. LLM 调用层 · `backend/app/core/llm_utils.py`、`backend/app/core/openai_compat.py`

| 看什么 | 符号 |
|---|---|
| 结构化输出统一入口 | `structured_parse`（带 schema）、`structured_create`（自由文本） |
| effort 统一下发 | `_with_effort`、`STRUCTURED_EFFORT` |
| 各处的返回形状 | `LLMIntentPayload`、`UserProfilePayload`、`QueryRewritePayload`、`RerankPayload`、`JudgePayload` |
| OpenAI 侧等价实现 | `openai_compat._Messages.parse` |
| 工具调用的互译 | `_openai_tool`、`_tool_messages`、`_MessageStream._collect_tool_calls` |
| 网关不支持工具 | `ToolsUnsupportedError`、`tools_rejected` |
| 缓存前缀边界 | `BaseAgent._system_blocks` |

**为什么不再手工从正文里抠 JSON**：原来五处都是
`raw.find("{") … json.loads(raw[s:e])`。它有三种失败方式——模型加了前言、
JSON 被 markdown 围栏包住、字段名或类型漂了——而三种的表现是同一个：
调用点 except 里的一次静默降级。换成结构化输出之后 schema 在服务端强制，
这三类失败不再成立，剩下的解析异常都是真异常。

**为什么删掉了 temperature 的探测降级**：SDK 1.x 把 `temperature/top_p/top_k`
从 `messages.*` 的签名里移除了，再传是 **TypeError** 而不是 400。原来那套探测
只认 400 加关键词，TypeError 会被调用点的 `except Exception` 吃掉后照样静默降级
——同一个坑换了个形状。所以不做兼容，一处都不传，确定性交给 schema。

**为什么 effort 定在 low**：意图分类、查询改写、重排、判分都是判别式任务。
留着默认的高 effort 不只是贵——推理块会先吃掉 `max_tokens`，正文没产出就又是
一次空正文降级，正是 `STRUCTURED_MAX_TOKENS` 那段注释描述的事故。

**为什么缓存标在 system 而不是用顶层 `cache_control`**：缓存是前缀匹配，
顺序 tools → system → messages。顶层自动缓存标的是最后一个可缓存块，
在这里就是每次都不同的 messages 尾部——那样等于每次写一份再也命中不了的新缓存，
还要按 1.25 倍写入价付钱。标在 system 上，边界正好落在"每次相同"与"每次不同"的交界。

**为什么流式的工具调用按 index → id → 上一个来拼**：OpenAI 的分片只有第一片带 id 和函数名，
后面的只带 arguments 的一截；有的网关不给 index。调用方和 Anthropic 一样只从
`get_final_message()` 取 `tool_use` 块，不用分供应商。网关明确拒绝 tools（400 里同时点名工具参数
和"不支持"）之后记住这份（地址, 模型），不再出网，这一轮退回单次生成。

**为什么 Anthropic 独有的旋钮在兼容层被丢掉而不是硬映射**：
`output_config`（effort）/ `cache_control` 在 OpenAI 侧要么没有对应物、要么只有
推理模型才认（`reasoning_effort` 传给非推理模型直接 400）。硬映射会把"省点钱"
换成"整条链路挂掉"；丢掉只影响成本，不影响产出形状。

## 15. 事件日志 · `backend/app/events/`

| 看什么 | 符号 |
|---|---|
| 一轮的记录器（contextvar，调用点只写一行） | `recorder.py`：`events()`、`bind()`、`TurnRecorder.emit` |
| 事件类型（固定集合，有测试列全） | `types.py`：`EVENT_TYPES`、`Event` |
| 模型调用在哪记、用途怎么标 | `model_calls.py`（套在 `core/llm_guard.GuardedLLMClient` 上）、`recorder.model_call_tag` |
| 结构和原文怎么分 | `shapes.py`：`tool_call_parts` / `record_from_event` |
| 攒批写、停机限时写完 | `writer.py`：`EventWriter` |
| 存储、按实例隔离、保留期 | `store.py`（`PgEventStore` / `MemoryEventStore`）、`retention.py` |
| 事件 → 响应（读接口、影子校验共用） | `projection.py`：`project_turn` |
| 重放和运维 | `replay.py`、`ops.py`、`scripts/replay_turns.py`、`scripts/turn_events.py` |

**为什么用 contextvar 而不顺着参数传**：发事件的有准备阶段、编排器、专家、API 层，打用途标签
的还有意图、检索、记忆、评审几处，而模型调用那一层是共享的客户端单例，拿不到请求对象。
asyncio 建任务时复制 context，准备任务、并行专家、轮次之后的画像更新都自动带着同一个记录器；
没绑定时 `events()` 返回什么都不做的那一个，调用点不用判空。

**为什么 `emit` 是同步的**：它在 `finally` 里、在流式生成器被关闭时也要能调（客户端中途
断开记 `turn_aborted`）。里面只做序列化（当场快照，之后谁改原字典都不影响已记的）和
`put_nowait`，出任何异常都只计数——记日志不能打断回答。

**为什么分结构和原文**：任何来自客户、模型、知识库、业务系统的文字只进 `body`，`data` 里只有
代码自己产生的枚举、数字、id 和哈希。`body` 到期清空之后，剩下的行里没有能指向人的东西；
有一条金丝雀测试守着这条线。

**为什么响应字段和事件要有"影子校验"**：事件从十几个节点发出，迟早有人改了响应、忘了事件。
每轮结束时拿内存里的事件投影一次，和真正发出去的响应比，对不上记
`turn_projection_mismatch_total`；八条路径的金标准序列测试也在比同一件事。

## 测试在哪

`backend/tests/` 下 1196 个测试（设了 `A2A_CONTRACT_DIR` 时契约夹具还会再展开一批），全部不发真实网络请求（PG 合同测试在 CI 的 pgvector
service container 上执行，本地无 `TEST_POSTGRES_DSN` 时跳过）。重点覆盖那些"看起来在工作
但实际没有"的路径：多实例轮询与 Monitor 惩罚是否真的改变路由选择、跨域工单能否
提名辅助 Agent、流式下策略守卫是否漏拦、回归检测是否真的在对比基线、
伪造 user_id 能否读到他人记忆、两个存储后端行为是否一致。
