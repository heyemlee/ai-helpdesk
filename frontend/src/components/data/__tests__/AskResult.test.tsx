/**
 * 问数页一轮答案的画法（移植自 test_console_ask.py 的"答案"一组和 test_console_run_cancel.py 的停止提示）。
 *
 * 结果面板有三种看法（数字、图、表），是同一批行的不同画法；每一种都要把来自数据库的文字按文本输出。
 * 等待时的进度只认真实收到的阶段事件；停止之后的提示要和服务端那边的实际情况对得上。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import userEvent from '@testing-library/user-event'
import { render, screen, within } from '@/test/render'
import type { Catalog } from '@/api/sql/types'
import type { Lang } from '@/i18n'
import { CATALOG, type Key } from '@/i18n/catalog'
import { summaryText } from '@/lib/sql/ask-summary'
import { normalizeResult } from '@/lib/sql/model'
import { TurnView } from '../ask/Answer'
import type { CancelState, Turn } from '../ask/turn'

const EVIL = '<img src=x onerror=alert(1)>'
const COUNTED = 'SELECT stage, COUNT(*) AS n FROM deal GROUP BY stage'

const tr = (lang: Lang) => (key: Key, vars?: Record<string, string | number>): string => {
  const text = (CATALOG[lang] as Record<string, string>)[key] ?? key
  const forms = text.split('|')
  const raw = forms.length === 2 && typeof vars?.n === 'number' ? forms[vars.n === 1 ? 0 : 1]! : text
  return vars ? raw.replace(/\{(\w+)\}/g, (m, k: string) => (k in vars ? String(vars[k]) : m)) : raw
}

const step = (rows: Array<Record<string, unknown>>, extra: Record<string, unknown> = {}) => ({
  database: 'sales_db', sql: COUNTED, final_sql: COUNTED, success: true, row_count: rows.length, rows, ...extra,
})

function turnOf(payload: Record<string, unknown>, extra: Partial<Turn> = {}): Turn {
  return {
    id: 't1', question: 'How many?', askedAt: new Date().toISOString(), status: 'done', error: null,
    transport: 'query', stages: [], stageNow: null, startedAt: 0, runId: '', cancel: '', rerun: false, backfill: null,
    result: normalizeResult({
      trace_id: 'trace-1', route: 'database_query', queried_database: true, has_risk: false, degraded: false,
      execution_status: 'success', ...payload,
    }, { transport: 'query', clientMs: 1234 }),
    ...extra,
  }
}

const noop = () => {}
function show(turn: Turn, catalog: Catalog | null = null) {
  return render(
    <TurnView turn={turn} catalog={catalog} service={null} learning={false} now={0}
              onStop={noop} onAskAgain={noop} onFollowUp={noop} />,
    { lang: 'en' },
  )
}

/** 一块面板现在是什么样：哪几种看法在、按下的是哪个、有没有说明、库里来的文字是不是按文本出来的 */
function facts(container: HTMLElement) {
  const group = container.querySelector('[role="group"]')
  return {
    figures: !!container.querySelector('.sq-stat-tiles'),
    bars: !!container.querySelector('.sq-bars'),
    columns: !!container.querySelector('.sq-cols'),
    table: !!container.querySelector('table.sq-table'),
    switcher: group ? group.querySelectorAll('button').length : 0,
    pressed: group?.querySelector('[aria-pressed="true"]')?.textContent ?? '',
    legend: !!container.querySelector('details.sq-legend'),
    raw: !!container.querySelector('img'),
    escaped: (container.textContent ?? '').includes(EVIL),
  }
}

const SALES_CATALOG = {
  domains: [{
    database: 'sales_db', status: 'available', kind: '', aliases: [], title: { en: 'Sales', zh: '销售' }, tables: [{
      name: 'deal', status: 'available', aliases: [], columns: [
        { name: 'stage', status: 'available', role: 'filter', kind: 'text', aliases: [], description: 'Where the deal stands.' },
        { name: 'amount', status: 'available', role: 'metric', kind: 'number', unit: 'USD', aliases: [], description: 'Agreed price.' },
        { name: 'seq', status: 'available', role: 'join_key', kind: 'number', aliases: [], description: 'Running number.' },
      ],
    }],
  }],
} as unknown as Catalog
const PLAIN = { sql: 'SELECT stage, amount FROM deal', final_sql: 'SELECT stage, amount FROM deal' }

afterEach(() => { vi.unstubAllGlobals() })

describe('结果面板的几种看法', () => {
  it('每个结果先用最清楚的那种看法，也都能切到表格', async () => {
    const chart = show(turnOf({ steps: [step([{ stage: EVIL, n: 3 }, { stage: 'b"c', n: 5 }])] }))
    expect(facts(chart.container)).toMatchObject({ bars: true, table: false, pressed: 'Chart', switcher: 2 })
    await userEvent.click(within(chart.container).getByRole('button', { name: 'Table' }))
    expect(facts(chart.container)).toMatchObject({ bars: false, table: true, pressed: 'Table' })
    chart.unmount()

    const figures = show(turnOf({ steps: [step([{ customer: EVIL, total: 12.5 }], { column_units: { total: 'USD' } })] }))
    expect(facts(figures.container)).toMatchObject({ figures: true, table: false })
    expect(figures.container.querySelector('.sq-stat-unit')?.textContent).toBe('USD')
    await userEvent.click(within(figures.container).getByRole('button', { name: 'Table' }))
    expect(facts(figures.container).table).toBe(true)
    figures.unmount()

    // 只有表格这一种看法时不画开关
    const wide = show(turnOf({ steps: [step([{ a: EVIL, b: 1, c: 2 }, { a: 'x', b: 3, c: 4 }])] }))
    expect(facts(wide.container)).toMatchObject({ table: true, switcher: 0 })
  })

  it('记下来的选择对不上任何一种看法时，退回默认的那种', async () => {
    const view = show(turnOf({ steps: [step([{ stage: 'a', n: 3 }, { stage: 'b', n: 5 }])] }))
    await userEvent.click(within(view.container).getByRole('button', { name: 'Chart' }))
    // 同一轮重画，这一步的结果已经画不成图了（有一个负数）：按下过的"图表"不能留下一个空面板
    view.rerender(
      <TurnView turn={turnOf({ steps: [step([{ stage: 'a', n: 3 }, { stage: 'b', n: -5 }])] })} catalog={null}
                service={null} learning={false} now={0} onStop={noop} onAskAgain={noop} onFollowUp={noop} />,
    )
    expect(facts(view.container)).toMatchObject({ bars: false, table: true, switcher: 0 })
  })

  it('连着的月份：柱子和表格一起出；最高的那个月写出来，并列的都写，全一样就不写', () => {
    const monthly = (rows: Array<Record<string, unknown>>) => show(turnOf({ steps: [step(rows)] }))
    const note = (container: HTMLElement) => container.querySelector('.sq-chart-note')?.textContent ?? ''
    const series = monthly([{ month: '2025-01', n: 7 }, { month: '2025-02', n: 12 }, { month: '2025-03', n: 9 }])
    expect(facts(series.container)).toMatchObject({ columns: true, table: true, switcher: 0 })
    expect(note(series.container)).toBe('Highest: 2025-02 (12)')
    series.unmount()
    const tied = monthly([{ month: '2025-01', n: 12 }, { month: '2025-02', n: 12 }, { month: '2025-03', n: 9 }])
    expect(note(tied.container)).toBe('Highest: 2025-01, 2025-02 (12)')
    tied.unmount()
    const allTied = monthly([{ month: '2025-01', n: 5 }, { month: '2025-02', n: 5 }, { month: '2025-03', n: 5 }, { month: '2025-04', n: 5 }])
    expect(note(allTied.container)).toBe('')
  })

  it('列说明在每一种看法下都有；数据字典里一列都对不上时不画', async () => {
    const listed = show(turnOf({ steps: [step([{ stage: 'won', amount: 300 }, { stage: 'lost', amount: 120 }], PLAIN)] }), SALES_CATALOG)
    // 登记成指标的列：没有聚合也画
    expect(facts(listed.container)).toMatchObject({ bars: true, pressed: 'Chart', legend: true })
    await userEvent.click(within(listed.container).getByRole('button', { name: 'Table' }))
    expect(facts(listed.container)).toMatchObject({ table: true, legend: true })
    listed.unmount()

    const figures = show(turnOf({ steps: [step([{ stage: 'won', amount: 300 }], PLAIN)] }), SALES_CATALOG)
    expect(facts(figures.container)).toMatchObject({ figures: true, legend: true })
    figures.unmount()

    // 编号是数字但不是量
    const identifiers = show(turnOf({ steps: [step([{ stage: 'won', seq: 7 }, { stage: 'lost', seq: 8 }],
      { sql: 'SELECT stage, seq FROM deal', final_sql: 'SELECT stage, seq FROM deal' })] }), SALES_CATALOG)
    expect(facts(identifiers.container)).toMatchObject({ bars: false, table: true })
    identifiers.unmount()

    const unlisted = show(turnOf({ steps: [step([{ stage: 'a', n: 3 }, { stage: 'b', n: 5 }])] }))
    expect(facts(unlisted.container).legend).toBe(false)
  })

  it('有一步没有返回行：它查的那个库不能从页面上消失', () => {
    const view = show(turnOf({ steps: [
      step([{ stage: 'a', n: 3 }, { stage: 'b', n: 4 }]),
      step([], { database: 'stock_db', sql: 'SELECT zone, qty FROM bin WHERE qty < 0', final_sql: 'SELECT zone, qty FROM bin WHERE qty < 0' }),
    ] }))
    expect(view.container.querySelectorAll('.sq-result-panel')).toHaveLength(1)
    const meta = screen.getByTestId('result-meta').textContent ?? ''
    expect(meta).toContain('stock_db')
    expect(meta).toContain('sales_db')
    view.unmount()

    // 每一步都有面板时来源已经写在面板头上，不在下面重复一遍
    show(turnOf({ steps: [step([{ stage: 'a', n: 3 }, { stage: 'b', n: 4 }])] }))
    expect(screen.getByTestId('result-meta').textContent).not.toContain('sales_db')
  })

  it('被截断的结果从不画成图，复制出去的结论也带着这句', () => {
    const truncated = turnOf({ result_truncated: true,
      steps: [step([{ stage: 'a', n: 3 }, { stage: 'b', n: 4 }], { truncated: true, row_limit: 2 })] })
    const view = show(truncated)
    expect(facts(view.container)).toMatchObject({ bars: false, table: true })
    expect(screen.getByText('Not all rows are shown')).toBeInTheDocument()
    expect(summaryText(truncated.question, truncated.result!, { t: tr('en'), lang: 'en', catalog: null }))
      .toContain('Not all rows are shown')
  })

  it('库里来的文字在每一种看法下都按文本输出', async () => {
    for (const rows of [
      [{ stage: EVIL, n: 3 }, { stage: 'b', n: 5 }],              // 图
      [{ customer: EVIL, total: 12.5 }],                          // 数字
      [{ a: EVIL, b: 1, c: 2 }, { a: 'x', b: 3, c: 4 }],          // 表
    ]) {
      const view = show(turnOf({ steps: [step(rows)] }))
      expect(facts(view.container)).toMatchObject({ raw: false, escaped: true })
      const table = within(view.container).queryByRole('button', { name: 'Table' })
      if (table) {
        await userEvent.click(table)
        expect(facts(view.container)).toMatchObject({ raw: false, escaped: true })
      }
      view.unmount()
    }

    // 对话回答：认加粗，不认 HTML，也没有结果面板
    const chat = show(turnOf({ route: 'data_qa', queried_database: false, execution_status: 'not_applicable',
      answer: `**hi** ${EVIL}`, steps: [] }))
    expect(chat.container.querySelector('strong')?.textContent).toBe('hi')
    expect(chat.container.querySelector('img')).toBeNull()
    expect(chat.container.querySelector('.sq-result-panel')).toBeNull()
    chat.unmount()

    // 请求没成功时服务端的原话也是文本
    const { SqlApiError } = await import('@/api/sql/client')
    const failed = show(turnOf({}, { status: 'error', result: null,
      error: new SqlApiError({ status: 401, message: EVIL }) }))
    expect(failed.container.querySelector('img')).toBeNull()
    expect(failed.container.textContent).toContain(EVIL)
    expect(screen.getByText(/rejected the console’s access key/)).toBeInTheDocument()
  })
})

describe('等待、停止、链到这次查询', () => {
  it('走流式时进度只画收到的阶段；走 /query 时只有一条不表示进度的动效', () => {
    const pending = (transport: Turn['transport'], stages: string[]) => turnOf({}, {
      status: 'pending', result: null, transport, startedAt: 0,
      stages: stages.map((stage) => ({ stage, step: null, database: '', shortPath: null, withMemory: null, offsetMs: null })),
      stageNow: stages.length ? { stage: stages.at(-1)!, step: null, database: '', shortPath: null, withMemory: null, offsetMs: null } : null,
    })
    const streamed = show(pending('stream', ['normalization', 'retrieval']))
    expect(streamed.container.querySelector('.sq-stage-track')).not.toBeNull()
    expect(streamed.container.querySelector('.sq-stage.is-now')?.textContent).toBe('Find tables')
    expect(streamed.container.querySelector('.sq-stage.is-done')?.textContent).toBe('Understand')
    expect(streamed.container.querySelector('.sq-progress-indet')).toBeNull()
    expect(screen.getByRole('status')).toHaveTextContent('Finding the right tables')
    expect(within(streamed.container).getByRole('button', { name: 'Stop' })).toBeInTheDocument()
    streamed.unmount()

    const plain = show(pending('query', []))
    expect(plain.container.querySelector('.sq-stage-track')).toBeNull()
    expect(plain.container.querySelector('.sq-progress-indet')).not.toBeNull()
    expect(plain.container.querySelector('.is-done')).toBeNull()
    expect(screen.getByRole('status')).toHaveTextContent('Working on your question…')
    expect(within(plain.container).getByRole('button', { name: 'Stop' })).toBeInTheDocument()
  })

  it('停止之后的提示照服务端那边的实际情况写', () => {
    const shown = (cancel: CancelState) => {
      const view = show(turnOf({}, { status: 'stopped', result: null, cancel }))
      const text = view.container.textContent ?? ''
      expect(within(view.container).getByRole('button', { name: /Ask again/ })).toBeInTheDocument()
      view.unmount()
      return text
    }
    const STOPPED = 'Closing the connection stops it on the server too'
    const CANCELLED = 'The server was asked to stop it as well'
    const LATE = 'It had already finished on the server'
    // 请求还在路上或已受理：服务端一并停下
    for (const cancel of ['sent', 'accepted'] as const) {
      const text = shown(cancel)
      expect(text).toContain(CANCELLED)
      expect(text).not.toContain(STOPPED)
    }
    expect(shown('finished')).toContain(LATE)
    // /query 没有运行号，或者取消请求本身失败：只剩断开连接这一条路
    for (const cancel of ['', 'failed'] as const) {
      const text = shown(cancel)
      expect(text).toContain(STOPPED)
      expect(text).not.toContain(CANCELLED)
    }
  })

  it('答完的一轮链到这次查询的经过', () => {
    show(turnOf({ steps: [step([{ stage: 'a', n: 3 }, { stage: 'b', n: 5 }])] }, { runId: 'abc 123' }))
    expect(screen.getByRole('link', { name: /See how it ran/ })).toHaveAttribute('href', '#/data/runs/abc%20123')
  })
})

describe('反馈', () => {
  it('只发 trace_id、判定和补充说明：问题原文和 SQL 由服务端按审计记录回填', async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => new Response(
      JSON.stringify({ trace_id: 'trace-fb', verdict: 'correct', recorded: true }),
      { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    show(turnOf({ trace_id: 'trace-fb', steps: [step([{ stage: 'a', n: 3 }, { stage: 'b', n: 5 }])] }))

    await userEvent.click(screen.getByRole('button', { name: 'Add a comment' }))
    await userEvent.type(screen.getByRole('textbox', { name: 'Comment' }), '  close enough  ')
    await userEvent.click(screen.getByRole('button', { name: 'Yes' }))

    expect(await screen.findByText(/marked as correct/)).toBeInTheDocument()
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/sql-api/feedback')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual({ trace_id: 'trace-fb', verdict: 'correct', comment: 'close enough' })
    expect(init?.headers).not.toHaveProperty('X-API-Key')
  })
})
