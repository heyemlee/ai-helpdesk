import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import userEvent from '@testing-library/user-event'
import { render, screen, waitFor, within } from '@/test/render'
import feedbackStats from '@/test/fixtures/sql/feedback-stats.json'
import health from '@/test/fixtures/sql/health.json'
import readyz from '@/test/fixtures/sql/readyz.json'
import schemaStatus from '@/test/fixtures/sql/schema-status.json'
import { parseRoute } from '@/lib/route'
import { SqlAdminPanel } from '../SqlAdminPanel'

/** 管理与诊断 → SQL Agent。夹具是录下来的 /health、/readyz、/admin/schema/status、/feedback/stats */
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

function serve(overrides: Record<string, () => Response> = {}) {
  const calls: string[] = []
  const routes: Record<string, () => Response> = {
    'GET /sql-api/health': () => json(health),
    'GET /sql-api/readyz': () => json(readyz),
    'GET /sql-api/admin/schema/status': () => json(schemaStatus),
    'GET /sql-api/feedback/stats': () => json(feedbackStats),
    'POST /sql-api/admin/schema/reload': () => json({ reloaded: true, changed_databases: ['erp_db'], fingerprints: {} }),
    'POST /sql-api/admin/circuit/reset': () => json({ reset: true, before: {}, after: {} }),
    ...overrides,
  }
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const key = `${init?.method ?? 'GET'} ${url.replace(/\?.*$/, '')}`
    calls.push(key)
    return routes[key]?.() ?? json({ detail: 'not recorded' }, 404)
  }))
  return calls
}

function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><SqlAdminPanel /></QueryClientProvider>, { lang: 'en' })
}

const card = (title: string) => screen.getByRole('heading', { name: title }).closest('section') as HTMLElement

describe('管理与诊断 → SQL Agent', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

  it('有自己的一个标签页：#/admin/sql', () => {
    expect(parseRoute('#/admin/sql')).toEqual({ view: 'admin', tab: 'sql' })
  })

  it('诊断：进程、依赖项和每一项检查；Schema 索引的指纹；还没有反馈', async () => {
    serve()
    show()
    const diag = card('Diagnostics')
    await waitFor(() => expect(within(diag).getAllByText('OK')).toHaveLength(2))
    expect(within(diag).getByText('auth_mode')).toBeInTheDocument()
    const schema = card('Schema index')
    expect(await within(schema).findByText('The index matches the business databases.')).toBeInTheDocument()
    for (const [database, fingerprint] of Object.entries(schemaStatus.fingerprints)) {
      expect(within(schema).getByText(database)).toBeInTheDocument()
      expect(within(schema).getByText(fingerprint)).toBeInTheDocument()
    }
    expect(await within(card('Feedback on answers')).findByText('No verdicts yet.')).toBeInTheDocument()
  })

  it('没就绪（503）时照样列出是哪一项没过', async () => {
    serve({ 'GET /sql-api/readyz': () => json({ detail: { ready: false, checks: { audit_store: 'error: locked' } } }, 503) })
    show()
    const diag = card('Diagnostics')
    expect(await within(diag).findByText('Not ready')).toBeInTheDocument()
    expect(within(diag).getByText('error: locked')).toBeInTheDocument()
  })

  it('重建索引、复位熔断器都先在页面里确认，结果照服务端回的写', async () => {
    const confirm = vi.spyOn(window, 'confirm')
    const calls = serve()
    show()
    const schema = card('Schema index')
    await userEvent.click(within(schema).getByRole('button', { name: 'Rebuild schema index' }))
    expect(calls).not.toContain('POST /sql-api/admin/schema/reload')
    await userEvent.click(within(within(schema).getByRole('alertdialog')).getByRole('button', { name: 'Confirm' }))
    expect(await within(schema).findByText('Index rebuilt. Changed: erp_db')).toBeInTheDocument()

    const circuit = card('Model circuit breakers')
    await userEvent.click(within(circuit).getByRole('button', { name: 'Reset model circuit breakers' }))
    await userEvent.click(within(within(circuit).getByRole('alertdialog')).getByRole('button', { name: 'Cancel' }))
    expect(calls).not.toContain('POST /sql-api/admin/circuit/reset')
    await userEvent.click(within(circuit).getByRole('button', { name: 'Reset model circuit breakers' }))
    await userEvent.click(within(within(circuit).getByRole('alertdialog')).getByRole('button', { name: 'Confirm' }))
    expect(await within(circuit).findByText('Circuit breakers reset.')).toBeInTheDocument()
    expect(confirm).not.toHaveBeenCalled()
  })

  it('有反馈时写准确率、静默错误率，和最近判错的问题', async () => {
    serve({ 'GET /sql-api/feedback/stats': () => json({
      stats: { ...feedbackStats.stats, total: 8, correct: 6, wrong: 2, accuracy: 0.75, silent_error_rate: 0.125, unresolved_wrong: 1 },
      recent_wrong: [{ trace_id: 't1', query: 'How many open orders?', comment: 'missed one', had_risk: false, resolved: false }],
    }) })
    show()
    const feedback = card('Feedback on answers')
    expect(await within(feedback).findByText('75%')).toBeInTheDocument()
    expect(within(feedback).getByText('12.5%')).toBeInTheDocument()
    expect(within(feedback).getByText('How many open orders?')).toBeInTheDocument()
    expect(within(feedback).getByText('missed one')).toBeInTheDocument()
  })
})
