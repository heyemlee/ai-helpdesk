/**
 * 数据浏览的纯逻辑，改写自 ai-sql-agent/tests/test_console_logic.py 里对应的几条（那边用 Node 跑旧控制台的
 * dataRoute.js、links.js、charts.js、table.js、format.js）：一张表"正在怎么看"和地址互转、表与表怎么连、
 * 时间轴怎么并、取值怎么配色、数量怎么写、数字怎么排。
 */
import { describe, expect, it } from 'vitest'
import type { CatalogDomain } from '@/api/sql/types'
import { rebucket, seriesSlots } from '../charts'
import { exploreHref, formatView, freshView, parseView, type TableView } from '../dataView'
import { formatCompact, formatCount, formatShare } from '../format'
import { buildLinks, crossDatabaseLinks, relatedTables, type LinkNode } from '../links'
import { sortRows } from '../table'
import { chipItems, emptyColumns, operatorsFor, worthShowing } from '@/components/data/explore/tableLogic'

describe('一张表"正在怎么看"和地址互转', () => {
  it('搜索词、筛选值、字段名里的冒号、问号、斜杠、中文都不影响拆分；默认的样子不往地址里写东西', () => {
    const view: TableView = {
      ...freshView(), search: 'a b&c', page: 3, pageSize: 50, tab: 'fields',
      sort: { key: 'signed on', direction: 'desc' },
      filters: [
        { column: 'status', op: 'eq', value: '已取消' },
        { column: 'odd:name?x/y', op: 'contains', value: 'a:b&c=d #1' },
        { column: 'notes', op: 'is_empty', value: '' },
      ],
    }
    expect(parseView(formatView(view))).toEqual(view)
    expect(formatView(freshView())).toBe('')
    expect([exploreHref(), exploreHref('x_db'), exploreHref('', 'ignored')])
      .toEqual(['#/data/explore', '#/data/explore/x_db', '#/data/explore'])
    expect(exploreHref('shop db', 'a/b?c', { filters: [{ column: 'k', op: 'eq', value: 'J-1' }] }))
      .toBe('#/data/explore/shop%20db/a%2Fb%3Fc?f=k%3Aeq%3AJ-1')
  })

  it('认不出的部分丢掉，不报错：地址是人会手改、会被截断的东西', () => {
    const junk = parseView('q=x&f=broken&f=a:regexp:b&f=%E0%A4%A:eq:1&sort=nodirection&page=-2&size=7&tab=sql&f=ok:eq:1')
    expect(junk.search).toBe('x')
    expect(junk.filters).toEqual([{ column: 'ok', op: 'eq', value: '1' }])
    expect(junk.sort).toBeNull()
    expect([junk.page, junk.pageSize, junk.tab]).toEqual([1, 25, 'records'])
    // 条数超过服务端的上限也不往下传
    expect(parseView(Array.from({ length: 9 }, (_, i) => `f=c${i}:eq:1`).join('&')).filters).toHaveLength(5)
  })
})

describe('表与表怎么连', () => {
  const column = (name: string, references: Array<[string, string, string, string]> = [], extra = {}) => ({
    name, references: references.map(([relation, database, table, col]) => ({ relation, database, table, column: col })), ...extra,
  })
  const domains = [
    { database: 'a_db', tables: [
      { name: 'orders', columns: [
        column('order_no', [
          ['cross_database', 'b_db', 'work', 'order_no'],
          ['cross_database', 'c_db', 'ticket', 'ref_no'],
          ['cross_database', 'z_db', 'hidden', 'order_no'],       // 对端不在目录里（没权限）
        ], { primary_key: true }),
        column('customer'),
      ] },
      { name: 'items', columns: [column('order_no', [['foreign_key', 'a_db', 'orders', 'order_no']]), column('sku')] },
    ] },
    { database: 'b_db', tables: [
      { name: 'work', columns: [column('order_no', [['cross_database', 'a_db', 'orders', 'order_no']])] },
    ] },
    { database: 'c_db', tables: [
      { name: 'ticket', columns: [
        column('ref_no', [['cross_database', 'a_db', 'orders', 'order_no']]),
        column('part', [['cross_database', 'c_db', 'stock', 'name']]),
      ] },
      { name: 'stock', columns: [column('name', [['cross_database', 'c_db', 'ticket', 'part']], { primary_key: true })] },
    ] },
  ] as unknown as CatalogDomain[]
  const links = buildLinks(domains)
  const name = (member: LinkNode) => `${member.database}.${member.table}.${member.column}`
  const related = (database: string, table: string) =>
    relatedTables(links, database, table).map((item) => [item.column, item.others.map(name)])

  it('线只按目录登记的关联画：连得最多的那个是本家，一样多时取主键；对端看不见的那条不画', () => {
    expect(links.edges).toHaveLength(4)
    expect(links.groups.map((group) => ({ hub: name(group.hub), members: group.members.map(name), databases: group.databases })))
      .toEqual([
        {
          hub: 'a_db.orders.order_no',
          members: ['a_db.orders.order_no', 'a_db.items.order_no', 'b_db.work.order_no', 'c_db.ticket.ref_no'],
          databases: ['a_db', 'b_db', 'c_db'],
        },
        { hub: 'c_db.stock.name', members: ['c_db.ticket.part', 'c_db.stock.name'], databases: ['c_db'] },
      ])
    expect(JSON.stringify(links.groups.map((group) => group.members.map(name)))).not.toContain('z_db')
    expect(buildLinks(domains)).toBe(links)      // 按目录那份数组缓存
    expect(buildLinks(null).groups).toHaveLength(0)
  })

  it('相关的表沿着共用的键传递：工单只登记了和订单的关联，同一个订单号也找得到明细和生产记录', () => {
    expect(related('c_db', 'ticket')).toEqual([
      ['ref_no', ['a_db.orders.order_no', 'a_db.items.order_no', 'b_db.work.order_no']],
      ['part', ['c_db.stock.name']],
    ])
    expect(related('c_db', 'stock')).toEqual([['name', ['c_db.ticket.part']]])
    expect(related('a_db', 'nothing')).toEqual([])
    expect(crossDatabaseLinks(links, 'a_db').map((group) =>
      [group.domain.database, group.links.map((link) => `${name(link.mine)}>${name(link.other)}`)]))
      .toEqual([
        ['b_db', ['a_db.orders.order_no>b_db.work.order_no']],
        ['c_db', ['a_db.orders.order_no>c_db.ticket.ref_no']],
      ])
  })
})

describe('时间轴怎么并、取值怎么配色、数量怎么写', () => {
  const months = (from: number, count: number) => Array.from({ length: count }, (_, offset) => {
    const index = from + offset
    return { period: `${Math.floor(index / 12)}-${String((index % 12) + 1).padStart(2, '0')}`, count: 1 }
  })

  it('没有记录的月份也是一个月；太长的并成季度（按日历对齐）、再长并成年', () => {
    expect(rebucket([{ period: '2025-11', count: 3 }, { period: '2026-02', count: 5 }, { period: 'junk', count: 9 }])).toEqual({
      grain: 'month', bars: [
        { label: '2025-11', start: '2025-11-01', end: '2025-12-01', count: 3 },
        { label: '2025-12', start: '2025-12-01', end: '2026-01-01', count: 0 },
        { label: '2026-01', start: '2026-01-01', end: '2026-02-01', count: 0 },
        { label: '2026-02', start: '2026-02-01', end: '2026-03-01', count: 5 },
      ],
    })
    const quarters = rebucket(months(2020 * 12 + 1, 40))      // 2020-02 起 40 个月
    expect([quarters.grain, quarters.bars.length]).toEqual(['quarter', 14])
    expect(quarters.bars[0]).toEqual({ label: '2020 Q1', start: '2020-01-01', end: '2020-04-01', count: 2 })
    expect(quarters.bars.at(-1)).toEqual({ label: '2023 Q2', start: '2023-04-01', end: '2023-07-01', count: 2 })
    const years = rebucket(months(2000 * 12, 200))
    expect([years.grain, years.bars.length]).toEqual(['year', 17])
    expect(years.bars[0]).toEqual({ label: '2000', start: '2000-01-01', end: '2001-01-01', count: 12 })
    expect(years.bars.at(-1)).toEqual({ label: '2016', start: '2016-01-01', end: '2017-01-01', count: 8 })
    expect(rebucket([])).toEqual({ grain: 'month', bars: [] })
  })

  it('颜色跟着取值走，不跟着名次走；超过六个或者没取全就不上色', () => {
    const facet = (values: Array<string | number>, extra = {}) => ({
      complete: true, ordered_by: 'count', values: values.map((value, index) => ({ value, count: 10 - index })), ...extra,
    })
    const slots = (item: Parameters<typeof seriesSlots>[0]) => [...seriesSlots(item)]
    expect(slots(facet(['open', 'waiting', 'closed'], { ordered_by: 'declared' })))
      .toEqual([['open', 1], ['waiting', 2], ['closed', 3]])
    expect(slots(facet(['zeta', 'alpha', 10, 9]))).toEqual([['9', 1], ['10', 2], ['alpha', 3], ['zeta', 4]])
    expect(slots(facet(['a', 'b', 'c', 'd', 'e', 'f', 'g']))).toEqual([])
    expect(slots(facet(['a', 'b'], { complete: false }))).toEqual([])
    expect(slots(null)).toEqual([])
  })

  it('数量写给人读：记录数带千分位，大数写短，不是零的一份不显示成 0%', () => {
    expect(formatCount(1200, 'en')).toBe('1,200')
    expect([formatCompact(1200, 'en'), formatCompact(47029260.75, 'en'), formatCompact(2356.65, 'en', { unit: 'USD' }),
      formatCompact(39.5, 'en')]).toEqual(['1,200', '47M', '2,356.65', '39.5'])
    expect([formatShare(1, 1200, 'en'), formatShare(550, 1200, 'en'), formatShare(60, 1200, 'en'), formatShare(0, 10, 'en'),
      formatShare(1, 0, 'en')]).toEqual(['<0.1%', '46%', '5%', '0%', ''])
  })
})

describe('数字以文本到达时也按数字排', () => {
  const column = (values: unknown[], direction: 'asc' | 'desc' = 'asc') =>
    sortRows(values.map((value) => [value]), 0, direction).map((row) => row[0])

  it('负数、小数、超过 2^53 的整数、混着类型的、空值都排对', () => {
    expect(column(['-0.50', '-9.00', '-10.00', '3', '12.5'])).toEqual(['-10.00', '-9.00', '-0.50', '3', '12.5'])
    expect(column(['-0.50', '-9.00', '-10.00', '3', '12.5'], 'desc')).toEqual(['12.5', '3', '-0.50', '-9.00', '-10.00'])
    expect(column([10, '9', 2.5, '-1'])).toEqual(['-1', 2.5, '9', 10])
    expect(column(['9007199254740993', '9007199254740992', '9007199254740994']))
      .toEqual(['9007199254740992', '9007199254740993', '9007199254740994'])
    // 空值永远排最后：没有值不是最小的值
    expect(column([null, '2', '', '1'], 'desc')).toEqual(['2', '1', null, ''])
    expect(column(['banana', 'Apple', 'cherry'])).toEqual(['Apple', 'banana', 'cherry'])
  })
})

describe('表页上的几段判断', () => {
  it('时间轴点出来的一对日期条件合成一个标签；别的条件各是一个', () => {
    const items = chipItems([
      { column: 'signed', op: 'gte', value: '2024-03-01' },
      { column: 'status', op: 'eq', value: 'open' },
      { column: 'signed', op: 'lt', value: '2024-04-01' },
      { column: 'note', op: 'is_empty', value: '' },
    ], (column) => column.toUpperCase(), (op) => `[${op}]`, (from, to) => `${from}..${to}`)
    expect(items).toEqual([
      { label: 'SIGNED: 2024-03', column: 'signed' },
      { label: 'STATUS [eq] open', index: 1 },
      { label: 'NOTE [is_empty]', index: 3 },
    ])
  })

  it('条件按字段类型给，再和服务端认的取交集；服务端没说就不限', () => {
    const number = { kind: 'number' } as Parameters<typeof operatorsFor>[0]
    expect(operatorsFor(number, null)).toEqual(['eq', 'ne', 'gte', 'lte', 'gt', 'lt', 'is_empty', 'not_empty'])
    expect(operatorsFor(number, ['eq', 'gt'])).toEqual(['eq', 'gt'])
    expect(operatorsFor(null, null)[0]).toBe('contains')
  })

  it('整张表都空着的字段才收起；只列了一两条的长尾分布不占卡片，正筛着的一定留着', () => {
    const profile = (rows: number, columns: Array<{ name: string; filled: number }>) =>
      ({ total_rows: rows, columns, facets: [], timelines: [] }) as unknown as Parameters<typeof emptyColumns>[0]
    expect(emptyColumns(profile(10, [{ name: 'a', filled: 0 }, { name: 'b', filled: 3 }]))).toEqual(['a'])
    expect(emptyColumns(profile(0, [{ name: 'a', filled: 0 }]))).toEqual([])
    const tail = { column: 'memo', complete: false, values: [{ value: 'x', count: 1 }] } as Parameters<typeof worthShowing>[0]
    expect(worthShowing(tail, 1000, [])).toBe(false)
    expect(worthShowing(tail, 1000, [{ column: 'memo', op: 'eq', value: 'x' }])).toBe(true)
  })
})
