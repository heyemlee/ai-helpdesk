import type {
  ChatModelBody, ChatRequest, ChatResponse, ConversationResponse, EmbeddingModelBody, EvalReport,
  HealthResponse, ImportResult, KnowledgeCategoryOverview, KnowledgeDocument, KnowledgeDocumentDetail,
  KnowledgeDocumentList, ModelStatus,
  ModelTestResult, MonitorSummary, SearchResponse, SopList, StreamEvent, TicketDetail, TicketList,
} from './types'

const DEFAULT_API_BASE = import.meta.env.VITE_API_BASE_URL || '/api'
const STORAGE_KEY = 'ai-helpdesk.console.settings.v2'
const STAFF_KEY_STORAGE = 'ai-helpdesk.console.staff-key'

export interface Settings {
  userId: string
  conversationId: string
  apiBaseUrl: string
}

export function loadSettings(): Settings {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}') as Partial<Settings>
    return {
      userId: saved.userId || 'u1001',
      conversationId: saved.conversationId || '',
      apiBaseUrl: saved.apiBaseUrl || DEFAULT_API_BASE,
    }
  } catch {
    return { userId: 'u1001', conversationId: '', apiBaseUrl: DEFAULT_API_BASE }
  }
}

export function saveSettings(settings: Settings): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(settings))
}

/**
 * 员工钥匙（X-API-Key）。只有配了 AI_HELPDESK_API_KEY 的后端才需要它；
 * 本机联调的后端不校验，留空即可。
 *
 * 放 sessionStorage 而不是 localStorage：这是一把所有员工共用的钥匙，
 * 关掉标签页就该失效，不该在一台共用电脑上留到下一个人来。权限是后端
 * 判的——这里只负责把钥匙带上，界面上没有任何东西靠"前端认为你是谁"放行。
 */
let staffKey = ''
try { staffKey = globalThis.sessionStorage?.getItem(STAFF_KEY_STORAGE) ?? '' } catch { /* 存储不可用就当没配 */ }

export const getStaffKey = (): string => staffKey

export function setStaffKey(value: string): void {
  staffKey = value.trim()
  try {
    if (staffKey) globalThis.sessionStorage?.setItem(STAFF_KEY_STORAGE, staffKey)
    else globalThis.sessionStorage?.removeItem(STAFF_KEY_STORAGE)
  } catch { /* 存不下就只在这一页有效 */ }
}

const authHeaders = (): Record<string, string> => (staffKey ? { 'X-API-Key': staffKey } : {})

const trimSlash = (v: string) => v.replace(/\/+$/, '')

export class ApiError extends Error {
  /** 后端错误体里 `detail` 的原样内容。模型连接的接口在这里给出 `{code, message, test}`。 */
  constructor(readonly status: number, message: string, readonly detail?: unknown) {
    super(message)
    this.name = 'ApiError'
  }
}

/**
 * 拼错误文案。detail 为空体、null 或 undefined 时不要再拼冒号——
 * 否则界面上会出现 "500 Internal Server Error: null" 或结尾一个孤立冒号。
 */
function errorMessage(status: number, statusText: string, detail: unknown): string {
  // FastAPI 的错误体是 {"detail": "..."}：把那句话取出来，别把 JSON 外壳也显示给人看
  const inner = detail && typeof detail === 'object' && 'detail' in detail
    ? (detail as { detail: unknown }).detail
    : detail
  const text =
    typeof inner === 'string' ? inner
      : inner === null || inner === undefined ? ''
        : JSON.stringify(inner)
  const head = `${status} ${statusText}`.trim()
  const body = text.trim()
  return body ? `${head}: ${body}` : head
}

async function request<T>(base: string, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${trimSlash(base)}${path}`, {
    ...init,
    headers: { ...authHeaders(), ...(init?.headers as Record<string, string> | undefined) },
  })
  const text = await response.text()
  let data: unknown = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!response.ok) {
    const detail = data && typeof data === 'object' && 'detail' in data
      ? (data as { detail: unknown }).detail
      : undefined
    throw new ApiError(response.status, errorMessage(response.status, response.statusText, data), detail)
  }
  return data as T
}

const json = (body: unknown, method = 'POST'): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const api = {
  health: (base: string) => request<HealthResponse>(base, '/health'),
  monitor: (base: string) => request<MonitorSummary>(base, '/monitor'),

  search: (base: string, query: string, topK = 5) =>
    request<SearchResponse>(base, '/search', json({ query, top_k: topK })),

  chat: (base: string, body: ChatRequest) =>
    request<ChatResponse>(base, '/chat', json(body)),

  conversation: (base: string, convId: string) =>
    request<ConversationResponse>(base, `/conversations/${encodeURIComponent(convId)}`),

  runEval: (base: string) => request<EvalReport>(base, '/eval/run', json({})),

  documents: (base: string, query = '', category = '') =>
    request<KnowledgeDocumentList>(
      base,
      `/knowledge/documents?limit=200${query ? `&q=${encodeURIComponent(query)}` : ''}`
        + (category ? `&category=${encodeURIComponent(category)}` : ''),
    ),

  document: (base: string, docId: string) =>
    request<KnowledgeDocumentDetail>(base, `/knowledge/documents/${encodeURIComponent(docId)}`),

  categories: (base: string) => request<KnowledgeCategoryOverview>(base, '/knowledge/categories'),

  /** 只改分类：正文、片段和检索都不受影响。空串是未分类。 */
  setDocumentCategory: (base: string, docId: string, category: string) =>
    request<KnowledgeDocument>(
      base, `/knowledge/documents/${encodeURIComponent(docId)}`, json({ category }, 'PATCH'),
    ),

  addKnowledge: (base: string, documents: Array<{ title: string; content: string; category?: string }>) =>
    request<ImportResult>(base, '/knowledge/add', json({ documents })),

  /** category 是这次导入的分类；JSON 文件里每篇自己写了 category 的以它为准。 */
  uploadKnowledge: (base: string, file: File, category = '') => {
    const form = new FormData()
    form.append('file', file)
    if (category) form.append('category', category)
    return request<ImportResult>(base, '/knowledge/upload', { method: 'POST', body: form })
  },

  sops: (base: string) => request<SopList>(base, '/sops'),

  tickets: (base: string, status = '') =>
    request<TicketList>(base, `/tickets?limit=200${status ? `&status=${encodeURIComponent(status)}` : ''}`),

  ticket: (base: string, ticketId: string) =>
    request<TicketDetail>(base, `/tickets/${encodeURIComponent(ticketId)}`),

  setTicketStatus: (base: string, ticketId: string, status: string) =>
    request<TicketDetail>(base, `/tickets/${encodeURIComponent(ticketId)}/status`, json({ status })),

  // ── 模型连接（管理与诊断 → 模型连接）。Key 只往里送，响应里永远没有它 ──
  modelStatus: (base: string) => request<ModelStatus>(base, '/admin/model'),
  testChatModel: (base: string, body: ChatModelBody) =>
    request<ModelTestResult>(base, '/admin/model/chat/test', json(body)),
  saveChatModel: (base: string, body: ChatModelBody & { force?: boolean }) =>
    request<ModelStatus & { test: ModelTestResult }>(base, '/admin/model/chat', json(body, 'PUT')),
  clearChatModel: (base: string) =>
    request<ModelStatus>(base, '/admin/model/chat', { method: 'DELETE' }),
  testEmbeddingModel: (base: string, body: EmbeddingModelBody) =>
    request<ModelTestResult>(base, '/admin/model/embedding/test', json(body)),
  saveEmbeddingModel: (base: string, body: EmbeddingModelBody) =>
    request<ModelStatus & { test: ModelTestResult }>(base, '/admin/model/embedding', json(body, 'PUT')),
  clearEmbeddingModel: (base: string) =>
    request<ModelStatus>(base, '/admin/model/embedding', { method: 'DELETE' }),
}

/**
 * 消费 /chat/stream 的 SSE。
 *
 * 浏览器原生的 EventSource 只支持 GET，而这个接口是 POST，
 * 所以用 fetch + ReadableStream 手动按 SSE 线格式解析。
 */
export async function* streamChat(
  base: string,
  body: ChatRequest,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const response = await fetch(`${trimSlash(base)}/chat/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(body),
    signal: signal ?? null,
  })
  if (!response.ok || !response.body) {
    const raw = await response.text()
    let detail: unknown = raw
    try { detail = raw ? JSON.parse(raw) : null } catch { /* 不是 JSON 就按原文显示 */ }
    throw new ApiError(response.status, errorMessage(response.status, response.statusText, detail))
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      // SSE 用空行分隔事件；最后一段可能被截断，留在 buffer 里等下一片
      const blocks = buffer.split('\n\n')
      buffer = blocks.pop() ?? ''
      for (const block of blocks) {
        const parsed = parseSseBlock(block)
        if (parsed) yield parsed
      }
    }
    const tail = parseSseBlock(buffer)
    if (tail) yield tail
  } finally {
    reader.releaseLock()
  }
}

export function parseSseBlock(block: string): StreamEvent | null {
  let event = ''
  const dataLines: string[] = []
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }
  if (!event || dataLines.length === 0) return null
  try {
    return { event, data: JSON.parse(dataLines.join('\n')) } as StreamEvent
  } catch {
    return null
  }
}
