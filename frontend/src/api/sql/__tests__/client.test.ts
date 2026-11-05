import { readFileSync } from 'node:fs'
import path from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SqlApiError, sqlApi, streamQuery } from '../client'
import query from '@/test/fixtures/sql/query.json'
import unauthorized from '@/test/fixtures/sql/unauthorized.json'
import rowsError from '@/test/fixtures/sql/rows-error.json'

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } })

const SSE = readFileSync(path.join(process.cwd(), 'src', 'test', 'fixtures', 'sql', 'a2a-stream.sse'), 'utf8')

/** 把一段 SSE 文本按给定的块大小切开发：网络分块不等于事件边界 */
function sse(text: string, chunk = 7): Response {
  const bytes = new TextEncoder().encode(text)
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (let at = 0; at < bytes.length; at += chunk) controller.enqueue(bytes.slice(at, at + chunk))
      controller.close()
    },
  })
  return new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } })
}

describe('SQL Agent 客户端', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('请求走 /sql-api，不带任何 Key——Key 由控制台的 nginx 在服务端加', async () => {
    const fetchMock = vi.fn(async () => json({ items: [], user_id: 'console' }))
    vi.stubGlobal('fetch', fetchMock)
    await sqlApi.history({ limit: 20, userId: 'ai-helpdesk' })
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/sql-api/history?limit=20&user_id=ai-helpdesk')
    expect(JSON.stringify(init.headers ?? {})).not.toMatch(/api-key/i)
  })

  it('/query 把 X-Run-Id 一起交回来：它和 A2A 的 taskId 是同一个东西', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json(query.response, 200, { 'X-Run-Id': query.run_id })))
    const { response, runId } = await sqlApi.query({ query: 'q', user_id: 'console', session_id: 's' })
    expect(runId).toBe(query.run_id)
    expect(response.trace_id).toBe(query.response.trace_id)
  })

  it('成功的状态码配上读不懂的响应体，是错误，不是一个空结果', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<html>login</html>', { status: 200 })))
    await expect(sqlApi.catalog()).rejects.toMatchObject({ code: 'bad_response', status: 200 })
  })

  it('三种 detail 形状都归成 SqlApiError，服务端原话留着', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json(unauthorized, 401)))
    await expect(sqlApi.history()).rejects.toMatchObject({ status: 401, serverMessage: unauthorized.detail })

    vi.stubGlobal('fetch', vi.fn(async () => json(rowsError, 400)))
    await expect(sqlApi.rows('d', 't', { page: 1, page_size: 25 } as never))
      .rejects.toMatchObject({ code: rowsError.detail.code, serverMessage: rowsError.detail.message })

    vi.stubGlobal('fetch', vi.fn(async () => json({ detail: [{ msg: 'a' }, { msg: 'b' }] }, 422)))
    await expect(sqlApi.rows('d', 't', { page: 0, page_size: 25 } as never))
      .rejects.toMatchObject({ code: 'invalid_request', serverMessage: 'a; b' })
  })

  it('请求没到服务端是 status 0；用户自己取消的照原样抛 AbortError，不当成故障', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch') }))
    await expect(sqlApi.whoami()).rejects.toMatchObject({ status: 0, code: 'network' })

    vi.stubGlobal('fetch', vi.fn(async () => { throw new DOMException('aborted', 'AbortError') }))
    await expect(sqlApi.whoami()).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('库名、表名、run_id 在路径里都转义', async () => {
    const fetchMock = vi.fn(async () => json({}))
    vi.stubGlobal('fetch', fetchMock)
    await sqlApi.table('a/b', 'c d')
    await sqlApi.run('x?y')
    expect(fetchMock.mock.calls.map((call) => (call as unknown as [string])[0]))
      .toEqual(['/sql-api/catalog/a%2Fb/c%20d', '/sql-api/runs/x%3Fy'])
  })
})

describe('A2A message/stream', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('按录下来的真实流拆帧：先交出 run_id，阶段一条不多一条不少，最后拿到载荷', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => sse(SSE)))
    const order: string[] = []
    const { payload, stages, runId } = await streamQuery('q', {
      userId: 'console', sessionId: 's',
      onTask: (id) => order.push(`task:${id}`),
      onStage: (stage) => order.push(`stage:${stage.stage}`),
    })
    const frames = SSE.split('\n\n').filter((block) => block.startsWith('data:'))
      .map((block) => JSON.parse(block.slice(5)).result)
    const working = frames.filter((frame) => frame.kind === 'status-update' && !frame.final)
    expect(order[0]).toBe(`task:${frames[0].id}`)
    expect(runId).toBe(frames[0].id)
    expect(stages.map((stage) => stage.stage)).toEqual(working.map((frame) => frame.metadata.stage))
    expect(stages.every((stage) => stage.offsetMs === null || stage.offsetMs >= 0)).toBe(true)
    expect(payload.trace_id).toBeTruthy()
    expect(typeof payload.answer).toBe('string')
    expect(Array.isArray(payload.steps)).toBe(true)
  })

  it('流在终态之前断了，就是没拿到结果——哪怕已经收到了载荷', async () => {
    const cut = SSE.slice(0, SSE.lastIndexOf('data:'))
    vi.stubGlobal('fetch', vi.fn(async () => sse(cut)))
    await expect(streamQuery('q', { userId: 'u', sessionId: 's' })).rejects.toMatchObject({ code: 'stream_interrupted' })
  })

  it('终态 failed 带着服务端的原话；canceled 单独认出来', async () => {
    const end = (state: string, extra = {}) => `data: ${JSON.stringify({ jsonrpc: '2.0', id: 1, result: {
      kind: 'status-update', taskId: 't', contextId: 'c', final: true, status: { state },
      metadata: { trace_id: 'tr', ...extra } } })}\n\n`
    const task = `data: ${JSON.stringify({ jsonrpc: '2.0', id: 1, result: { kind: 'task', id: 't' } })}\n\n`

    vi.stubGlobal('fetch', vi.fn(async () => sse(task + end('failed', { error: '执行超时' }))))
    await expect(streamQuery('q', { userId: 'u', sessionId: 's' }))
      .rejects.toMatchObject({ code: 'query_failed', serverMessage: '执行超时' })

    vi.stubGlobal('fetch', vi.fn(async () => sse(task + end('canceled'))))
    await expect(streamQuery('q', { userId: 'u', sessionId: 's' })).rejects.toMatchObject({ code: 'query_canceled' })
  })

  it('流还没打开就被拒（鉴权、限流）：普通的 HTTP 错误，不当成流断了', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json(unauthorized, 401)))
    const error = await streamQuery('q', { userId: 'u', sessionId: 's' }).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(SqlApiError)
    expect(error).toMatchObject({ status: 401 })
  })

  it('服务端没开流式：-32004 认成 stream_unsupported，调用方据此改走 /query', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ jsonrpc: '2.0', id: 1, error: { code: -32004, message: 'no' } })))
    await expect(streamQuery('q', { userId: 'u', sessionId: 's' })).rejects.toMatchObject({ code: 'stream_unsupported' })
  })
})
