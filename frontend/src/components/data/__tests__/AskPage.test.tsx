/**
 * 问数页整页走一遍（fetch 换成假的，回放 ai-sql-agent/scripts/record_console_fixtures.py 录下的真实响应；
 * 不调模型、不连网）。移植自 test_console_ask.py 的"最近问过"和 test_console_run_cancel.py：
 *
 * - 问题走 A2A 流，首帧就拿到 run_id；答完链到 #/data/runs/<run_id>，并从 /history 回填服务端记的细节；
 * - 停止：先 POST /runs/{id}/cancel，再断开连接；停止提示照服务端的回答写；
 * - 服务端说流式没开（-32004）就退回 /query，run_id 从 X-Run-Id 拿；之后不再试流；
 * - "最近问过"跟着身份走：换了身份，上一个身份的问题不留在页面上；
 * - 查询历史交过来的问题：填进输入框、说明来由，发出时是一次新的查询。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@/test/render'
import { sqlApi, streamQuery } from '@/api/sql/client'
import { setAskDraft } from '@/lib/sql/askDraft'
import { AskPage } from '../AskPage'

const FIXTURES = resolve(process.cwd(), 'src/test/fixtures/sql')
const fixture = (name: string) => readFileSync(resolve(FIXTURES, name), 'utf8')
const CATALOG = JSON.parse(fixture('catalog.json')) as Record<string, unknown> & { identity: Record<string, unknown> }
const HISTORY = JSON.parse(fixture('history.json')) as Record<string, unknown>
const QUERY = JSON.parse(fixture('query.json')) as { run_id: string; response: Record<string, unknown> }
const STREAM = fixture('a2a-stream.sse')
const STREAM_RUN = 'a0c9ee5033f04215986629f1febc5516'

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } })
const sse = (body: BodyInit) => new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
const frame = (result: unknown) => `data: ${JSON.stringify({ jsonrpc: '2.0', id: 'x', result })}\n\n`

interface Call { url: string; method: string; body: Record<string, any> | undefined; headers: unknown }

function stubFetch(handle: (call: Call, init: RequestInit) => Response | Promise<Response>) {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const call: Call = {
      url: String(input), method: init.method ?? 'GET',
      body: init.body ? JSON.parse(String(init.body)) : undefined, headers: init.headers,
    }
    calls.push(call)
    return handle(call, init)
  }))
  return calls
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(<AskPage active />, {
    lang: 'en',
    wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
  })
  return { client, ...view }
}

const ask = async (question: string) => {
  await userEvent.type(await screen.findByRole('textbox', { name: 'Your question' }), `${question}{Enter}`)
}

afterEach(() => { vi.unstubAllGlobals() })

describe('问数页', () => {
  it('问题走 A2A 流：答完链到这次查询，并从历史回填服务端记的细节', async () => {
    const calls = stubFetch(({ url }) => {
      if (url === '/sql-api/catalog') return json(CATALOG)
      if (url.startsWith('/sql-api/history')) return json(HISTORY)
      if (url === '/sql-api/a2a') return sse(STREAM)
      throw new Error(`unexpected ${url}`)
    })
    const { container } = mount()
    await ask('List the sales orders signed this year')

    await waitFor(() => expect(container.querySelector('.sq-conclusion')?.textContent).toMatch(/^3 records found: /))
    // 读屏只念一次：状态区里是同一句结论
    expect(container.querySelector('.sq-ask-announce')?.textContent).toMatch(/^3 records found: /)
    expect(screen.getByRole('link', { name: /See how it ran/ })).toHaveAttribute('href', `#/data/runs/${STREAM_RUN}`)
    const a2a = calls.find((call) => call.url === '/sql-api/a2a')!
    expect(a2a.method).toBe('POST')
    expect(a2a.body?.method).toBe('message/stream')
    // 发问用的是服务端解析出来的身份，不是页面编的
    expect(a2a.body?.params.message.metadata).toEqual({ user_id: 'console' })
    expect(a2a.body?.params.message.contextId).toMatch(/^s-/)
    // 浏览器从不带 SQL Agent 的 Key：它在控制台的 nginx 里
    expect(JSON.stringify(a2a.headers)).not.toContain('X-API-Key')
    // 服务端记账的细节（检索考虑过的表）查完从 /history 回填
    expect(await screen.findByText('payment, purchase_order, sales_order, supplier')).toBeInTheDocument()
    expect(calls.filter((call) => call.url === '/sql-api/history?limit=100&user_id=console').length).toBeGreaterThanOrEqual(2)
  })

  it('停止：先请服务端停下这次运行，再断开连接；提示照服务端的回答写', async () => {
    const order: string[] = []
    const encoder = new TextEncoder()
    stubFetch(({ url, method }, init) => {
      if (url === '/sql-api/catalog') return json(CATALOG)
      if (url.startsWith('/sql-api/history')) return json(HISTORY)
      if (url === '/sql-api/a2a') {
        return sse(new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode(frame({ kind: 'task', id: 'run-stop-1', contextId: 'c',
              status: { state: 'submitted', timestamp: '2026-01-01T00:00:00.000Z' } })))
            controller.enqueue(encoder.encode(frame({ kind: 'status-update', taskId: 'run-stop-1', contextId: 'c', final: false,
              status: { state: 'working', timestamp: '2026-01-01T00:00:00.100Z' }, metadata: { stage: 'routing', trace_id: 't' } })))
            // 真的 fetch 在断开时让读流失败；假的照做
            init.signal?.addEventListener('abort', () => {
              order.push('abort')
              controller.error(new DOMException('aborted', 'AbortError'))
            })
          },
        }))
      }
      if (url === '/sql-api/runs/run-stop-1/cancel' && method === 'POST') {
        order.push('cancel')
        return json({ run_id: 'run-stop-1', status: 'cancel_requested' }, 202)
      }
      throw new Error(`unexpected ${method} ${url}`)
    })
    const { container } = mount()
    await ask('A long question')

    // 首帧已经到了（taskId 就是 run_id），进度停在第一个阶段
    expect(await screen.findByText('Deciding how to answer')).toBeInTheDocument()
    await userEvent.click(within(container.querySelector('.sq-turn-a') as HTMLElement).getByRole('button', { name: 'Stop' }))

    const answer = container.querySelector('.sq-turn-a') as HTMLElement
    expect(await within(answer).findByText(/The server was asked to stop it as well/)).toBeInTheDocument()
    expect(order).toEqual(['cancel', 'abort'])
    expect(within(answer).getByText('You stopped this query')).toBeInTheDocument()
  })

  it('服务端说流式没开（-32004）：退回 /query，run_id 从响应头拿，之后不再试流', async () => {
    const calls = stubFetch(({ url }) => {
      if (url === '/sql-api/catalog') return json(CATALOG)
      if (url.startsWith('/sql-api/history')) return json(HISTORY)
      if (url === '/sql-api/a2a') return json({ jsonrpc: '2.0', id: 'x', error: { code: -32004, message: 'streaming is off' } })
      if (url === '/sql-api/query') return json(QUERY.response, 200, { 'X-Run-Id': 'run-from-header' })
      throw new Error(`unexpected ${url}`)
    })
    const { container } = mount()
    await ask('Which suppliers are late?')

    await waitFor(() => expect(container.querySelector('.sq-conclusion')?.textContent).toMatch(/^3 records found: /))
    expect(screen.getByRole('link', { name: /See how it ran/ })).toHaveAttribute('href', '#/data/runs/run-from-header')
    const query = calls.find((call) => call.url === '/sql-api/query')!
    expect(query.body).toMatchObject({ query: 'Which suppliers are late?', user_id: 'console' })
    expect(query.body?.session_id).toMatch(/^s-/)

    await userEvent.click(screen.getByRole('button', { name: /Ask again/ }))
    await waitFor(() => expect(calls.filter((call) => call.url === '/sql-api/query')).toHaveLength(2))
    expect(calls.filter((call) => call.url === '/sql-api/a2a')).toHaveLength(1)
  })

  it('"最近问过"跟着身份走：刷新失败时留着，换了身份就换一份', async () => {
    let who = 'alice'
    const items = (...queries: string[]) => ({
      user_id: who, items: queries.map((query, index) => ({ trace_id: `${who}-${index}`, query, created_at: new Date().toISOString() })),
    })
    let history = (user: string): Response | Promise<Response> => json(user === 'alice' ? items('first identity question') : items())
    stubFetch(({ url }) => {
      if (url === '/sql-api/catalog') return json({ ...CATALOG, identity: { ...CATALOG.identity, user_id: who } })
      if (url.startsWith('/sql-api/history')) return history(new URL(url, 'http://console').searchParams.get('user_id') ?? '')
      throw new Error(`unexpected ${url}`)
    })
    const { client } = mount()
    expect(await screen.findByText('first identity question')).toBeInTheDocument()

    // 一次刷新失败：取到过的那份还在，不至于整段消失
    history = () => json({ detail: 'slow down' }, 429)
    await act(async () => { await client.invalidateQueries({ queryKey: ['sql', 'history', 'alice'] }) })
    await waitFor(() => expect(client.getQueryState(['sql', 'history', 'alice'])?.status).toBe('error'))
    expect(screen.getByText('first identity question')).toBeInTheDocument()

    // 换了身份：上一个身份问过的问题马上从页面上消失，新身份的到了再出现
    let releaseBob = () => {}
    history = (user) => (user === 'bob'
      ? new Promise<Response>((done) => { releaseBob = () => done(json(items('second identity question'))) })
      : json(items('stale answer for the old identity')))
    who = 'bob'
    await act(async () => { await client.invalidateQueries({ queryKey: ['sql', 'catalog'] }) })
    await waitFor(() => expect(screen.queryByText('first identity question')).toBeNull())
    act(() => releaseBob())
    expect(await screen.findByText('second identity question')).toBeInTheDocument()
    expect(screen.queryByText('stale answer for the old identity')).toBeNull()
  })

  it('查询历史交过来的问题：填进输入框、说明来由，发出去是一次新的查询', async () => {
    stubFetch(({ url }) => {
      if (url === '/sql-api/catalog') return json(CATALOG)
      if (url.startsWith('/sql-api/history')) return json(HISTORY)
      if (url === '/sql-api/a2a') return sse(STREAM)
      throw new Error(`unexpected ${url}`)
    })
    setAskDraft({ question: 'List the sales orders signed this year', note: 'Asks the same question again and reads today’s data.' })
    mount()

    expect(await screen.findByRole('textbox', { name: 'Your question' })).toHaveValue('List the sales orders signed this year')
    expect(screen.getByText('Asks the same question again and reads today’s data.')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Send question' }))

    expect(await screen.findByText('This is a new query')).toBeInTheDocument()
    expect(screen.getByText(/^New query, run/)).toBeInTheDocument()
    expect(screen.queryByText('Asks the same question again and reads today’s data.')).toBeNull()
  })
})

describe('流和取消的请求', () => {
  it('流从首帧交出 run_id，不影响正常解析结果', async () => {
    stubFetch(() => sse([
      frame({ kind: 'task', id: 'run123', contextId: 'c', status: { state: 'submitted', timestamp: '2026-01-01T00:00:00.000Z' } }),
      frame({ kind: 'artifact-update', taskId: 'run123', contextId: 'c', lastChunk: true,
        artifact: { parts: [{ kind: 'text', text: 'ok' }, { kind: 'data', data: { route: 'database_query' } }] } }),
      frame({ kind: 'status-update', taskId: 'run123', contextId: 'c', final: true,
        status: { state: 'completed', timestamp: '2026-01-01T00:00:01.000Z' }, metadata: { trace_id: 't' } }),
    ].join('')))
    const seen: string[] = []
    const streamed = await streamQuery('q', { userId: 'console', sessionId: 's-1', onTask: (id) => seen.push(id) })
    expect(seen).toEqual(['run123'])
    expect(streamed.runId).toBe('run123')
    expect(streamed.payload.answer).toBe('ok')
  })

  it('取消发到这次运行上；控制台不代别人取消，不带 user_id', async () => {
    const calls = stubFetch(() => json({ run_id: 'run 1/2', status: 'cancel_requested' }, 202))
    const cancelled = await sqlApi.cancelRun('run 1/2')
    expect(calls.at(-1)).toMatchObject({ method: 'POST', url: '/sql-api/runs/run%201%2F2/cancel' })
    expect(cancelled.status).toBe('cancel_requested')
  })
})
