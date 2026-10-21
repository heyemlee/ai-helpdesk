import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, getStaffKey, parseSseBlock, setStaffKey, streamRun, StreamIdle } from '../client'

describe('parseSseBlock', () => {
  it('解析标准 SSE 块', () => {
    const evt = parseSseBlock('event: delta\ndata: {"text":"你好"}')
    expect(evt).toEqual({ event: 'delta', data: { text: '你好' } })
  })

  it('没有 event 名时返回 null，避免把心跳当成数据', () => {
    expect(parseSseBlock('data: {"text":"x"}')).toBeNull()
  })

  it('data 不是合法 JSON 时返回 null 而不是抛异常', () => {
    expect(parseSseBlock('event: delta\ndata: {不是json')).toBeNull()
  })

  it('忽略空块', () => {
    expect(parseSseBlock('')).toBeNull()
    expect(parseSseBlock('\n\n')).toBeNull()
  })

  it('支持多行 data', () => {
    const evt = parseSseBlock('event: result\ndata: {"a":1,\ndata: "b":2}')
    expect(evt?.data).toEqual({ a: 1, b: 2 })
  })
})

describe('员工钥匙与错误文案', () => {
  const okJson = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
    status, statusText: status === 200 ? 'OK' : 'Unauthorized',
    headers: { 'Content-Type': 'application/json' },
  })

  afterEach(() => { setStaffKey(''); vi.unstubAllGlobals() })

  it('没配钥匙时不带 X-API-Key；配了之后每个请求都带上', async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => okJson({ tickets: [], stats: {} }))
    vi.stubGlobal('fetch', fetchMock)

    await api.tickets('/api')
    expect((fetchMock.mock.calls[0]![1] as RequestInit).headers).toEqual({})

    setStaffKey('  s3cret ')
    expect(getStaffKey()).toBe('s3cret')
    await api.tickets('/api', 'open')
    expect(fetchMock.mock.calls[1]![0]).toBe('/api/tickets?limit=200&status=open')
    expect((fetchMock.mock.calls[1]![1] as RequestInit).headers).toEqual({ 'X-API-Key': 's3cret' })

    await api.setTicketStatus('/api', 'tk_1', 'assigned')
    const init = fetchMock.mock.calls[2]![1] as RequestInit
    expect(init.headers).toEqual({ 'X-API-Key': 's3cret', 'Content-Type': 'application/json' })
    expect(init.body).toBe('{"status":"assigned"}')
  })

  it('后端的 {"detail": …} 取出里面那句话，不把 JSON 外壳显示给人看', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => okJson({ detail: '缺少或错误的 X-API-Key' }, 401)))
    const error = await api.documents('/api').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(401)
    expect((error as ApiError).message).toBe('401 Unauthorized: 缺少或错误的 X-API-Key')
  })

  it('文档筛选词和 id 进 URL 前先转义', async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => okJson({}))
    vi.stubGlobal('fetch', fetchMock)
    await api.documents('/api/', 'LTL delivery & 安装')
    await api.document('/api', 'doc a/b')
    expect(fetchMock.mock.calls[0]![0]).toBe('/api/knowledge/documents?limit=200&q=LTL%20delivery%20%26%20%E5%AE%89%E8%A3%85')
    expect(fetchMock.mock.calls[1]![0]).toBe('/api/knowledge/documents/doc%20a%2Fb')
  })
})


describe('半开的连接', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('收到过数据之后再也没有动静：超过空闲上限就当断线，抛 StreamIdle', async () => {
    const encoder = new TextEncoder()
    const hanging = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('id: 1-0\nevent: delta\ndata: {"text":"你好"}\n\n'))
        // 之后既不发数据也不关闭：API 实例没了、代理还开着
      },
    })
    vi.stubGlobal('fetch', vi.fn(async () => new Response(hanging, { status: 200 })))
    const seen: string[] = []
    const error = await (async () => {
      try {
        for await (const evt of streamRun('/api', 'r1', undefined, undefined, 50)) seen.push(evt.event)
      } catch (e) {
        return e
      }
      return null
    })()
    expect(seen).toEqual(['delta'])
    expect(error).toBeInstanceOf(StreamIdle)
  })
})
