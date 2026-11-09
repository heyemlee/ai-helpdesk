/** SQL requests pass through the employee session gateway. */
import { authHeaders, sessionExpired, sessionStamp } from '../client'
import type {
  ArtifactDataPart, FeedbackRequest, FeedbackResponse, HistoryResponse, PreviewRequest,
  ProfileRequest, QueryResponse,
} from './generated'
import type {
  CancelResult, Catalog, CatalogTable, FeedbackStats, Readiness, RowsPage, RunEventsPage, RunRecord,
  SchemaStatus, TableProfile, WhoAmI,
} from './types'
import type {
  SqlEmbeddingBody, SqlModelBody, SqlModelList, SqlModelSaved, SqlModelStatus, SqlProbeReport,
} from './types'

export const SQL_API_BASE: string = import.meta.env.VITE_SQL_API_BASE_URL || '/sql-api'

/**
 * 每个请求都带的自定义头。控制台的 nginx 只给带着它的请求加 Key：别的网站借员工的浏览器发来的
 * 跨站请求要带自定义头，就得先过 CORS 预检，而 nginx 不回预检——这样挡住 CSRF。
 */
export const CONSOLE_HEADER = { 'X-Console-Request': '1' } as const

export class SqlApiError extends Error {
  /** 0：请求没到服务端（网络断了、服务没起、nginx 找不到 sql-agent 时是 502） */
  readonly status: number
  /** 服务端的错误码（数据浏览接口的 detail.code），或者本地判出来的：network、bad_response、stream_*… */
  readonly code: string
  /** 服务端给的原话；没有就是空，不拿错误码顶替 */
  readonly serverMessage: string
  readonly detail: unknown

  constructor({ status = 0, code = '', message = '', detail = null }: {
    status?: number; code?: string; message?: string; detail?: unknown
  }) {
    super(message || code || `HTTP ${status}`)
    this.name = 'SqlApiError'
    this.status = status
    this.code = code
    this.serverMessage = message
    this.detail = detail
  }
}

export const isAbort = (error: unknown): boolean =>
  error instanceof DOMException ? error.name === 'AbortError' : (error as { name?: string })?.name === 'AbortError'

function toError(status: number, payload: unknown): SqlApiError {
  const detail = payload && typeof payload === 'object' ? (payload as { detail?: unknown }).detail : null
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const { code = '', message = '' } = detail as { code?: string; message?: string }
    return new SqlApiError({ status, code, message, detail })
  }
  if (Array.isArray(detail)) {
    const message = detail.map((item) => (item as { msg?: string })?.msg).filter(Boolean).join('; ')
    return new SqlApiError({ status, code: 'invalid_request', message, detail })
  }
  return new SqlApiError({ status, message: typeof detail === 'string' ? detail : '', detail })
}

type Params = Record<string, string | number | boolean | undefined | null>

function withParams(path: string, params?: Params): string {
  if (!params) return path
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '' && value !== false) query.set(key, String(value))
  }
  const text = query.toString()
  return text ? `${path}?${text}` : path
}

async function request<T>(method: string, path: string, { params, body, signal, onResponse }: {
  params?: Params; body?: unknown; signal?: AbortSignal
  /** 要读响应头的调用方用（/query 的 X-Run-Id） */
  onResponse?: (response: Response) => void
} = {}): Promise<T> {
  const stamp = sessionStamp()
  let response: Response
  try {
    response = await fetch(SQL_API_BASE + withParams(path, params), {
      method,
      credentials: 'same-origin',
      signal,
      headers: body !== undefined ? { ...CONSOLE_HEADER, ...authHeaders(), 'Content-Type': 'application/json' } : { ...CONSOLE_HEADER, ...authHeaders() },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch (error) {
    if (isAbort(error)) throw error
    throw new SqlApiError({ code: 'network' })
  }

  onResponse?.(response)
  let payload: unknown = null
  let readable = true
  try {
    payload = await response.json()
  } catch (error) {
    // 读响应体的途中被取消，仍然是「取消」，不是服务出了问题
    if (isAbort(error)) throw error
    readable = false
  }
  if (!response.ok) { sessionExpired(response.status, stamp); throw toError(response.status, payload) }
  // 状态码是成功、响应体却不是 JSON（被代理换成了别的页面、中途断了）：不是一个可用的结果
  if (!readable) throw new SqlApiError({ status: response.status, code: 'bad_response' })
  return payload as T
}

const segment = encodeURIComponent

export const sqlApi = {
  health: () => request<{ status: string }>('GET', '/health'),
  ready: () => request<Readiness>('GET', '/readyz'),
  whoami: () => request<WhoAmI>('GET', '/whoami'),

  catalog: ({ refresh = false }: { refresh?: boolean } = {}) =>
    request<Catalog>('GET', '/catalog', { params: { refresh } }),
  table: (database: string, table: string) =>
    request<CatalogTable>('GET', `/catalog/${segment(database)}/${segment(table)}`),
  /** 一页真实数据。请求体里没有 SQL，也不会有：只有页码、搜索词、列名和筛选值，服务端按清单核对 */
  rows: (database: string, table: string, body: Omit<PreviewRequest, 'user_id'>, signal?: AbortSignal) =>
    request<RowsPage>('POST', `/catalog/${segment(database)}/${segment(table)}/rows`, { body, signal }),
  /** 表概况：只有聚合结果（行数、取值分布、按月的记录数），没有记录本身 */
  profile: (database: string, table: string, body: Omit<ProfileRequest, 'user_id'>, signal?: AbortSignal) =>
    request<TableProfile>('POST', `/catalog/${segment(database)}/${segment(table)}/profile`, { body, signal }),

  /** 最近的提问。不给 userId 是控制台自己（这把 Key 绑定的身份）的；管理员可以看别人的 */
  history: ({ limit = 50, userId = '' }: { limit?: number; userId?: string } = {}) =>
    request<HistoryResponse>('GET', '/history', { params: { limit, user_id: userId } }),
  run: (runId: string) => request<RunRecord>('GET', `/runs/${segment(runId)}`),
  runEvents: (runId: string, after = 0, limit = 1000) =>
    request<RunEventsPage>('GET', `/runs/${segment(runId)}/events`, { params: { after, limit } }),
  /** 请服务端停下一次正在跑的 run；已经结束会回 409，调用方不必理会 */
  cancelRun: (runId: string) => request<CancelResult>('POST', `/runs/${segment(runId)}/cancel`),

  /**
   * 同步问答（流式没开、或者流打不开时的退路）。run_id 在响应头 X-Run-Id 里——走 A2A 流时它是 taskId，
   * 两条路拿到的是同一个东西，都能链到 #/data/runs/<run_id>。
   */
  query: async (body: { query: string; user_id: string; session_id: string; enable_long_term?: boolean },
    signal?: AbortSignal): Promise<{ response: QueryResponse; runId: string }> => {
    let runId = ''
    const response = await request<QueryResponse>('POST', '/query', {
      body, signal, onResponse: (raw) => { runId = raw.headers.get('X-Run-Id') ?? '' },
    })
    return { response, runId }
  },
  feedback: (body: FeedbackRequest) => request<FeedbackResponse>('POST', '/feedback', { body }),

  feedbackStats: () => request<FeedbackStats>('GET', '/feedback/stats'),
  schemaStatus: () => request<SchemaStatus>('GET', '/admin/schema/status'),
  reloadSchema: () => request<Record<string, unknown>>('POST', '/admin/schema/reload'),
  resetCircuits: () => request<Record<string, unknown>>('POST', '/admin/circuit/reset'),
}

// ── A2A message/stream ──────────────────────────────────────────────────────

/** 一条进度：服务端真实发出的阶段事件，一条不多一条不少（契约 §4.3 / §4.5） */
export interface StreamStage {
  stage: string
  step: number | null
  database: string
  shortPath: boolean | null
  withMemory: boolean | null
  /** 距 task 帧的毫秒数；拿不到时间戳时是 null */
  offsetMs: number | null
}

/** 流的结果：artifact 的 data part（形状见契约 schema）加上 text part 的回答 */
export type StreamPayload = Partial<ArtifactDataPart> & Record<string, unknown> & { answer: string; trace_id?: string }

const uid = () => (globalThis.crypto?.randomUUID?.() ?? `${Date.now().toString(16)}${Math.random().toString(16).slice(2, 14)}`)
  .replace(/-/g, '')

interface Frame {
  kind?: string
  id?: string
  final?: boolean
  status?: { state?: string; timestamp?: string }
  metadata?: Record<string, unknown>
  artifact?: { parts?: Array<{ kind?: string; data?: Record<string, unknown>; text?: string }> }
}

/**
 * POST /a2a 的 message/stream，SSE 自己拆帧（EventSource 只能发 GET）。
 *
 * 网络分块不等于事件边界：一个事件、甚至一个 UTF-8 字符都可能跨块，所以解码器开 stream 模式，
 * 缓冲区按空行切。帧的顺序由契约保证：task → 若干 status-update(working) → artifact-update →
 * status-update(final)。收到载荷还要等到 completed 终态才算数；流中途断了就是没拿到结果。
 *
 * onTask 拿到的 taskId 就是服务端这次 run 的编号：停止按钮用它请服务端停下（POST /runs/{id}/cancel），
 * 页面也用它链到 #/data/runs/<run_id>。
 */
export async function streamQuery(question: string, { userId, sessionId, signal, onStage, onTask }: {
  userId: string
  sessionId: string
  signal?: AbortSignal
  onStage?: (stage: StreamStage) => void
  onTask?: (runId: string) => void
}): Promise<{ payload: StreamPayload; stages: StreamStage[]; runId: string }> {
  const stamp = sessionStamp()
  let response: Response
  try {
    response = await fetch(`${SQL_API_BASE}/a2a`, {
      method: 'POST',
      signal,
      headers: { ...CONSOLE_HEADER, ...authHeaders(), 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: uid(),
        method: 'message/stream',
        params: {
          message: {
            kind: 'message',
            role: 'user',
            messageId: uid(),
            contextId: sessionId,
            parts: [{ kind: 'text', text: question }],
            metadata: { user_id: userId },
          },
        },
      }),
    })
  } catch (error) {
    if (isAbort(error)) throw error
    throw new SqlApiError({ code: 'network' })
  }

  sessionExpired(response.status, stamp)
  const type = response.headers.get('content-type') || ''
  if (!type.includes('text/event-stream') || !response.body) {
    // 鉴权、限流、流式没开：都发生在流打开之前，回来的是普通的 JSON-RPC 错误或 HTTP 错误
    let payload: unknown = null
    try { payload = await response.json() } catch { payload = null }
    const rpc = (payload as { error?: { code?: number; message?: string } } | null)?.error
    if (!rpc && !response.ok) throw toError(response.status, payload)
    throw new SqlApiError({
      status: response.status,
      code: rpc?.code === -32004 ? 'stream_unsupported' : 'rpc_error',
      message: rpc?.message || '',
      detail: rpc ?? payload,
    })
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  const stages: StreamStage[] = []
  let buffer = ''
  let startedAt: number | null = null
  let runId = ''
  let payload: StreamPayload | null = null
  let terminal: Frame | null = null

  const handle = (frame: Frame | undefined) => {
    if (!frame || typeof frame !== 'object') return
    const stamp = Date.parse(frame.status?.timestamp ?? '')
    if (frame.kind === 'task') {
      if (!Number.isNaN(stamp)) startedAt = stamp
      if (frame.id) {
        runId = frame.id
        onTask?.(frame.id)
      }
    } else if (frame.kind === 'status-update' && !frame.final) {
      const meta = frame.metadata ?? {}
      const stage: StreamStage = {
        stage: typeof meta.stage === 'string' ? meta.stage : '',
        step: typeof meta.step === 'number' ? meta.step : null,
        database: typeof meta.database === 'string' ? meta.database : '',
        shortPath: typeof meta.short_path === 'boolean' ? meta.short_path : null,
        withMemory: typeof meta.with_memory === 'boolean' ? meta.with_memory : null,
        offsetMs: startedAt !== null && !Number.isNaN(stamp) ? stamp - startedAt : null,
      }
      stages.push(stage)
      onStage?.(stage)
    } else if (frame.kind === 'artifact-update') {
      const parts = frame.artifact?.parts ?? []
      const data = parts.find((part) => part?.kind === 'data')
      const text = parts.find((part) => part?.kind === 'text')
      payload = { ...(data?.data ?? {}), answer: text?.text ?? '' } as StreamPayload
    } else if (frame.kind === 'status-update' && frame.final) {
      terminal = frame
    }
  }

  const drain = (flush: boolean) => {
    const events = buffer.split(/\r?\n\r?\n/)
    buffer = flush ? '' : events.pop() ?? ''
    for (const block of events) {
      const data = block
        .split(/\r?\n/)
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trimStart())
        .join('\n')
      if (!data) continue
      let envelope: { result?: Frame; error?: { message?: string } } | null = null
      try { envelope = JSON.parse(data) } catch { continue }
      if (envelope?.error) {
        throw new SqlApiError({ status: 200, code: 'rpc_error', message: envelope.error.message || '' })
      }
      handle(envelope?.result)
    }
  }

  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (value) buffer += decoder.decode(value, { stream: true })
      drain(done)
      if (done) break
    }
  } catch (error) {
    reader.cancel().catch(() => {})
    if (error instanceof SqlApiError || isAbort(error)) throw error
    throw new SqlApiError({ code: 'stream_interrupted' })
  }

  const final = terminal as Frame | null
  const state = final?.status?.state
  if (state === 'failed') {
    throw new SqlApiError({
      status: 500,
      code: 'query_failed',
      message: typeof final?.metadata?.error === 'string' ? final.metadata.error : '',
      detail: final?.metadata ?? null,
    })
  }
  if (state === 'canceled') throw new SqlApiError({ code: 'query_canceled', detail: final?.metadata ?? null })
  const result = payload as StreamPayload | null
  if (!result || state !== 'completed') throw new SqlApiError({ code: 'stream_interrupted' })
  if (!result.trace_id && typeof final?.metadata?.trace_id === 'string') result.trace_id = final.metadata.trace_id
  return { payload: result, stages, runId }
}

// ── 模型连接（管理员）────────────────────────────────────────────────────────

/**
 * SQL Agent 用哪个模型回答、哪个 Embedding 做表结构检索（/admin/model …）。
 *
 * 表单里填的模型 Key 只在这一次请求的请求体里，交给 SQL Agent 保存；这些接口都不把它带回来，
 * 页面也不存它（不进 localStorage / sessionStorage）。留空表示接着用现有的那把。
 * /models 只读接口的模型清单，不调模型；/test 会真的调一次模型（每个模型名一条极短的请求）。
 */
export const sqlModelApi = {
  status: () => request<SqlModelStatus>('GET', '/admin/model'),
  listModels: (body: SqlModelBody) => request<SqlModelList>('POST', '/admin/model/models', { body }),
  test: (body: SqlModelBody) => request<SqlProbeReport>('POST', '/admin/model/test', { body }),
  save: (body: SqlModelBody) => request<SqlModelSaved>('PUT', '/admin/model', { body }),
  clear: () => request<SqlModelSaved>('DELETE', '/admin/model'),
  listEmbeddingModels: (body: Pick<SqlEmbeddingBody, 'base_url' | 'api_key'>) =>
    request<SqlModelList>('POST', '/admin/model/embedding/models', { body }),
  testEmbedding: (body: SqlEmbeddingBody) => request<SqlProbeReport>('POST', '/admin/model/embedding/test', { body }),
  /** 要几秒到几十秒：先测，再用新模型把检索索引整个重建，全部成功才换上 */
  saveEmbedding: (body: SqlEmbeddingBody) => request<SqlModelSaved>('PUT', '/admin/model/embedding', { body }),
  clearEmbedding: () => request<SqlModelSaved>('DELETE', '/admin/model/embedding'),
}
