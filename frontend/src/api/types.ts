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
}

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
// /chat/stream 的事件序列：meta → routing → delta* → result

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
>

export type StreamEvent =
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
}

// ── 知识库 ──────────────────────────────────────────────────────────────────

export interface SearchResultItem {
  title?: string
  content?: string
  score?: number
  fallback?: boolean
}

export interface SearchResponse {
  query: string
  results: SearchResultItem[]
  reranked: boolean
}

export interface KnowledgeStats {
  total_chunks: number
}
