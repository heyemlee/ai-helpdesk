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
}

/**
 * 模型服务不可用的原因（后端 core/llm_guard 的 REASON_*）。前三种要等人处理完
 * 凭证或额度；rate_limited / forbidden 是有限退避，到期自己恢复；model_error 是
 * 这一次调用没成（超时、上游报错），和凭证无关。
 */
export type LlmUnavailableReason =
  | 'credential_rejected' | 'permission_denied' | 'quota_exhausted'
  | 'rate_limited' | 'forbidden' | 'model_error'

// ── 对话审计日志（GET /conversations/{conv_id}）────────────────────────────
// 服务端只留原文和作答 Agent；路由决策、置信度这些只存在于当轮的响应里。

export interface ConversationEntry {
  seq: number
  conv_id: string
  user_id: string
  request_id: string
  role: 'user' | 'assistant' | string
  content: string
  agent_type: string
  created_at: number
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
}

export interface StreamMetaEvent {
  conv_id: string
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

export type StreamEvent =
  | { event: 'progress'; data: StreamProgressEvent }
  | { event: 'meta'; data: StreamMetaEvent }
  | { event: 'routing'; data: StreamRoutingEvent }
  | { event: 'delta'; data: StreamDeltaEvent }
  | { event: 'result'; data: StreamResultEvent }
  | { event: 'error'; data: { detail: string } }

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
  score?: number
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
  /** 只在带筛选词时返回 */
  match?: { title: boolean; snippet: string }
}

export interface KnowledgeDocumentList {
  query: string
  documents: KnowledgeDocument[]
  /** 符合筛选条件的文档数（不受分页影响） */
  matched: number
  total_documents: number
  legacy_documents: number
  total_chunks: number
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
}

export interface TicketList {
  tickets: Ticket[]
  stats: Record<string, number>
  notifications_enabled?: boolean
}

export interface TicketDetail extends Ticket {
  notifications_enabled?: boolean
}
