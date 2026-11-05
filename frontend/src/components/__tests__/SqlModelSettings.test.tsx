import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import userEvent from '@testing-library/user-event'
import { render, screen, waitFor, within } from '@/test/render'
import recorded from '@/test/fixtures/sql/model-status.json'
import { SqlModelSettings } from '../SqlModelSettings'

/**
 * 模型连接页的 SQL Agent 那一栏。夹具是录下来的 GET /admin/model（演示库、没有模型凭据、能改）。
 * 移植自 ai-sql-agent/tests/test_console_frontend.py 里管模型连接的几条：走管理员接口、确认在页面里做、
 * Key 只交给服务端。
 */
type Status = typeof recorded
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

interface Call { method: string; path: string; body: Record<string, unknown> | null }

function serve(routes: Record<string, (body: Record<string, unknown> | null) => Response>) {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    const path = url.replace(/\?.*$/, '')
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : null
    calls.push({ method, path, body })
    const handler = routes[`${method} ${path}`]
    return handler ? handler(body) : json({ detail: 'not recorded' }, 404)
  }))
  return calls
}

function show(ui: ReactElement = <SqlModelSettings />) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(ui, { lang: 'en', wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider> })
}

/** 已经配了 Key、存过页面的那种状态（从录下来的那份改出来） */
const withKey = (patch: Partial<Status> = {}): Status => ({
  ...recorded,
  key_configured: true, key_hint: 'ab12', source: 'page', saved_at: '2026-11-05T21:00:00Z',
  providers: { ...recorded.providers, openai: { ...recorded.providers.openai, key_configured: true } },
  ...patch,
})

const OK_REPORT = { ok: true, endpoint: 'x', provider: 'openai', warnings: [],
  results: [{ roles: ['chat', 'coder'], model: 'gpt-6-luna', ok: true, outcome: 'ok', status_code: 200, message: '', duration_ms: 420 }] }

describe('SQL Agent 的模型连接', () => {
  // jsdom 没有 origin 时连 window.localStorage 都没有：盯住 Storage 的写入口，哪个存储都算
  let stored: ReturnType<typeof vi.spyOn>
  beforeEach(() => { stored = vi.spyOn(Storage.prototype, 'setItem') })
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

  it('最上面写清楚现在用的是哪个模型；没有 Key 时连接设置默认展开', async () => {
    serve({ 'GET /sql-api/admin/model': () => json(recorded) })
    show()
    const card = (await screen.findByRole('heading', { name: 'Model connection' })).closest('section') as HTMLElement
    expect(card.querySelector('.model-now-name')).toHaveTextContent(recorded.chat_model)
    expect(within(card).getAllByText('No key').length).toBeGreaterThan(0)
    expect(within(card).getByText(/built-in demo logic/)).toBeInTheDocument()
    expect(within(card).getByText('Connection settings').closest('details')).toHaveAttribute('open')
    // 没有能用的 Key：不去读模型清单，也不让保存
    expect(within(card).getByText(/Enter this endpoint’s key/)).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('换主机要在页面里确认（不用 window.confirm），先测再存；Key 只进请求体，存完就清空', async () => {
    const confirm = vi.spyOn(window, 'confirm')
    const saved = { ...withKey(), provider: 'anthropic', chat_model: 'claude-sonnet-5', coder_model: 'claude-sonnet-5' }
    const calls = serve({
      'GET /sql-api/admin/model': () => json(withKey()),
      'POST /sql-api/admin/model/models': () => json({ ok: true, models: ['claude-sonnet-5', 'claude-opus-5-5'], total: 2,
        outcome: 'ok', status_code: 200, message: '' }),
      'POST /sql-api/admin/model/test': () => json(OK_REPORT),
      'PUT /sql-api/admin/model': () => json({ saved: true, warnings: [], status: saved }),
    })
    show()
    const card = (await screen.findByRole('heading', { name: 'Model connection' })).closest('section') as HTMLElement
    await userEvent.click(within(card).getByText('Connection settings'))
    await userEvent.selectOptions(within(card).getByRole('combobox', { name: 'Protocol' }), 'anthropic')
    const key = within(card).getByLabelText(/^API key/)
    await userEvent.type(key, '  sk-new-key  ')
    key.blur()
    await waitFor(() => expect(calls.some((c) => c.path === '/sql-api/admin/model/models')).toBe(true))

    await userEvent.click(within(card).getByRole('button', { name: 'Save' }))
    const bar = within(card).getByRole('alertdialog')
    expect(bar).toHaveTextContent('https://api.anthropic.com')
    expect(confirm).not.toHaveBeenCalled()
    await userEvent.click(within(bar).getByRole('button', { name: 'Save' }))

    expect(await within(card).findByText('Saved. Now using claude-sonnet-5.')).toBeInTheDocument()
    const order = calls.filter((c) => c.method !== 'GET' && !c.path.endsWith('/models')).map((c) => `${c.method} ${c.path}`)
    expect(order).toEqual(['POST /sql-api/admin/model/test', 'PUT /sql-api/admin/model'])
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.body).toMatchObject({ provider: 'anthropic', api_key: 'sk-new-key', base_url: 'https://api.anthropic.com' })
    // 存完输入框清空；浏览器的存储里没有它
    expect(within(card).getByLabelText(/^API key/)).toHaveValue('')
    expect(JSON.stringify(stored.mock.calls)).not.toContain('sk-new-key')
  })

  it('测试没通过就不存，给一个"仍然保存"', async () => {
    const failed = { ...OK_REPORT, ok: false, results: [{ ...OK_REPORT.results[0]!, ok: false, outcome: 'auth', status_code: 401, message: 'invalid key' }] }
    const calls = serve({
      'GET /sql-api/admin/model': () => json(withKey()),
      'POST /sql-api/admin/model/models': () => json({ ok: true, models: ['gpt-6-luna', 'gpt-5.6-luna'], total: 2, outcome: 'ok', status_code: 200, message: '' }),
      'POST /sql-api/admin/model/test': () => json(failed),
      'PUT /sql-api/admin/model': () => json({ saved: true, warnings: [], status: { ...withKey(), chat_model: 'gpt-6-luna' } }),
    })
    show()
    const card = (await screen.findByRole('heading', { name: 'Model connection' })).closest('section') as HTMLElement
    const picker = await within(card).findByRole('combobox', { name: 'Model' })
    await waitFor(() => expect(within(picker).getByRole('option', { name: 'gpt-6-luna' })).toBeInTheDocument())
    await userEvent.selectOptions(picker, 'gpt-6-luna')
    await userEvent.click(within(card).getByRole('button', { name: 'Save' }))   // 同一台主机：不用确认

    expect(await within(card).findByText('Nothing was saved.')).toBeInTheDocument()
    expect(within(card).getByText(/the key was rejected/)).toBeInTheDocument()
    expect(calls.some((c) => c.method === 'PUT')).toBe(false)
    await userEvent.click(within(card).getByRole('button', { name: 'Save anyway' }))
    expect(await within(card).findByText('Saved. Now using gpt-6-luna.')).toBeInTheDocument()
    expect(calls.filter((c) => c.method === 'POST' && c.path.endsWith('/test'))).toHaveLength(1)
  })

  it('模型服务拒过这把 Key：直接标在当前使用上', async () => {
    serve({ 'GET /sql-api/admin/model': () => json(withKey({
      access_faults: [{ permanent: true, scope: 'credential', status_code: 401 }] as never,
    })) })
    show()
    expect(await screen.findByText('rejected by the model service · HTTP 401')).toBeInTheDocument()
  })

  it('不能改的服务（页面配置关着）只给看，说明原因', async () => {
    serve({ 'GET /sql-api/admin/model': () => json({ ...recorded, editable: false, reason: 'disabled' }) })
    show()
    expect(await screen.findByText('Page configuration is turned off on the SQL Agent')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
  })

  it('老版本的 SQL Agent 没有这组接口（404）：说没有这个功能，不当成故障', async () => {
    serve({})
    show()
    expect(await screen.findByText(/cannot configure the model connection/)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('Embedding：本地向量时写明；保存会重建索引，一律先在页面里确认', async () => {
    const confirm = vi.spyOn(window, 'confirm')
    const remote = { ...withKey(), embedding: { ...recorded.embedding, backend: 'remote', model: 'text-embedding-3-small',
      active: { backend: 'remote', model: 'text-embedding-3-small', dimensions: 1024 }, base_url: 'https://api.openai.com/v1',
      endpoint: 'https://api.openai.com/v1/embeddings', key_configured: true, key_hint: 'ab12', source: 'page' } }
    const calls = serve({
      'GET /sql-api/admin/model': () => json(withKey({ embedding: { ...recorded.embedding,
        reusable_key_addresses: ['https://api.openai.com'] } as never })),
      'POST /sql-api/admin/model/embedding/models': () => json({ ok: true, models: ['text-embedding-3-small'], total: 1, outcome: 'ok', status_code: 200, message: '' }),
      'PUT /sql-api/admin/model/embedding': () => json({ saved: true, warnings: [], status: remote,
        rebuild: { texts: 120, duration_ms: 2500 },
        test: { ok: true, endpoint: 'x', warnings: [], results: [{ roles: ['embedding'], model: 'text-embedding-3-small', ok: true, outcome: 'ok', status_code: 200, message: '', duration_ms: 90, dimensions: 1024 }] } }),
    })
    show()
    const card = (await screen.findByRole('heading', { name: 'Embedding (schema retrieval)' })).closest('section') as HTMLElement
    expect(within(card).getByText(/Local hash vectors/)).toBeInTheDocument()
    // 换成远端本身就是改动；地址落在对话模型那台已有 Key 的主机上，Key 可以留空
    await userEvent.click(within(card).getByRole('button', { name: 'Save' }))
    const bar = within(card).getByRole('alertdialog')
    expect(bar).toHaveTextContent('text-embedding-3-small')
    await userEvent.click(within(bar).getByRole('button', { name: 'Save and rebuild index' }))
    expect(await within(card).findByText('Saved. Index rebuilt: 120 texts embedded in 2.5 s')).toBeInTheDocument()
    expect(confirm).not.toHaveBeenCalled()
    expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({ base_url: 'https://api.openai.com/v1', api_key: '',
      model: 'text-embedding-3-small', dimensions: 1024 })
  })
})
