// 与后端 Pydantic 模型对齐的类型定义。
// 后端返回 snake_case，这里不做 camelCase 转换——保持字段名一致，
// 改后端模型时 tsc 能直接指出前端哪里没跟上。

export interface ChatRequest {
  message: string
  user_id: string
  conv_id?: string
}

/** 意图识别走的通道 */
export type IntentStrategy = 'fast_path' | 'ensemble'

/** 被答案策略拦截的一条表述 */
export interface PolicyViolation {
  rule: 'price_commitment' | 'lead_time_commitment' | string
  label: string
  evidence: string
  sentence: string
  reason: string
}

/**
 * 本轮放进上下文的一段知识。doc_id / chunk_id 能追回原文；
 * 没有文档归属的片段 doc_id 为空，这时只能显示片段自己的标题。
 */
export interface KnowledgeSource {
  doc_id: string
  chunk_id: string
  /** 片段标题（导入时填的，常常只是文件名） */
  title: string
  /** 所属文档的展示标题；老后端不返回 */
  doc_title?: string
  /** 第几片（从 0 起） */
  passage: number
  score: number | null
  /** dense / lexical / hybrid */
  retrieval: string
  excerpt: string
}

/** 这一轮知识检索走了哪条路。老后端没有这个字段，整个对象可能缺失。 */
export interface KnowledgeTrace {
  attempted?: boolean
  /** direct = 规则快通道直检；rewrite_rerank = 改写 + 并行召回 + 重排 */
  mode?: 'direct' | 'rewrite_rerank' | ''
  reranked?: boolean
  corrected?: boolean
  evidence_ok?: boolean | null
  /** 知识库不可用，上下文里只有一条兜底文案 */
  degraded?: boolean
}

/** 业务数据委派（SQL Agent）这一轮的结局。 */
export type BusinessDataOutcome =
  | 'not_configured' | 'skipped' | 'evidence' | 'unverified'
  | 'failed' | 'unavailable' | 'error' | 'not_applicable' | 'empty'
  /** 负载降级暂停了委派（排队模式，后端 app/runs/degrade.py 第 3 档起） */
  | 'paused'

export interface BusinessDataTrace {
  enabled?: boolean
  attempted?: boolean
  outcome?: BusinessDataOutcome | string
  databases?: string[]
  trace_id?: string
  execution_status?: string
  has_risk?: boolean
  degraded?: boolean
  reasons?: string[]
  reason?: string
}

export interface ChatResponse {
  conv_id: string
  response: string
  intent: string
  intent_group: string
  agent_type: string
  agent_types: string[]
  primary_agent: string
  supporting_agents: string[]
  routing_reason: string
  routing_confidence: number
  escalated: boolean
  latency_ms: number
  knowledge_used: boolean
  entities: Record<string, string[]>
  intent_confidence: number
  intent_source_scores: Record<string, number>
  intent_strategy: IntentStrategy
  intent_latency_ms: number
  handoff: boolean
  handoff_reason: string
  needs_clarification: boolean
  answer_confidence: number
  policy_violations: PolicyViolation[]
  /** 请求追踪 id，与响应头 X-Request-ID 一致，报障时携带 */
  request_id?: string
  /** 这一轮的 id，服务端生成。刷新之后按它取回这一轮的经过（GET /turns/{turn_id}）。老后端没有 */
  turn_id?: string
  /** 转人工时创建的工单 id；未转人工为 null */
  ticket_id?: string | null
  /** open = 工单已落库；not_created = 判了转人工但工单没建成；未转人工为空串 */
  ticket_status?: 'open' | 'not_created' | '' | string
  /** queued = 通知已入队（不等于送达）；not_configured = 没配通知接收方 */
  ticket_notification?: 'queued' | 'not_configured' | '' | string
  knowledge_sources?: KnowledgeSource[]
  knowledge?: KnowledgeTrace
  business_data?: BusinessDataTrace
  /**
   * 模型服务不可用，这一轮没有作答。和转人工是两件事：handoff 为 false、没有工单，
   * response 是一句固定的故障提示。老后端没有这两项。
   */
  service_unavailable?: boolean
  unavailable_reason?: LlmUnavailableReason | ''
  /**
   * 工具循环打开时才有（后端 agents/tool_loop.py）：这一轮的调用轨迹，规则预取的两条
   * （actor = rule）在前，专家作答时自己发起的（actor = model）在后。
   */
  trajectory?: TrajectoryEntry[]
  /** 同上：作答阶段的模型调用次数和 token。循环关着时是空对象 */
  answer_usage?: Partial<AnswerUsage>
  /** 排队模式才有：这一轮是哪个 run 的第几次执行 */
  run?: RunInfo
  /** 排队模式才有：这一轮按哪一档降级（level 0 是没降） */
  degrade?: DegradeInfo
}

/**
 * 排队模式（后端 AI_HELPDESK_RUN_MODE=queued，app/runs/）：一轮对话是一个 run，交给 worker 跑。
 * attempt 大于 1 说明之前处理它的 worker 失联了，换了一个接着跑；resumed_from 是"上一遍已经
 * 答完、这一遍只补收尾"时作答的那一轮；dead 是反复失联、转了人工。
 */
export interface RunInfo {
  run_id: string
  attempt: number
  resumed_from?: string
  dead?: boolean
}

/**
 * 按积压降级（后端 app/runs/degrade.py）。档位累加：1 关工具循环，2 检索不改写不重排，
 * 3 不委派查订单数据，4 这一轮不调模型。off 是这一轮关掉的环节。
 */
export interface DegradeInfo {
  level: number
  backlog: number
  threshold: number
  off: string[]
}

/** 工具循环里的一次工具调用。outcome 的取值随工具：知识库是 found / empty，业务数据同 BusinessDataTrace.outcome */
export interface TrajectoryEntry {
  /** 0 = 规则预取；1 起是作答的第几次模型调用 */
  step: number
  call_id: string
  actor: 'rule' | 'model'
  /** 发起调用的专家；规则预取为 null */
  agent: string | null
  tool: string
  args: Record<string, unknown>
  /** found / empty / evidence / unverified / failed / ... ；没有执行的是 refused / skipped / budget / invalid_args / unavailable_tool */
  outcome: string
  is_error: boolean
  /** 查回来的内容能不能当出处 */
  evidence: boolean
  /** 复用了本轮之前同样的查询（含规则预取），没有再查一次 */
  dedup: boolean
  latency_ms: number | null
  trace: Record<string, unknown>
}

export interface AnswerUsage {
  model_calls: number
  input_tokens: number
  output_tokens: number
  /** 被要求直接作答的原因：模型调用次数、查询次数或时间用完了；循环自己停下时没有 */
  forced?: 'model_calls' | 'tool_calls' | 'deadline'
  /** 模型连接不支持工具调用，这一轮退回了单次生成 */
  fallback?: 'tools_unsupported'
}

/**
 * 模型服务不可用的原因（后端 core/llm_guard 的 REASON_*）。前三种要等人处理完
 * 凭证或额度；rate_limited / forbidden 是有限退避，到期自己恢复；model_error 是
 * 这一次调用没成（超时、上游报错），和凭证无关。
 */
export type LlmUnavailableReason =
  | 'credential_rejected' | 'permission_denied' | 'quota_exhausted'
  | 'rate_limited' | 'forbidden' | 'model_error'
  /** 本地判的过载：等令牌等不起，或者负载降级到最后一档（后端 core/admission.py） */
  | 'overloaded'

// ── 对话审计日志（GET /conversations/{conv_id}）────────────────────────────
// 审计日志只留原文和作答 Agent。路由决策、置信度这些在事件日志里，按 turn_id 取
// （GET /turns/{turn_id}）；超出保留期或当时日志没开就取不到。

export interface ConversationEntry {
  seq: number
  conv_id: string
  user_id: string
  request_id: string
  role: 'user' | 'assistant' | string
  content: string
  agent_type: string
  created_at: number
  /** 指回那一轮的事件。加列之前的老记录、老后端为空 */
  turn_id?: string
  /** staff / customer；加列之前的老记录为空串 */
  instance?: string
}

// ── 一轮的经过（GET /turns/{turn_id}）──────────────────────────────────────
// 后端从事件投影出来的三个对象，和当轮 /chat/stream 的 meta、routing、result 同一个形状。

/**
 * 跑这一轮的进程没了，别人替它补了一条记录（排队模式下 worker 被杀）：superseded 是第
 * by_attempt 次执行接了手，dead_letter 是这个 run 进了死信、转了人工。
 */
export interface TurnAbandoned {
  reason: 'superseded' | 'dead_letter' | string
  attempt?: number
  by_attempt?: number
}

export interface TurnProjection {
  turn_id: string
  /** 为假：中间丢过事件，或者这一轮没写完 */
  complete: boolean
  /** 原文已按保留期清空（或当时只记结构）：要原文的字段是空的 */
  redacted: boolean
  /** 这一轮没写完、但去向清楚（见 TurnAbandoned）。老后端没有这一项 */
  abandoned?: TurnAbandoned | null
  meta: Partial<StreamMetaEvent>
  routing: Partial<StreamRoutingEvent>
  result: Partial<ChatResponse>
}

export interface ConversationResponse {
  conv_id: string
  messages: ConversationEntry[]
}

// ── SSE 事件 ────────────────────────────────────────────────────────────────
// /chat/stream 的事件序列：progress* → meta → routing → delta* → result
//
// progress 在 meta 之前：准备阶段（记忆、意图、检索、委派查库）本身就要十几到
// 四十秒，以前这段是纯空白。委派走 A2A 时下游每进一个阶段推一条。

/** 准备阶段的进度。stage 是契约面的机器可读标识，label 是已经本地化好的文案。 */
export interface StreamProgressEvent {
  /** 下游透传的阶段名（契约 §4.5），或本系统自己的 `helpdesk.*` 阶段 */
  stage: string
  /** 后端给的文案。`helpdesk.*` 阶段前端按界面语言自己出文案，这个只作兜底 */
  label: string
  /** `helpdesk.gathering`：这一轮会不会检索知识库 / 委派查订单数据 */
  knowledge?: boolean
  order_data?: boolean
  step?: number | null
  database?: string | null
  trace_id?: string | null
  /** `helpdesk.tool_call` / `helpdesk.tool_result`：工具循环里的一次调用（同一个 call_id 一开一合） */
  tool?: string
  call_id?: string
  agent?: string | null
  /** tool_call：模型写的查询词（截到 120 字） */
  query?: string
  /** tool_result：结局，取值同 TrajectoryEntry.outcome */
  outcome?: string
  evidence?: boolean
  is_error?: boolean
  dedup?: boolean
  latency_ms?: number | null
}

export interface StreamMetaEvent {
  conv_id: string
  /** 老后端没有 */
  turn_id?: string
  intent: string
  intent_group: string
  intent_confidence: number
  intent_strategy: IntentStrategy
  intent_latency_ms: number
  intent_source_scores: Record<string, number>
  entities: Record<string, string[]>
  knowledge_used: boolean
  knowledge_sources?: KnowledgeSource[]
  knowledge?: KnowledgeTrace
  business_data?: BusinessDataTrace
  /** 排队模式才有，见 RunInfo / DegradeInfo */
  run?: RunInfo
  degrade?: DegradeInfo
}

/** 排队模式的第一帧：run_id 是断线之后续流的凭据（GET /runs/{run_id}/events） */
export interface StreamRunEvent {
  run_id: string
  conv_id: string
  status: string
  attempt: number
}

/** 排队模式：第几次执行从这一帧开始。attempt > 1 时，上一遍已经发出来的半截作废 */
export interface StreamAttemptEvent {
  run_id: string
  attempt: number
  reclaimed: boolean
  degrade?: DegradeInfo
}

export interface StreamRoutingEvent {
  primary_agent: string
  supporting_agents: string[]
  reason: string
  confidence: number
  domain_scores?: Record<string, number>
}

export interface StreamDeltaEvent {
  text: string
}

export type StreamResultEvent = Omit<
  ChatResponse,
  | 'intent' | 'intent_group' | 'intent_confidence' | 'intent_strategy'
  | 'intent_latency_ms' | 'intent_source_scores' | 'entities' | 'knowledge_used'
  | 'knowledge_sources' | 'knowledge' | 'business_data'
>

/** id 是 SSE 的 `id:`：排队模式下每帧都有，断线重连时带上最后一个（Last-Event-ID）接着读 */
export type StreamEvent = (
  | { event: 'progress'; data: StreamProgressEvent }
  | { event: 'meta'; data: StreamMetaEvent }
  | { event: 'routing'; data: StreamRoutingEvent }
  | { event: 'delta'; data: StreamDeltaEvent }
  | { event: 'result'; data: StreamResultEvent }
  | { event: 'error'; data: { detail: string } }
  | { event: 'run'; data: StreamRunEvent }
  | { event: 'attempt'; data: StreamAttemptEvent }
) & { id?: string }

// ── 监控 ────────────────────────────────────────────────────────────────────

export interface AgentStat {
  total: number
  success_rate: number
  avg_ms: number
  monitor_penalty: number
  routing_score: number
  specialist: boolean
  /** 对外展示的业务角色名，例如「收入与合规 Agent」 */
  display_name?: string
  model: string
}

export interface ToolStat {
  total: number
  success_rate: number
  avg_latency_ms: number
  consecutive_fails: number
  circuit_state: 'closed' | 'open' | 'half_open' | string
}

export interface RoutingConfig {
  specialists: string[]
  fallback_agent: string
  /** agent_type -> 对外角色名 */
  display_names?: Record<string, string>
  replicas_per_type: number
  handoff_thresholds: Record<string, number>
  clarify_confidence_threshold: number
  supporting_min_score: number
  supporting_min_ratio: number
  answer_policy: {
    enabled: boolean
    checked_total: number
    blocked_total: number
    block_rate: number
    blocked_by_rule: Record<string, number>
  }
  intent: {
    fine_grained_intents: number
    vote_weights: Record<string, number>
    embedding_backend: string
    fast_path_hits: number
    ensemble_hits: number
    fast_path_rate: number
    llm_calls_saved: number
    cache: { size: number; hits: number; misses: number; hit_rate: number }
  }
}

export interface MonitorSummary {
  agent_stats: Record<string, AgentStat>
  tool_stats: Record<string, ToolStat>
  routing?: RoutingConfig
  active_alerts: Array<{
    severity: string
    metric: string
    message: string
    value: number
    threshold: number
    ts: string
  }>
  suggestions: Array<{ title: string; action: string; priority: number }>
}

export interface HealthResponse {
  status: string
  agents: Record<string, AgentStat>
  routing?: RoutingConfig
  /** 按状态统计的工单数：open / assigned / resolved / cancelled / total */
  tickets?: Record<string, number>
  ticket_notifications?: Record<string, number>
  conversations?: { messages: number; conversations: number }
  storage_backend?: string
  retrieval?: { mode: string; rerank: string }
  auth?: { user_token: boolean; rate_limit_per_minute: number }
  memory?: { cross_session: boolean }
  /** 老后端没有下面两项 */
  llm?: {
    provider?: string
    model?: string
    /** 模型凭证不可用保护的状态：读的是进程里记下的结果，后端不为这个接口去调模型 */
    available?: boolean
    unavailable_reason?: LlmUnavailableReason | ''
    /** 有限退避还剩几秒；null = 不会自己恢复，要等人处理 */
    retry_after_s?: number | null
    /** 保护期间挡下、没有发出去的模型调用数 */
    blocked_calls?: number
  }
  capabilities?: { business_data: boolean; ticket_notifications: boolean }
  /** 工具循环开没开；tools_rejected = 模型网关拒绝过工具调用、实际退回了单次生成。老后端没有 */
  tool_loop?: { enabled: boolean; tools_rejected: boolean }
}

// ── 评测 ────────────────────────────────────────────────────────────────────

export interface EvalResultItem {
  test_id: string
  passed: boolean
  scores: Record<string, number>
  detail: string
  metadata: Record<string, unknown>
}

export interface EvalReport {
  release: string
  timestamp: string
  pass_rate: number
  total: number
  passed: number
  avg_scores: Record<string, number>
  regressions: string[]
  recommendations: string[]
  baseline: { release: string; timestamp: string }
  promoted: boolean
  results: EvalResultItem[]
  /** 模型服务不可用时评测中途中止：results 是中止前完成的那些，planned 是本来要跑的项数 */
  incomplete?: boolean
  incomplete_reason?: string
  planned?: number
}

// ── 知识库 ──────────────────────────────────────────────────────────────────

export interface SearchResultItem {
  title?: string
  content?: string
  /** 余弦相似度。null：Embedding 服务不可用，这次只走了关键词那一路，没有向量分 */
  score?: number | null
  fallback?: boolean
  /** 第几片（从 0 起） */
  chunk?: number
  retrieval?: string
  doc_id?: string
  chunk_id?: string
  doc_title?: string
}

export interface SearchResponse {
  query: string
  results: SearchResultItem[]
  reranked: boolean
  corrected?: boolean
  evidence_ok?: boolean | null
}

export interface KnowledgeStats {
  total_chunks: number
  total_documents?: number
  legacy_documents?: number
}

/**
 * 文档原文的可信程度。
 * original = 导入时存下的原文；legacy = 加文档表之前导入的，正文由当时的分片拼回；
 * legacy_partial = 历史分片不全，正文只有找得到的那几片。
 */
export type DocumentOrigin = 'original' | 'legacy' | 'legacy_partial'

export interface KnowledgeDocument {
  id: string
  /** 导入时填的标题（上传文件时是文件名） */
  title: string
  /** 正文第一行的标题；没有就等于 title */
  display_title: string
  /** 正文开头的摘录，不是模型概括 */
  excerpt: string
  origin: DocumentOrigin | string
  /** manual / upload / seed / legacy */
  source_type: string
  source_name: string
  /** 导入时切成了几片 */
  chunk_count: number
  /** 现在库里实际挂在它名下的片数 */
  chunks: number
  chars: number
  created_at: number
  /** 大类的 key，空串是未分类。加分类之前的后端不返回 */
  category?: string
  /** 只在带筛选词时返回 */
  match?: { title: boolean; snippet: string }
}

/** 知识库的一个大类。顺序由后端定（按客户从咨询到售后），界面照着列。 */
export interface KnowledgeCategory {
  key: string
  name: string
  name_en: string
  /** 这一类的篇数。文档列表里给的是满足筛选词的篇数 */
  documents: number
}

export interface KnowledgeCategoryOverview {
  categories: KnowledgeCategory[]
  uncategorized: number
}

export interface KnowledgeDocumentList {
  query: string
  documents: KnowledgeDocument[]
  /** 符合筛选条件（筛选词 + 分类）的文档数，不受分页影响 */
  matched: number
  total_documents: number
  legacy_documents: number
  total_chunks: number
  /** 下面三项加分类之前的后端不返回。category：当前只看哪一类，null 是全部 */
  category?: string | null
  /** 每个大类里满足筛选词的篇数，不受 category 影响 */
  categories?: KnowledgeCategory[]
  uncategorized?: number
}

export interface KnowledgePassage {
  id: string
  index: number
  chars: number
  content: string
  /** 有没有关键词索引 */
  indexed: boolean
  ingested_at: number
}

export interface KnowledgeDocumentDetail extends KnowledgeDocument {
  /** 存下来的全文 */
  content: string
  /** 去掉开头标题行之后的正文（标题在 display_title 里） */
  body?: string
  passages: KnowledgePassage[]
}

export interface ImportedDocument {
  id: string | null
  title: string
  /** created 新建；duplicate 已存在；restored 补全了一篇历史文档；empty 正文为空 */
  status: 'created' | 'duplicate' | 'restored' | 'empty' | string
  chunks: number
  /** 这篇文档现在所在的分类（已存在的那篇不会因为重新导入而挪走） */
  category?: string
}

export interface ImportResult {
  message: string
  added_chunks: number
  total_chunks: number
  /** 老后端不返回下面几项 */
  added_documents?: number
  documents?: ImportedDocument[]
  total_documents?: number
}

// ── SOP ─────────────────────────────────────────────────────────────────────

export interface Sop {
  name: string
  description: string
  agents: string[]
  keywords: string[]
  enabled: boolean
  content: string
  content_chars: number
  /** 相对 Skill 目录的路径 */
  source: string
  modified_at: number | null
}

export interface SopList {
  count: number
  sops: Sop[]
  errors: number
}

// ── 工单 ────────────────────────────────────────────────────────────────────

export type TicketStatus = 'open' | 'assigned' | 'resolved' | 'cancelled'

/** outbox 里那条通知记录。delivered 只说明 webhook 收下了，不说明有人读了。 */
export interface TicketNotification {
  status: 'pending' | 'delivered' | 'failed' | string
  attempts: number
  last_error: string
  updated_at: number
}

export interface Ticket {
  id: string
  conv_id: string
  user_id: string
  request_id: string
  reason: string
  intent: string
  urgency: string
  answer_confidence: number
  last_message: string
  status: TicketStatus | string
  created_at: number
  updated_at: number
  /** 老后端不返回：这时界面不出流转按钮 */
  allowed_transitions?: string[]
  /** null = 没有通知记录（没配，或没入队）；undefined = 老后端不报 */
  notification?: TicketNotification | null
  /** 转人工的那一轮；老工单、老后端为空 */
  turn_id?: string
}

export interface TicketList {
  tickets: Ticket[]
  stats: Record<string, number>
  notifications_enabled?: boolean
}

export interface TicketDetail extends Ticket {
  notifications_enabled?: boolean
}

// ── 模型连接（/admin/model）────────────────────────────────────────────────────

export type ModelProvider = 'openai' | 'anthropic'
export type ModelSource = 'page' | 'env'
/** 这个请求为什么不能改；空串表示能改。 */
export type ModelEditReason = '' | 'disabled' | 'read_only_instance' | 'not_local' | 'cross_origin'

export interface ChatModelStatus {
  source: ModelSource
  provider: ModelProvider
  base_url: string
  model: string
  key_set: boolean
  /** Key 的末四位；太短的 Key 不给 */
  key_hint: string
  saved_at: string
  saved_by: string
}

export interface EmbeddingModelStatus {
  source: ModelSource
  backend: 'remote' | 'local_ngram'
  /** 远端刚失败过、还在冷却期时为 false */
  available: boolean
  base_url: string
  model: string
  dim: number
  query_prefix: string
  key_set: boolean
  key_hint: string
  saved_at: string
  saved_by: string
}

export interface EmbeddingSwitch {
  state: 'idle' | 'running' | 'succeeded' | 'failed'
  target_model: string
  target_dim: number
  restoring: boolean
  total: number
  done: number
  error: string
  started_at: string
  finished_at: string
}

export interface ModelStatus {
  enabled: boolean
  editable: boolean
  reason: ModelEditReason
  chat: ChatModelStatus
  embedding: EmbeddingModelStatus
  switch: EmbeddingSwitch
  problems: Record<string, string>
  defaults: {
    chat: Record<ModelProvider, { base_url: string; model: string }>
    embedding: { base_url: string; model: string; dim: number }
  }
}

export interface ModelCheck {
  check: 'chat' | 'structured' | 'embedding'
  outcome: string
  status: number | null
  /** 服务商自己的那句报错，已去掉 Key */
  detail: string
  latency_ms: number
}

export interface ModelTestResult {
  ok: boolean
  checks: ModelCheck[]
  warnings: string[]
  /** 向量测试才有：模型实际输出的维度 */
  dim?: number
}

export interface ChatConnectionBody {
  provider: ModelProvider
  base_url: string
  /** 留空 = 沿用现在这一把（只在协议和主机都没换时允许） */
  api_key: string
}

export interface ChatModelBody extends ChatConnectionBody {
  model: string
}

export interface EmbeddingConnectionBody {
  base_url: string
  api_key: string
}

export interface EmbeddingModelBody extends EmbeddingConnectionBody {
  model: string
  /** 0 = 按测试时模型实际返回的维度 */
  dim: number
  query_prefix: string
}

/** 接口自己报的可用模型（GET /models），已按这一组筛过。读不到时 ok 为 false、给出结果代号。 */
export interface ModelListResult {
  ok: boolean
  models: string[]
  /** 接口一共报了多少个（筛选之前） */
  total: number
  outcome: string
  status: number | null
  detail: string
}
