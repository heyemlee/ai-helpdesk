/**
 * SQL Agent 里"处理函数直接返回 dict"的那几组接口：数据浏览、运行记录、管理。
 *
 * 它们在 OpenAPI 里只是 `object`，生成不出类型（generated.ts 只有带模型的请求体和响应），
 * 所以在这里手写。形状以 ai-sql-agent/scripts/record_console_fixtures.py 录下来的真实响应为准：
 * __tests__/fixtures.test.ts 把每份夹具赋给这里的类型——服务端改了格式、夹具一重录，
 * 类型检查就报出来，不会出现"页面和测试一起通过一个不存在的格式"。
 *
 * 只写页面用得到的字段。服务端多给的字段照收不误（TS 的结构类型不管多出来的）。
 */
import type { PreviewFilterModel } from './generated'

export type PreviewOperator = PreviewFilterModel['op']
export type CellValue = string | number | boolean | null

export interface Identity {
  user_id: string
  caller: string
  is_admin: boolean
  can_impersonate: boolean
  anonymous: boolean
}

export interface ServiceInfo {
  version: string
  auth_mode: string
  feedback_enabled: boolean
  streaming: boolean
  catalog_enabled: boolean
  preview: {
    max_page_size: number
    max_offset: number
    max_filters: number
    max_value_length: number
    max_cell_chars: number
    max_search_columns: number
    /** 服务端认的筛选运算符；以后可能会多，页面只用它认识的那几个 */
    operators: string[]
  }
  profile: {
    enabled: boolean
    max_facets: number
    max_facet_values: number
    max_timelines: number
  }
}

export interface WhoAmI {
  identity: Identity
  service: ServiceInfo
}

/** 语义层声明的关联：同库外键，或"按命名约定指同一个东西"的跨库关联 */
export interface ColumnReference {
  relation: 'foreign_key' | 'cross_database' | string
  database: string
  table: string
  column: string
  note?: string
}

export type ColumnKind = 'text' | 'number' | 'date' | string

export interface CatalogColumn {
  name: string
  status: string
  documented: boolean
  data_type: string
  kind: ColumnKind
  nullable: boolean
  primary_key: boolean
  previewable: boolean
  aliases: string[]
  description: string
  usage: string
  role: string
  unit: string
  value_range: string
  references: ColumnReference[]
}

export interface CatalogTable {
  name: string
  status: string
  documented: boolean
  aliases: string[]
  description: string
  primary_key: string[]
  previewable: boolean
  /** 表很多的部署里 /catalog 不带字段（columns_included=false），按表从 /catalog/{库}/{表} 取 */
  columns?: CatalogColumn[]
}

/** 数据域的显示名：服务端预设了中英两份时是对象，否则是一个字符串 */
export type DomainTitle = { en: string; zh: string } | string

export interface CatalogDomain {
  database: string
  kind: string
  title: DomainTitle
  title_source: string
  aliases: string[]
  description: string
  description_en: string
  description_zh: string
  dialect: string
  documented: boolean
  preview_supported: boolean
  /** available | unavailable（库连不上时那个域照样列出来，带 status_reason） */
  status: string
  status_reason: string
  tables: CatalogTable[]
}

export interface Catalog {
  source: string
  domains: CatalogDomain[]
  access_limited: boolean
  columns_included: boolean
  glossary: Array<{ term: string; definition: string }>
  generated_at: string
  identity: Identity
  service: ServiceInfo
}

export interface RowsColumn {
  name: string
  kind: ColumnKind
  sortable: boolean
}

/** 一页预览。`as_of` 是读库的时刻，不是业务日期；业务日期看 date_ranges */
export interface RowsPage {
  database: string
  table: string
  columns: RowsColumn[]
  rows: CellValue[][]
  page: number
  page_size: number
  offset: number
  has_more: boolean
  total_rows: number | null
  date_ranges: Array<{ column: string; min: string; max: string }>
  columns_hidden: boolean
  columns_unsupported: string[]
  cells_truncated: boolean
  cell_limit: number
  stable_order: boolean
  search_columns: string[]
  search_columns_limited: boolean
  as_of: string
  as_of_source: string
  dialect: string
  sql: string
  trace_id: string
}

export interface ProfileFacet {
  column: string
  values: Array<{ value: CellValue; count: number }>
  distinct: number
  complete: boolean
  ordered_by: string
  own_filters_excluded: boolean
}

export interface ProfileTimeline {
  column: string
  grain: 'month' | 'year' | string
  buckets: Array<{ period: string; count: number }>
  unparsed: number
  truncated: boolean
  own_filters_excluded: boolean
}

/** 表概况：只有聚合结果，没有任何一行记录 */
export interface TableProfile {
  database: string
  table: string
  filtered: boolean
  total_rows: number
  columns: Array<{ name: string; filled: number }>
  columns_limited: boolean
  columns_hidden: boolean
  breakdowns: boolean
  facets: ProfileFacet[]
  timelines: ProfileTimeline[]
  skipped: Array<{ column: string; reason: string } | string>
  as_of: string
  as_of_source: string
  dialect: string
  sql: string[]
  trace_id: string
}

export type RunStatus = 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted' | string

export interface RunUsage {
  calls: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  by_component: Record<string, unknown>
}

/** 一次 run：/query、A2A 每次请求各一个；A2A 的 taskId 和 /query 的 X-Run-Id 就是它的 run_id */
export interface RunRecord {
  run_id: string
  trace_id: string
  entry: string
  status: RunStatus
  /** 关掉持久化时只有内存里的状态，下面这些字段都没有 */
  persisted: boolean
  caller?: string
  user_id?: string
  session_id?: string
  query?: string
  cancel_reason?: string | null
  error?: string | null
  created_at?: string
  updated_at?: string
  finished_at?: string | null
  last_seq?: number
  usage?: RunUsage
  outcome?: {
    route?: string
    degraded?: boolean
    degrade_reason?: string
    execution_status?: string
    steps?: number
    failed_steps?: number
    repaired_steps?: number
    has_risk?: boolean
  }
}

export type RunEventType =
  | 'run.started' | 'run.finished' | 'run.cancel_requested'
  | 'stage.started' | 'stage.finished' | 'model.call' | 'tool.sql'

export interface RunEvent {
  run_id: string
  seq: number
  ts: string
  /** 上面那几类；以后加了新类型，页面按"其它事件"显示，不报错 */
  type: RunEventType | string
  /** 服务端内部的阶段名（memory_context、sql_generation……），不是契约的 stage */
  stage: string
  step: number | null
  attrs: Record<string, unknown>
}

export interface RunEventsPage {
  run_id: string
  events: RunEvent[]
  next_after: number
}

export interface CancelResult {
  run_id: string
  status: string
}

export interface FeedbackStats {
  stats: {
    total: number
    correct: number
    wrong: number
    unsure: number
    unresolved_wrong: number
    wrong_with_risk: number
    wrong_without_risk: number
    accuracy: number | null
    silent_error_rate: number | null
  }
  recent_wrong: Array<{
    trace_id: string
    query: string
    comment: string
    had_risk: boolean
    resolved: boolean
  }>
}

export interface SchemaStatus {
  watcher_enabled: boolean
  interval_seconds: number
  pending_changes: unknown[]
  fingerprints: Record<string, string>
}

export interface Readiness {
  ready: boolean
  checks: Record<string, unknown>
}

// ── 模型连接（/admin/model …）：只有管理员拿得到；能改的请求才有地址、Key 末四位、谁存的 ─────────

export interface SqlProviderEntry {
  base_url: string
  official_base_url: string
  chat_model: string
  coder_model: string
  key_configured: boolean
  /** 现有那把 Key 交给的是哪台主机（协议 + 主机 + 端口）：同一台主机上换模型可以不重填 */
  key_address: string
}

export interface SqlAccessFault {
  permanent: boolean
  /** credential：这把 Key 被拒了；model：这把 Key 不能用这个模型 */
  scope: 'credential' | 'model' | string
  status_code?: number | null
  [key: string]: unknown
}

export interface SqlEmbeddingStatus {
  source: 'page' | 'server' | string
  /** local：本地哈希向量（没有语义）；remote：OpenAI 兼容的 /embeddings */
  backend: 'local' | 'remote' | string
  model: string
  dimensions: number
  key_configured: boolean
  /** 检索索引实际在用的：配的是远端、启动时它调不通的话，这里是 local */
  active: { backend?: string; model?: string; dimensions?: number }
  fallback: string
  base_url?: string
  endpoint?: string
  key_hint?: string
  reusable_key_addresses?: string[]
  saved_at?: string
  saved_by?: string
  store_problem?: string
  server?: { backend: string; model: string; dimensions: number }
  defaults?: { base_url: string; model: string; dimensions: number }
}

/** GET /admin/model。Key 本身从来不在里面 */
export interface SqlModelStatus {
  editable: boolean
  /** 不能改的原因：disabled（服务没开页面配置）/ local_only（没开鉴权，只能在本机改） */
  reason: string
  source: 'page' | 'server' | string
  provider: 'anthropic' | 'openai'
  chat_model: string
  coder_model: string
  key_configured: boolean
  embedding: SqlEmbeddingStatus
  base_url?: string
  endpoint?: string
  custom_endpoint?: boolean
  key_hint?: string
  saved_at?: string
  saved_by?: string
  store_problem?: string
  server?: { provider: string; chat_model: string; coder_model: string; key_configured: boolean }
  providers?: Record<'anthropic' | 'openai', SqlProviderEntry>
  access_faults?: SqlAccessFault[]
}

/** 保存 / 测试 / 读模型清单的请求体。api_key 留空 = 接着用现有的那把 */
export interface SqlModelBody {
  provider: 'anthropic' | 'openai'
  base_url: string
  api_key: string
  chat_model: string
  coder_model: string
}

/** Embedding 的请求体。dimensions 是 0 = 用默认值 */
export interface SqlEmbeddingBody {
  base_url: string
  api_key: string
  model: string
  dimensions: number
}

/** 连接测试的一项：对话模型和 SQL 模型同名时只测一次（roles 里两个都有） */
export interface SqlProbeResult {
  roles: string[]
  model: string
  ok: boolean
  outcome: string
  status_code: number | null
  message: string
  duration_ms: number
  dimensions?: number
}

export interface SqlProbeReport {
  ok: boolean
  endpoint: string
  provider?: string
  results: SqlProbeResult[]
  warnings: string[]
}

/** POST …/models：这个接口提供哪些模型（给下拉框）。读不到不算接口错误，outcome 说为什么 */
export interface SqlModelList {
  ok: boolean
  models: string[]
  total: number
  outcome: string
  status_code: number | null
  message: string
  duration_ms?: number
}

export interface SqlModelSaved {
  saved?: boolean
  cleared?: boolean
  warnings?: string[]
  status: SqlModelStatus
  /** Embedding 保存时：先测一次，再用新模型把检索索引整个重建 */
  test?: SqlProbeReport
  rebuild?: { texts: number; duration_ms: number }
}
