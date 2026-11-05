import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import userEvent from '@testing-library/user-event'
import { render, screen, waitFor, within } from '@/test/render'
import catalog from '@/test/fixtures/sql/catalog.json'
import history from '@/test/fixtures/sql/history.json'
import run from '@/test/fixtures/sql/run.json'
import runEvents from '@/test/fixtures/sql/run-events.json'
import runNotFound from '@/test/fixtures/sql/run-not-found.json'
import { takeAskDraft } from '@/lib/sql/askDraft'
import { HistoryPage, ASSISTANT_USER_ID } from '../HistoryPage'
import { RunPage } from '../RunPage'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

/** 按路径回录下来的响应；记下每个请求，方便断言"问的是谁的历史" */
function serve(routes: Record<string, () => Response>) {
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    calls.push(url)
    const path = url.replace(/\?.*$/, '')
    const handler = routes[path]
    return handler ? handler() : json({ detail: 'not here' }, 404)
  }))
  return calls
}

function show(ui: ReactElement, lang: 'en' | 'zh' = 'en') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(ui, { lang, wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider> })
}

describe('查询历史', () => {
  beforeEach(() => { window.location.hash = '#/data/history' })
  afterEach(() => { vi.unstubAllGlobals(); takeAskDraft() })

  it('列出问题和状态；选一条看到当时的 SQL、跳到这次查询的经过', async () => {
    serve({ '/sql-api/history': () => json(history), '/sql-api/catalog': () => json(catalog) })
    show(<HistoryPage />)
    const first = history.items[0]!
    await userEvent.click(await screen.findByRole('button', { name: new RegExp(first.query) }))

    const detail = screen.getByRole('heading', { level: 3, name: first.query }).closest('.sq-detail') as HTMLElement
    expect(within(detail).getByText(first.sqls[0]!.sql, { normalizer: (text) => text })).toBeInTheDocument()
    expect(within(detail).getByRole('link', { name: 'See how it ran' })).toHaveAttribute('href', `#/data/runs/${first.run_id}`)
    // 库名写成业务名（来自目录），不是标识符
    expect(within(detail).getAllByText(/Orders & Purchasing \(erp_db\)/).length).toBeGreaterThan(0)
  })

  it('能切到客服助手委派过来的提问：按它的 user_id 去问', async () => {
    const calls = serve({ '/sql-api/history': () => json({ ...history, user_id: ASSISTANT_USER_ID }), '/sql-api/catalog': () => json(catalog) })
    show(<HistoryPage />)
    await screen.findAllByRole('button', { name: new RegExp(history.items[0]!.query) })
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Asked by' }), 'assistant')
    await waitFor(() => expect(calls.some((url) => url.includes(`user_id=${ASSISTANT_USER_ID}`))).toBe(true))
    expect(calls.filter((url) => url.startsWith('/sql-api/history'))[0]).not.toContain('user_id=')
  })

  it('"再问一次"把问题交给问数页，带上它当时的时间', async () => {
    serve({ '/sql-api/history': () => json(history), '/sql-api/catalog': () => json(catalog) })
    show(<HistoryPage />)
    const first = history.items[0]!
    await userEvent.click(await screen.findByRole('button', { name: new RegExp(first.query) }))
    await userEvent.click(screen.getByRole('button', { name: 'Ask again' }))
    expect(window.location.hash).toBe('#/data/ask')
    const draft = takeAskDraft()
    expect(draft?.question).toBe(first.query)
    expect(draft?.note).toMatch(/today’s data/)
  })

  it('搜索和筛选只动列表', async () => {
    serve({ '/sql-api/history': () => json(history), '/sql-api/catalog': () => json(catalog) })
    show(<HistoryPage />)
    await screen.findAllByRole('button', { name: new RegExp(history.items[0]!.query) })
    expect(screen.getByRole('status')).toHaveTextContent(`Showing ${history.items.length} of ${history.items.length}`)
    await userEvent.type(screen.getByRole('searchbox'), history.items[1]!.query.slice(0, 6))
    expect(screen.getByRole('status')).toHaveTextContent(`Showing 1 of ${history.items.length}`)
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Show' }), 'failed')
    expect(screen.getByText('No questions match.')).toBeInTheDocument()
  })

  it('控制台的 Key 被拒（401）：说要管理员核对服务器上的 Key，不叫员工去填', async () => {
    serve({ '/sql-api/history': () => json({ detail: '缺少或错误的 X-API-Key' }, 401) })
    show(<HistoryPage />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The administrator needs to check the console’s key on the server.')
    expect(alert).toHaveTextContent('缺少或错误的 X-API-Key')
  })

  it('SQL Agent 没起（nginx 502）：说数据服务没在运行', async () => {
    serve({ '/sql-api/history': () => new Response('<html>502</html>', { status: 502 }) })
    show(<HistoryPage />)
    expect(await screen.findByRole('alert')).toHaveTextContent('The data service is not running')
  })
})

describe('一次查询的经过', () => {
  afterEach(() => { vi.unstubAllGlobals(); takeAskDraft() })

  it('问题、状态、提问方，以及每一步——执行那一步下面是那条 SQL', async () => {
    serve({
      [`/sql-api/runs/${run.run_id}`]: () => json(run),
      [`/sql-api/runs/${run.run_id}/events`]: () => json(runEvents),
      '/sql-api/catalog': () => json(catalog),
    })
    show(<RunPage runId={run.run_id} />)
    expect(await screen.findByRole('heading', { level: 3, name: run.query })).toBeInTheDocument()
    expect(screen.getByText('Finished')).toBeInTheDocument()
    expect(screen.getByText('Data found')).toBeInTheDocument()
    // 控制台自己问的：认出是"这个控制台"
    await waitFor(() => expect(screen.getByText('this console')).toBeInTheDocument())

    const sql = runEvents.events.find((event) => event.type === 'tool.sql')!.attrs.sql as string
    expect(await screen.findByText(sql, { normalizer: (text) => text })).toBeInTheDocument()
    expect(screen.getByText('Running the query')).toBeInTheDocument()
    expect(screen.getByText('Planning the steps')).toBeInTheDocument()
  })

  it('别的调用方的 run（员工实例委派的）写明是谁问的', async () => {
    const delegated = { ...run, caller: 'ai-helpdesk', user_id: 'ai-helpdesk', entry: 'a2a_stream' }
    serve({
      [`/sql-api/runs/${run.run_id}`]: () => json(delegated),
      [`/sql-api/runs/${run.run_id}/events`]: () => json(runEvents),
      '/sql-api/catalog': () => json(catalog),
    })
    show(<RunPage runId={run.run_id} />)
    expect(await screen.findByText('ai-helpdesk (user ai-helpdesk)')).toBeInTheDocument()
    expect(screen.getByText('A2A, streaming')).toBeInTheDocument()
  })

  it('没有这次 run（或已过保留期）：说清楚，不当成故障', async () => {
    serve({ [`/sql-api/runs/nope`]: () => json(runNotFound, 404) })
    show(<RunPage runId="nope" />)
    expect(await screen.findByText(/No such run/)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('服务端关了运行记录：只有当时的状态，不去取事件', async () => {
    const calls = serve({ [`/sql-api/runs/${run.run_id}`]: () => json({ run_id: run.run_id, trace_id: 't', entry: 'http_query', status: 'running', persisted: false }) })
    show(<RunPage runId={run.run_id} />)
    expect(await screen.findByText(/Run history is switched off/)).toBeInTheDocument()
    expect(calls.some((url) => url.includes('/events'))).toBe(false)
  })
})
