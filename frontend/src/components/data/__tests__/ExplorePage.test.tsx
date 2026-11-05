/**
 * 数据浏览整页：用 SQL Agent 录下来的真实响应（src/test/fixtures/sql/，演示库）回放。
 *
 * 地址是这一页的状态：排序、筛选、标签页都写进 hash（不产生新的历史记录），这里经真的路由（useRoute）
 * 把地址交回页面，验的是"点了之后地址变了、按新地址重新取了数"这一整圈。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@/test/render'
import { fixtureJson } from '@/test/fixtures'
import type { Catalog, CatalogTable, RowsPage, TableProfile } from '@/api/sql/types'
import { takeAskDraft } from '@/lib/sql/askDraft'
import { labelOf } from '@/lib/sql/format'
import { useRoute } from '@/lib/route'
import { ExplorePage } from '../ExplorePage'

const CATALOG = fixtureJson<Catalog>('sql/catalog.json')
const ROWS = fixtureJson<RowsPage>('sql/rows.json')
const PROFILE = fixtureJson<TableProfile>('sql/profile.json')
const TABLE = fixtureJson<CatalogTable>('sql/table.json')
const ROWS_ERROR = fixtureJson<{ detail: { code: string; message: string } }>('sql/rows-error.json')

// 录夹具时看的那张表（ai-sql-agent/scripts/record_console_fixtures.py）：库和表名从夹具里取，不在测试里另写一份
const DATABASE = ROWS.database
const TABLE_NAME = ROWS.table
const OTHER_TOTAL = 10

interface Call { method: string; path: string; body: Record<string, unknown> | null }

function smallPage(database: string, table: string): RowsPage {
  const columns = (CATALOG.domains.find((domain) => domain.database === database)?.tables
    .find((entry) => entry.name === table)?.columns ?? []).slice(0, 3)
  return {
    ...ROWS, database, table, page_size: 5, total_rows: 1, has_more: false, date_ranges: [],
    columns: columns.map((column) => ({ name: column.name, kind: column.kind, sortable: true })),
    rows: [columns.map((_, index) => `cell-${index}`)],
  }
}

function serve({ rows = () => Response.json(ROWS) }: { rows?: () => Response } = {}) {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const path = url.replace(/^\/sql-api/, '').split('?')[0]!
    const method = init?.method ?? 'GET'
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : null
    calls.push({ method, path, body })
    const parts = path.split('/').map(decodeURIComponent)
    if (path === '/catalog') return Response.json(CATALOG)
    if (parts[4] === 'rows') {
      return parts[2] === DATABASE && parts[3] === TABLE_NAME ? rows() : Response.json(smallPage(parts[2]!, parts[3]!))
    }
    if (parts[4] === 'profile') {
      return parts[2] === DATABASE && parts[3] === TABLE_NAME
        ? Response.json(PROFILE)
        : Response.json({ ...PROFILE, database: parts[2], table: parts[3], total_rows: OTHER_TOTAL, columns: [], facets: [], timelines: [] })
    }
    if (parts.length === 4) return Response.json(TABLE)
    return Response.json({ detail: 'not found' }, { status: 404 })
  }))
  return calls
}

function Harness() {
  const { route } = useRoute()
  if (route.view !== 'data' || route.page !== 'explore') return <p>left the data browser</p>
  return <ExplorePage database={route.database ?? ''} table={route.table ?? ''} query={route.query} />
}

function show(hash: string) {
  window.location.hash = hash
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<Harness />, {
    lang: 'en',
    wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
  })
}

const column = (name: string) => TABLE.columns!.find((item) => item.name === name)!
const firstRow = ROWS.rows[0]!

beforeEach(() => { window.location.hash = '' })
afterEach(() => { vi.unstubAllGlobals() })

describe('数据浏览：全部数据和一个库', () => {
  it('列出目录里的每个库，记录数是每张表各数一遍再加起来', async () => {
    const calls = serve()
    show('#/data/explore')
    // 目录到之前也有一个同名标题（加载中的那一页），等关联矩阵出来才算整页到了
    expect(await screen.findByRole('heading', { name: 'How the data connects' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'All data', level: 2 })).toBeInTheDocument()
    for (const domain of CATALOG.domains) {
      const title = typeof domain.title === 'string' ? domain.title : domain.title.en
      expect(screen.getAllByRole('link', { name: new RegExp(title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')) }).length).toBeGreaterThan(0)
    }
    const tables = CATALOG.domains.reduce((sum, domain) => sum + domain.tables.length, 0)
    const records = await screen.findByText('Records', { selector: '.sq-kpi-label' })
    const total = PROFILE.total_rows + (tables - 1) * OTHER_TOTAL
    await waitFor(() => expect(within(records.closest('.sq-kpi')!).getByText(String(total))).toBeInTheDocument())
    // 每张表一个只要行数的请求，不带分布
    const profiles = calls.filter((call) => call.path.endsWith('/profile'))
    expect(profiles).toHaveLength(tables)
    expect(profiles.every((call) => call.body?.breakdowns === false)).toBe(true)
  })

  it('一个库：每张表一张卡片，带分布的概况只为这个库要', async () => {
    const calls = serve()
    show(`#/data/explore/${DATABASE}`)
    const domain = CATALOG.domains.find((item) => item.database === DATABASE)!
    const cards = await screen.findAllByRole('article')
    expect(cards.map((card) => within(card).getAllByRole('link')[0]!.textContent))
      .toEqual(domain.tables.map((table) => labelOf(table, 'en')))
    await waitFor(() => expect(calls.filter((call) => call.path.endsWith('/profile') && call.body?.breakdowns === true)
      .map((call) => call.path.split('/')[3]).sort()).toEqual(domain.tables.map((table) => table.name).sort()))
  })
})

describe('数据浏览：一张表', () => {
  it('记录照服务端给的显示；点列头排序、点概览里的取值筛选，都经地址重新取数', async () => {
    const calls = serve()
    show(`#/data/explore/${DATABASE}/${TABLE_NAME}`)
    expect(await screen.findByText(String(firstRow[0]))).toBeInTheDocument()

    const amount = 'order_amt'
    fireEvent.click(screen.getByTitle(`Sort by ${labelOf(column(amount), 'en')}`))
    await waitFor(() => expect(window.location.hash).toContain(`sort=${amount}.asc`))
    await waitFor(() => expect(calls.some((call) => call.path.endsWith('/rows')
      && JSON.stringify(call.body?.sort) === JSON.stringify({ column: amount, direction: 'asc' }))).toBe(true))

    const facet = PROFILE.facets.find((item) => item.ordered_by === 'declared')!
    const value = String(facet.values[0]!.value)
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(`^${value}`) }))
    await waitFor(() => expect(window.location.hash).toContain(`f=${facet.column}%3Aeq%3A${value}`))
    await waitFor(() => expect(calls.some((call) => call.path.endsWith('/rows')
      && JSON.stringify(call.body?.filters) === JSON.stringify([{ column: facet.column, op: 'eq', value }]))).toBe(true))
    expect(screen.getByRole('button', { name: new RegExp(`Remove filter: .*${value}`) })).toBeInTheDocument()
  })

  it('点开一条记录：全部字段，以及目录登记的关联表里键值相同的记录', async () => {
    const calls = serve()
    show(`#/data/explore/${DATABASE}/${TABLE_NAME}`)
    const cell = await screen.findByText(String(firstRow[0]))
    fireEvent.click(cell.closest('tr')!)
    const drawer = await screen.findByRole('complementary', { name: 'Record details' })
    expect(within(drawer).getByRole('heading', { level: 3 })).toHaveTextContent(String(firstRow[0]))

    // 关联只按目录登记的 references 走：这张表上登记了关联的字段，拿它的值去对面那张表筛
    const linked = TABLE.columns!.find((item) => item.references.length)!
    const reference = linked.references[0]!
    const linkedValue = String(firstRow[ROWS.columns.findIndex((item) => item.name === linked.name)])
    await waitFor(() => expect(calls.some((call) => call.path === `/catalog/${reference.database}/${reference.table}/rows`
      && JSON.stringify(call.body?.filters) === JSON.stringify([{ column: reference.column, op: 'eq', value: linkedValue }]))).toBe(true))
    expect(within(drawer).getByRole('heading', { name: 'Matching records elsewhere' })).toBeInTheDocument()

    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('complementary', { name: 'Record details' })).not.toBeInTheDocument())
  })

  it('字段标签页：数据字典里的单位、类型和整张表的填写率', async () => {
    serve()
    show(`#/data/explore/${DATABASE}/${TABLE_NAME}`)
    fireEvent.click(await screen.findByRole('tab', { name: `Fields (${TABLE.columns!.length})` }))
    await waitFor(() => expect(window.location.hash).toContain('tab=fields'))
    const amount = column('order_amt')
    expect(await screen.findByText(`Unit: ${amount.unit}`)).toBeInTheDocument()
    expect(screen.getByText(amount.name, { selector: '.sq-tech' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByText(`${PROFILE.total_rows} of ${PROFILE.total_rows} filled in`))
      .toHaveLength(PROFILE.columns.length))
  })

  it('预览出错按服务端的错误码说人话，原话留在小字里', async () => {
    serve({ rows: () => Response.json(ROWS_ERROR, { status: 422 }) })
    show(`#/data/explore/${DATABASE}/${TABLE_NAME}`)
    expect(await screen.findByText('These records couldn’t be loaded')).toBeInTheDocument()
    expect(screen.getByText('That field isn’t available in this table.')).toBeInTheDocument()
    expect(screen.getByText(new RegExp(ROWS_ERROR.detail.code))).toBeInTheDocument()
  })

  it('库里的文字只当文本显示：表格、概览、记录详情里都不会变成 HTML', async () => {
    const markup = '<img src=x onerror=alert(1)>'
    const rows = { ...ROWS, rows: ROWS.rows.map((row, index) => (index === 0 ? [row[0], row[1], markup, ...row.slice(3)] : row)) }
    PROFILE.facets[1]!.values[0]!.value = markup
    try {
      serve({ rows: () => Response.json(rows) })
      show(`#/data/explore/${DATABASE}/${TABLE_NAME}`)
      const cell = await screen.findByText(markup, { selector: 'td' })
      fireEvent.click(cell.closest('tr')!)
      await screen.findByRole('complementary', { name: 'Record details' })
      await waitFor(() => expect(screen.getAllByText(markup).length).toBeGreaterThanOrEqual(2))
      expect(document.querySelector('img')).toBeNull()
    } finally {
      PROFILE.facets[1]!.values[0]!.value = fixtureJson<TableProfile>('sql/profile.json').facets[1]!.values[0]!.value
    }
  })

  it('"就这份数据提问"把话头交给问数页，填进输入框、不自动发', async () => {
    serve()
    show(`#/data/explore/${DATABASE}/${TABLE_NAME}`)
    fireEvent.click(await screen.findByRole('button', { name: 'Ask about this data' }))
    expect(window.location.hash).toBe('#/data/ask')
    expect(takeAskDraft()).toEqual({ question: `In ${labelOf(TABLE, 'en')}: `, send: false })
  })

  it('目录里没有的表照实说没有', async () => {
    serve()
    show(`#/data/explore/${DATABASE}/no_such_table`)
    expect(await screen.findByRole('heading', { name: 'This table isn’t available', level: 2 })).toBeInTheDocument()
  })
})
