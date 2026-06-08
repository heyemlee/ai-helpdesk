import type {
  ChatRequest, ChatResponse, EvalReport, HealthResponse,
  KnowledgeStats, MonitorSummary, SearchResponse, StreamEvent,
} from './types'

const DEFAULT_API_BASE = import.meta.env.VITE_API_BASE_URL || '/api'
const STORAGE_KEY = 'echomind.console.settings.v2'

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

const trimSlash = (v: string) => v.replace(/\/+$/, '')

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(base: string, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${trimSlash(base)}${path}`, init)
  const text = await response.text()
  let data: unknown = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!response.ok) {
    const detail = typeof data === 'string' ? data : JSON.stringify(data)
    throw new ApiError(response.status, `${response.status} ${response.statusText}: ${detail}`)
  }
  return data as T
}

export const api = {
  health: (base: string) => request<HealthResponse>(base, '/health'),
  monitor: (base: string) => request<MonitorSummary>(base, '/monitor'),
  knowledgeStats: (base: string) => request<KnowledgeStats>(base, '/knowledge/stats'),

  search: (base: string, query: string, topK = 5) =>
    request<SearchResponse>(base, '/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, top_k: topK }),
    }),

  chat: (base: string, body: ChatRequest) =>
    request<ChatResponse>(base, '/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  runEval: (base: string) =>
    request<EvalReport>(base, '/eval/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    }),

  addKnowledge: (base: string, documents: Array<{ title: string; content: string }>) =>
    request<{ message: string; added_chunks: number; total_chunks: number }>(base, '/knowledge/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ documents }),
    }),

  uploadKnowledge: (base: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<{ message: string; added_chunks: number; total_chunks: number }>(
      base, '/knowledge/upload', { method: 'POST', body: form },
    )
  },
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
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: signal ?? null,
  })
  if (!response.ok || !response.body) {
    throw new ApiError(response.status, `${response.status} ${response.statusText}: ${await response.text()}`)
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
