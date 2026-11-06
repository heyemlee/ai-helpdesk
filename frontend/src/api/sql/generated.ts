/* 生成的文件，别手改。改了 SQL Agent 的接口或 A2A 契约之后重新生成：
 *   (cd ai-sql-agent && PYTHONPATH=src:. .venv/bin/python scripts/export_openapi.py)
 *   node frontend/scripts/gen-sql-types.mjs
 * 来源：ai-sql-agent/openapi.json、a2a-contract/schema/*.schema.json。 */
/* eslint-disable */

// ── SQL Agent（OpenAPI 1.0.0）────────────────────────────────────────

/** BusinessEvidence */
export interface BusinessEvidence {
  as_of?: string | null
  as_of_source?: string | null
  database: string
  entity: Record<string, unknown>
  evidence_id: string
  field: string
  issues?: string[]
  query_hash: string
  row_index: number
  source_records?: Array<Record<string, unknown>>
  step_index: number
  unit?: string | null
  value: unknown
}

/** 用户对一次回答的判定。 */
export interface FeedbackRequest {
  comment?: string
  trace_id: string
  verdict: "correct" | "wrong" | "unsure"
}

/** FeedbackResponse */
export interface FeedbackResponse {
  certified?: boolean
  message?: string
  recorded: boolean
  trace_id: string
  verdict: string
}

/** HTTPValidationError */
export interface HTTPValidationError {
  detail?: ValidationError[]
}

/** 一条历史查询（不含结果集，结果不落审计）。 */
export interface HistoryItem {
  created_at: string
  databases?: string[]
  degraded?: boolean
  duration_ms?: number
  error?: string | null
  execution_status?: string
  failed_steps?: number
  has_risk?: boolean
  query: string
  repaired_steps?: number
  route?: string
  run_id?: string
  session_id?: string
  sqls?: Array<Record<string, string>>
  status_code?: number
  success?: boolean
  tables?: string[]
  total_tokens?: number
  trace_id: string
}

/** HistoryResponse */
export interface HistoryResponse {
  items?: HistoryItem[]
  user_id: string
}

/** PreviewFilterModel */
export interface PreviewFilterModel {
  column: string
  op: "eq" | "ne" | "contains" | "starts_with" | "gt" | "gte" | "lt" | "lte" | "is_empty" | "not_empty"
  value?: string | number | null
}

/** 一页预览。**没有 SQL 字段，也不会有**：能表达的只有「哪几行、怎么排」。 */
export interface PreviewRequest {
  filters?: PreviewFilterModel[]
  page?: number
  page_size?: number
  search?: string
  sort?: PreviewSortModel | null
  user_id?: string
}

/** PreviewSortModel */
export interface PreviewSortModel {
  column: string
  direction?: "asc" | "desc"
}

/** 一张表的概况。和预览一样只收「看哪些行」：搜索词与筛选条件，同义、同上限。 没有页码和排序——聚合结果与它们无关。 */
export interface ProfileRequest {
  breakdowns?: boolean
  filters?: PreviewFilterModel[]
  search?: string
  user_id?: string
}

/** 自然语言查询请求。 */
export interface QueryRequest {
  enable_long_term?: boolean
  query: string
  session_id: string
  user_id: string
}

/** QueryResponse */
export interface QueryResponse {
  answer: string
  as_of?: string | null
  as_of_source?: string | null
  confidence: number
  currency?: string | null
  degrade_reason?: string
  degraded?: boolean
  evidence?: BusinessEvidence[]
  execution_status?: string
  failed_steps?: number
  has_risk?: boolean
  observed_at?: string | null
  queried_database: boolean
  reason: string
  repaired_steps?: number
  result_truncated?: boolean
  route: string
  selected_databases?: string[]
  step_logs?: StepLogResponse[]
  steps?: StepLogResponse[]
  succeeded_steps?: number
  trace_id?: string
}

/** 用户主动保存结构化结果到个人知识库。 */
export interface SaveMemoryRequest {
  columns?: string[]
  database?: string
  query: string
  result: Record<string, unknown>
  sql?: string
  tables?: string[]
  user_id: string
}

/** StepLogResponse */
export interface StepLogResponse {
  as_of?: string | null
  as_of_source?: string | null
  column_units?: Record<string, string>
  database: string
  error?: string | null
  final_sql: string
  observed_at?: string | null
  repair_attempts?: Array<Record<string, unknown>>
  risk_findings?: Array<Record<string, unknown>>
  row_count: number
  row_limit?: number | null
  rows?: Array<Record<string, unknown>>
  source_records?: Array<Record<string, unknown>>
  sql: string
  success: boolean
  truncated?: boolean
}

/** ValidationError */
export interface ValidationError {
  ctx?: Record<string, unknown>
  input?: unknown
  loc: Array<string | number>
  msg: string
  type: string
}

// ── A2A 契约────────────────────────────────────────────────────

/** 契约 §4.4 / §4.6。message/send 与 message/stream **完全同构**，两条 transport 用同一份 schema——这正是 §4.4「调用方走 send 还是走 stream，能做的信任判断必须一模一样」的机器可校验版本。校验的对象是 parts[] 里 kind=="... */
export interface ArtifactDataPart {
  /** §4.6：只表示「产生了 pipeline result」（dynamic_service.py:34-36），**不保证执行成功**。缺失按 false 处置。 */
  queried_database: ArtifactDataPartStrictBoolean
  /** §4.6.7：**缺失按 true 处置**（缺失 = 不可信）。汇总自各步的 risk_findings，与 failed_steps 是两套计数。 */
  has_risk: ArtifactDataPartStrictBoolean
  /** §4.6.7：**缺失按 true 处置**。degraded=true 仍是 state:"completed"，理由见 §4.6.8。 */
  degraded: ArtifactDataPartStrictBoolean
  degrade_reason?: string
  trace_id: string
  route: string
  confidence?: number
  selected_databases?: DatabaseId[]
  execution_status: "success" | "partial_success" | "no_data" | "failed" | "not_applicable"
  succeeded_steps?: number
  failed_steps?: number
  repaired_steps?: number
  result_truncated?: ArtifactDataPartStrictBoolean
  as_of?: string | null
  as_of_source?: "query_time" | "snapshot" | "unknown" | null
  currency?: string | null
  steps: ArtifactDataPartStep[]
  observed_at: string | null
  evidence: ArtifactDataPartBusinessEvidence[]
}

export interface ArtifactDataPartBusinessEvidence {
  evidence_id: string
  entity: {
    kind: "query_row"
    query_hash: string
    row_index: number
  }
  field: string
  value: string | number | boolean | null
  unit: string | null
  as_of: string | null
  as_of_source: "query_time" | "snapshot" | "unknown" | null
  database: DatabaseId
  step_index: number
  row_index: number
  query_hash: string
  source_records: Array<Record<string, unknown>>
  issues: Array<"missing_unit" | "missing_time" | "missing_provenance" | "truncated">
}

export interface ArtifactDataPartStep {
  database: DatabaseId
  success: ArtifactDataPartStrictBoolean
  row_count: number
  truncated: ArtifactDataPartStrictBoolean
  row_limit?: number | null
  sql?: string
  final_sql?: string
  error?: string | null
  rows: Array<Record<string, unknown>>
  repair_attempts?: unknown[]
  risk_findings?: unknown[]
  column_units?: Record<string, string>
  source_records?: Array<Record<string, unknown>>
  as_of?: string | null
  as_of_source?: "query_time" | "snapshot" | "unknown" | null
  observed_at?: string | null
}

export type ArtifactDataPartStrictBoolean = boolean

/** 契约 §4.5（v1.6 起）。契约面是**形状**，不是闭集——具体有哪几个库随部署配置（AI_SQL_SEMANTIC_DIR）走，由服务端声明清单。新增标识符不算契约变更，删除/改名算。下面的 $defs 是断言用的具名集合，不是契约面本身。 */
export type DatabaseId = string

/** 契约 §4.1 / §4.3。校验对象是 `data: ` 后面那个完整的 JSON-RPC 2.0 响应对象，不是 result 里面那一层——信封本身也是契约面（§4.1「每个 SSE data: 承载一个完整的 JSON-RPC 2.0 响应，id 与请求一致」）。 */
export interface ProgressEvent {
  jsonrpc: "2.0"
  id: string | number
  result: ProgressEventStatusUpdateWorking
}

export interface ProgressEventStatusUpdateWorking {
  kind: "status-update"
  taskId: string
  contextId: string
  final: false
  status: {
    state: "working"
    timestamp: string
    message?: ProgressEventAgentMessage
  }
  metadata: ProgressEventProgressMetadata
}

export interface ProgressEventAgentMessage {
  kind: "message"
  role: "agent"
  messageId?: string
  taskId?: string
  contextId?: string
  parts: Array<{
    kind: "text"
    text: string
  }>
}

/** §4.3：metadata 放在**事件根**而不是 status.message.metadata 里——调用方读一跳就拿到。stage 与 trace_id 必填，其余可选且**调用方必须容忍缺失**。 */
export interface ProgressEventProgressMetadata {
  stage: Stage
  trace_id: string
  step?: number
  database?: DatabaseId
  dialect?: string
  with_memory?: boolean
  short_path?: boolean
}

/** 契约 §4.5。这是契约面，不是我方内部 stage 名；内部重构不得改变它，改它就是一次 §6 契约变更。 */
export type Stage = "normalization" | "routing" | "retrieval" | "planning" | "generation" | "execution" | "repair" | "validation" | "answering"

/** 契约 §4.2 / §4.4 / §5.1，v1.13 新增。v1.12 之前终态的形状只靠两侧各自的测试守着，没有 schema。校验对象和 progress-event 一样，是 `data: ` 后面那个完整的 JSON-RPC 2.0 响应对象。`state` 是**传输终态**：只回答「载荷有没有完整送... */
export interface TerminalEvent {
  jsonrpc: "2.0"
  id: string | number
  result: TerminalEventStatusUpdateFinal
}

export interface TerminalEventStatusUpdateFinal {
  kind: "status-update"
  taskId: string
  contextId: string
  final: true
  status: {
    state: TerminalEventTerminalState
    timestamp?: string
    message?: Record<string, unknown>
  }
  metadata: {
    trace_id: string
    error?: string
  }
}

/** §4.4 的三种传输终态。`canceled` 是 v1.13 新增的（A2A 0.2 规范 TaskState 本来就有这个取值，不越出 §10 的 0.2 子集）。拼写跟规范走，是 canceled，不是 cancelled。 */
export type TerminalEventTerminalState = "completed" | "failed" | "canceled"
