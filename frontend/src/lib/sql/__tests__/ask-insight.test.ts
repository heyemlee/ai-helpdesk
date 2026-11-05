/**
 * 结果画不画成图、默认用哪种看法（移植自 test_console_ask.py 的图那一组）。
 *
 * 只有形状没有歧义、数字那一列确实是一个量时才画——缺值、负数、重名、被截断、月份不连续、
 * 数字其实是编号的时候不画，而不是画一张看着像回事的图。
 */
import { describe, expect, it } from 'vitest'
import { chartFor, stepViews, type Chart, type ChartColumn } from '../insight'
import { normalizeResult } from '../model'
import { stepColumns } from '../steps'

const COUNTED = 'SELECT stage, COUNT(*) AS n FROM deal GROUP BY stage'

const stepOf = (rows: Array<Record<string, unknown>>, extra: Record<string, unknown> = {}) => normalizeResult({
  route: 'database_query', queried_database: true, has_risk: false, degraded: false, execution_status: 'success',
  steps: [{ database: 'sales_db', sql: COUNTED, final_sql: COUNTED, success: true, row_count: rows.length, rows, ...extra }],
}, { transport: 'query' }).steps[0]!

const describeChart = (chart: Chart | null) => (chart
  ? { kind: chart.kind, label: chart.label.key, value: chart.value.key, items: chart.items.map((item) => [item.text, item.value]) }
  : null)

const verdict = (rows: Array<Record<string, unknown>>, extra?: Record<string, unknown>) => {
  const step = stepOf(rows, extra)
  const columns = stepColumns(step, null, 'en')
  return { chart: describeChart(chartFor(step, columns)), views: stepViews(step, columns) }
}
const months = (labels: string[]) => labels.map((label, index) => ({ month: label, n: index + 1 }))
const many = (count: number) => Array.from({ length: count }, (_, index) => ({ name: `item ${index}`, n: index + 1 }))

describe('画不画成图', () => {
  it('一列名字加一列数出来的数：横条，按返回的顺序', () => {
    const bars = verdict([{ stage: 'won', n: 3 }, { stage: 'lost', n: 5 }, { stage: 'open', n: 0 }])
    expect(bars.chart).toEqual({ kind: 'bars', label: 'stage', value: 'n', items: [['won', 3], ['lost', 5], ['open', 0]] })
    expect(bars.views).toEqual(['chart', 'table'])
    // 哪一列在前不要紧
    expect(verdict([{ n: 3, stage: 'won' }, { n: 5, stage: 'lost' }]).chart?.label).toBe('stage')
    expect(verdict(many(12)).chart?.items).toHaveLength(12)
  })

  it('连着的年月或年份：一排柱子，和表格一起出，不是单独一种看法', () => {
    const series = verdict([{ month: '2025-03', n: 2 }, { month: '2025-01', n: 7 }, { month: '2025-02', n: 5 }])
    expect(series.chart?.kind).toBe('columns')
    expect(series.chart?.items).toEqual([['2025-01', 7], ['2025-02', 5], ['2025-03', 2]])
    expect(series.views).toEqual(['table'])
    expect(verdict(months(['2025-11', '2025-12', '2026-01'])).chart?.kind).toBe('columns')
    expect(verdict([{ y: '2023', n: 1 }, { y: '2024', n: 2 }, { y: '2025', n: 3 }]).chart?.kind).toBe('columns')
  })

  it('四位数的编号不是年份：照返回的顺序画横条', () => {
    const codes = verdict([{ store: '1003', n: 9 }, { store: '1001', n: 4 }, { store: '1002', n: 6 }]).chart
    expect(codes?.kind).toBe('bars')
    expect(codes?.items.map(([label]) => label)).toEqual(['1003', '1001', '1002'])
  })

  it('缺了一个月的序列不画成挨着的柱子：行少退回横条，行多就不画', () => {
    expect(verdict(months(['2025-01', '2025-02', '2025-04'])).chart?.kind).toBe('bars')
    expect(verdict(months(Array.from({ length: 14 }, (_, index) => `20${10 + index}-01`))).chart).toBeNull()
    expect(verdict(months(['2025-01', '2025-02'])).chart?.kind).toBe('bars')
  })

  it('有歧义或不完整的结果只给表格', () => {
    const cases: Record<string, ReturnType<typeof verdict>> = {
      thirteen: verdict(many(13)),
      negative: verdict([{ stage: 'a', n: 3 }, { stage: 'b', n: -1 }]),
      missingValue: verdict([{ stage: 'a', n: 3 }, { stage: 'b', n: null }]),
      missingLabel: verdict([{ stage: 'a', n: 3 }, { stage: null, n: 4 }]),
      duplicateLabel: verdict([{ stage: 'a', n: 3 }, { stage: 'a', n: 4 }]),
      allZero: verdict([{ stage: 'a', n: 0 }, { stage: 'b', n: 0 }]),
      truncated: verdict([{ stage: 'a', n: 3 }, { stage: 'b', n: 4 }], { truncated: true, row_limit: 2 }),
      threeColumns: verdict([{ stage: 'a', n: 3, m: 1 }, { stage: 'b', n: 4, m: 2 }]),
      twoNumbers: verdict([{ year: 2024, n: 3 }, { year: 2025, n: 4 }]),
      twoTexts: verdict([{ a: 'x', b: 'y' }, { a: 'z', b: 'w' }]),
      numberAsText: verdict([{ stage: 'a', n: '3.50' }, { stage: 'b', n: '4.00' }]),
      // 没有聚合的 SQL、数据字典里也查不到这一列：分不清是量还是编号
      plainSelect: verdict([{ name: 'a', supplier: 3 }, { name: 'b', supplier: 15 }],
        { sql: 'SELECT name, supplier FROM vendor', final_sql: 'SELECT name, supplier FROM vendor' }),
    }
    for (const [name, result] of Object.entries(cases)) {
      expect(result.chart, name).toBeNull()
      expect(result.views, name).toEqual(['table'])
    }
  })

  it('数字只在它是一个"量"时才画：数据字典里登记的角色说了算', () => {
    const listed = stepOf([{ name: 'a', amount: 3 }, { name: 'b', amount: 15 }],
      { sql: 'SELECT name, amount FROM deal', final_sql: 'SELECT name, amount FROM deal' })
    const withRole = (role: string, extra: Partial<ChartColumn> = {}) => describeChart(chartFor(listed, [
      { key: 'name', numeric: false },
      { key: 'amount', numeric: true, entry: { role }, ...extra },
    ]))
    expect(withRole('metric')?.kind).toBe('bars')
    expect(withRole('metric_filter')?.kind).toBe('bars')
    // 编号、分类、日期也可能是数字，但不是量；主键即使标成指标也不画
    for (const role of ['join_key', 'filter', 'time', '']) expect(withRole(role), role).toBeNull()
    expect(withRole('metric', { plain: true })).toBeNull()
  })

  it('只有一行时是几个大数字；列太多时还是表格', () => {
    const one = verdict([{ deals: 12, amount: 3400.5 }])
    expect(one.views).toEqual(['figures', 'table'])
    expect(one.chart).toBeNull()
    const wide = verdict([Object.fromEntries(Array.from({ length: 9 }, (_, index) => [`c${index}`, index]))])
    expect(wide.views).toEqual(['table'])
  })
})
