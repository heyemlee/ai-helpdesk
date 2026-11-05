import { describe, expect, it } from 'vitest'
import {
  descriptionOf, formatCell, formatCount, formatNumber, formatRelative, formatShare, humanize, labelOf, recordDates,
} from '../format'

/** 移植自 ai-sql-agent/tests/test_console_logic.py、test_console_ask.py 里管"怎么显示"的那几条 */
describe('数据页面的显示规则', () => {
  it('一个不是零的数永远不显示成零', () => {
    expect(formatNumber(0.00001234, 'en')).not.toMatch(/^-?0(\.0+)?$/)
    expect(formatNumber(0.004, 'en', { unit: 'USD' })).not.toBe('0.00')
    expect(formatNumber(-0.00001, 'en')).not.toMatch(/^-?0$/)
    expect(formatNumber(0, 'en')).toBe('0')
  })

  it('编号、年份这类小整数不加千分位；一万起加；币种固定两位小数', () => {
    expect(formatNumber(2024, 'en')).toBe('2024')
    expect(formatNumber(12345, 'en')).toBe('12,345')
    expect(formatNumber(12345, 'en', { plain: true })).toBe('12345')
    expect(formatNumber(1412500, 'en', { unit: 'CNY' })).toBe('1,412,500.00')
  })

  it('数量和占比写成好读的样子：不是零的一份不写成 0%', () => {
    expect(formatCount(1200, 'en')).toBe('1,200')
    expect(formatCount(Number.NaN, 'en')).toBe('—')
    expect(formatShare(1, 3, 'en')).toBe('33%')
    expect(formatShare(1, 5000, 'en')).toBe('<0.1%')
    expect(formatShare(3, 100, 'en')).toBe('3%')
    expect(formatShare(0, 0, 'en')).toBe('')
  })

  it('数据字典的说明用读者的语言；没有就给原文并标明，页面不替它翻译', () => {
    const column = { name: 'x', description: '含税订单金额', description_en: '' }
    expect(descriptionOf(column, 'en')).toEqual({ text: '含税订单金额', original: true })
    expect(descriptionOf(column, 'zh')).toEqual({ text: '含税订单金额', original: false })
    expect(descriptionOf({ ...column, description_en: 'Order amount incl. tax' }, 'en'))
      .toEqual({ text: 'Order amount incl. tax', original: false })
  })

  it('名字：显式的 label，其次当前语言的别名，最后才拆技术名', () => {
    expect(labelOf({ name: 'order_amt', aliases: ['订单金额', 'Order amount'] }, 'zh')).toBe('订单金额')
    expect(labelOf({ name: 'order_amt', aliases: ['订单金额', 'Order amount'] }, 'en')).toBe('Order amount')
    expect(labelOf({ name: 'inv_amt', aliases: ['发票金额'] }, 'en')).toBe('Inv amount')
    expect(labelOf({ name: 'x', label_en: 'Explicit' }, 'en')).toBe('Explicit')
    expect(humanize('refNo')).toBe('Ref number')
  })

  it('业务日期只从真正的日期里读：单据号、月份不对的不算', () => {
    const rows = [
      { a: '2024-01-20', b: '2024-10-12345', c: '2024-13-01', d: '2024-03-05T10:00:00' },
      { a: '2024-04-20', b: '2024-10-12346', c: '2024-12-01', d: null },
    ]
    const ranges = recordDates(['a', 'b', 'c', 'd'], rows, (row, column) => (row as Record<string, unknown>)[column])
    expect(ranges).toEqual([
      { column: 'a', min: '2024-01-20', max: '2024-04-20' },
      { column: 'd', min: '2024-03-05', max: '2024-03-05' },
    ])
  })

  it('最近的时间说相对的，一周以前的直接写日期', () => {
    const now = Date.parse('2026-11-05T12:00:00Z')
    expect(formatRelative('2026-11-05T11:59:40Z', 'en', now)).toBe('now')
    expect(formatRelative('2026-11-05T11:55:00Z', 'en', now)).toBe('5 minutes ago')
    expect(formatRelative('2026-11-04T12:00:00Z', 'en', now)).toBe('yesterday')
    expect(formatRelative('2026-10-01T12:00:00Z', 'en', now)).toMatch(/2026/)
    expect(formatRelative('2026-11-05T11:55:00Z', 'zh', now)).toBe('5分钟前')
  })

  it('单元格：空值是破折号，对象按 JSON 显示，不是 [object Object]', () => {
    expect(formatCell(null, 'en')).toEqual({ text: '—', numeric: false, empty: true })
    expect(formatCell({ a: 1 }, 'en').text).toBe('{"a":1}')
    expect(formatCell('12.5', 'en', { kind: 'number' }).numeric).toBe(true)
    expect(formatCell('12.5', 'en').numeric).toBe(false)
  })
})
