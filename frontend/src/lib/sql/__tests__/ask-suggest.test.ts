/**
 * 建议问题（移植自 ai-sql-agent/tests/test_console_ask.py 的建议那一组）。
 *
 * 它们是拿数据目录拼的，不是写死的——所以要查：目录里没有的表和字段进不了建议，读不到的库不出现，
 * 换一套目录就换一套问题，而且不拼"合计"这种对比率类指标不成立的问法。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import type { CatalogDomain } from '@/api/sql/types'
import type { Lang } from '@/i18n'
import { CATALOG, type Key } from '@/i18n/catalog'
import { SQ_ASK_EN, SQ_ASK_ZH } from '@/i18n/sq-ask'
import { SUGGESTION_KINDS, buildSuggestions, declaredValues, pageCount, pageOf, type Suggestion } from '../suggest'

/** 和 LangProvider 里的 t 同一套规则：缺键回英文、`单数|复数` 按 n 选、{变量} 替换 */
export const tr = (lang: Lang) => (key: Key, vars?: Record<string, string | number>): string => {
  const text = (CATALOG[lang] as Record<string, string>)[key] ?? (CATALOG.en as Record<string, string>)[key] ?? key
  const forms = text.split('|')
  const raw = forms.length === 2 && typeof vars?.n === 'number' ? forms[vars.n === 1 ? 0 : 1]! : text
  return vars ? raw.replace(/\{(\w+)\}/g, (m, k: string) => (k in vars ? String(vars[k]) : m)) : raw
}

const column = (name: string, role: string, kind: string, extra: Record<string, unknown> = {}) => ({
  name, status: 'available', role, kind, unit: '', value_range: '', aliases: [], ...extra,
})
const table = (name: string, en: string, zh: string, columns: unknown[], status = 'available') => ({
  name, status, label_en: en, label_zh: zh, aliases: [], columns,
})
const domain = (database: string, status: string, tables: unknown[]) =>
  ({ database, status, tables }) as unknown as CatalogDomain

const sales = domain('sales_db', 'available', [
  table('deal', 'Deals', '成交单', [
    column('deal_code', 'join_key', 'text', { label_en: 'Deal code', label_zh: '成交单号' }),
    column('stage', 'filter', 'text', { label_en: 'Deal stage', label_zh: '成交阶段', value_range: '洽谈 / 签约 / 回款' }),
    column('region', 'dimension', 'text', { label_en: 'Region', label_zh: '区域', value_range: '华东 / 华南' }),
    column('units', 'metric', 'number', { label_en: 'Units', label_zh: '件数' }),
    column('amount', 'metric', 'number', { label_en: 'Deal amount', label_zh: '成交金额', unit: 'USD' }),
    column('closed_on', 'time', 'date', { label_en: 'Closed date', label_zh: '成交日期' }),
    // 数据字典里有、库里没有的字段
    column('ghost', 'filter', 'text', { status: 'missing', label_en: 'Ghost field', label_zh: '幽灵字段', value_range: 'a / b' }),
  ]),
  // 只有自由文字：拼不出任何一种问法
  table('memo', 'Memos', '备忘', [column('body', 'text', 'text', { label_en: 'Body', label_zh: '正文' })]),
  // 数据字典里有、库里没有的表
  table('gone', 'Gone table', '不在的表', [column('kind', 'filter', 'text', { label_en: 'Kind', label_zh: '种类', value_range: 'x / y' })], 'missing'),
])
const stock = domain('stock_db', 'available', [
  table('bin', 'PO bins', '库位', [
    column('zone', 'filter', 'text', { label_en: 'Zone', label_zh: '分区', value_range: 'A / B / C' }),
    column('qty', 'metric', 'number', { label_en: 'Quantity', label_zh: '数量' }),
  ]),
])
// 此刻读不到的库
const down = domain('down_db', 'unavailable', [
  table('thing', 'Things', '东西', [column('state', 'filter', 'text', { label_en: 'State', label_zh: '状态', value_range: 'p / q' })]),
])
// 指标是一个比率：不能相加
const vendors = domain('vendor_db', 'available', [
  table('vendor', 'Vendors', '供应商', [
    column('tier', 'dimension', 'text', { label_en: 'Tier', label_zh: '等级', value_range: 'gold / silver' }),
    column('punctuality', 'metric', 'number', { label_en: 'On-time rate', label_zh: '准时率', unit: 'ratio', value_range: '0-1' }),
  ]),
])
const domains = [sales, stock, down]

const brief = (list: Suggestion[]) => list.map((item) => [item.database, item.table, item.kind, item.text])
const texts = (list: Suggestion[]) =>
  Object.fromEntries(list.map((item) => [`${item.database}/${item.table}/${item.kind}`, item.text]))

const english = buildSuggestions(domains, 'en', tr('en'))
const chinese = buildSuggestions(domains, 'zh', tr('zh'))

describe('建议问题', () => {
  it('只拿目录里的表和字段拼，不写死任何一句', () => {
    const en = texts(english)
    expect(en['sales_db/deal/countBy']).toBe('Count deals by deal stage')
    // 两个数值字段里挑带单位的那个（金额），不是排在前面的那个
    expect(en['sales_db/deal/topBy']).toBe('Top 10 deals by deal amount')
    expect(en['sales_db/deal/perMonth']).toBe('Count deals per month, by closed date')
    expect(en['sales_db/deal/averageBy']).toBe('Average deal amount of deals by deal stage')
    // "筛出一个取值"换一个分类字段，取它声明的第一个取值（取值是数据，原样写）
    expect(en['sales_db/deal/whereIs']).toBe('List deals where region is “华东”')
    // 只有一个分类字段时就用它；全大写的缩写不改成小写
    expect(en['stock_db/bin/whereIs']).toBe('List PO bins where zone is “A”')
    expect(en['stock_db/bin/topBy']).toBe('Top 10 PO bins by quantity')
    // 没有日期字段就没有"按月"
    expect(en['stock_db/bin/perMonth']).toBeUndefined()
    expect(english).toHaveLength(9)
  })

  it('读不到的库、库里没有的表和字段、只有自由文字的表，都拼不出建议', () => {
    const everything = JSON.stringify([...brief(english), ...brief(chinese)])
    for (const absent of ['down_db', 'Things', 'gone', 'Gone table', 'Ghost field', '幽灵字段', 'memo', 'Memos']) {
      expect(everything).not.toContain(absent)
    }
    expect(buildSuggestions([down, domain('bare_db', 'available', [sales.tables[1]])], 'en', tr('en'))).toEqual([])
    expect(buildSuggestions(null, 'en', tr('en'))).toEqual([])
  })

  it('不拼"合计"：数据字典只说是指标，没说能不能相加', () => {
    const ratioEn = buildSuggestions([vendors], 'en', tr('en'))
    const ratioZh = buildSuggestions([vendors], 'zh', tr('zh'))
    expect(ratioEn.map((item) => item.text).sort()).toEqual([
      'Average on-time rate of vendors by tier', 'Count vendors by tier',
      'List vendors where tier is “gold”', 'Top 10 vendors by on-time rate',
    ])
    for (const item of [...english, ...chinese, ...ratioEn, ...ratioZh]) {
      for (const word of ['Total', 'Sum', '合计', '汇总', '总和']) expect(item.text).not.toContain(word)
    }
    expect(Object.keys(SQ_ASK_EN)).not.toContain('sq.ask.suggest.sumBy')
  })

  it('顺序稳定；第一页每个库一条、问法不重样', () => {
    expect(brief(buildSuggestions(domains, 'en', tr('en')))).toEqual(brief(english))
    const ids = english.map((item) => item.id)
    expect(new Set(ids).size).toBe(ids.length)
    const first = english.slice(0, 2)
    expect(first.map((item) => item.database)).toEqual(['sales_db', 'stock_db'])
    expect(new Set(first.map((item) => item.kind)).size).toBe(2)
    expect([...new Set(english.map((item) => item.kind))].sort()).toEqual([...SUGGESTION_KINDS].sort())
  })

  it('翻页翻过头绕回第一页，负数从最后一页数', () => {
    expect(pageCount(english)).toBe(3)
    expect(pageOf(english, 0)).toEqual(english.slice(0, 4))
    expect(pageOf(english, pageCount(english))).toEqual(pageOf(english, 0))
    expect(pageOf(english, -1)).toEqual(pageOf(english, pageCount(english) - 1))
    expect(pageOf(english, 2)).toEqual(english.slice(8))
    expect([pageCount([]), pageOf([], 3).length]).toEqual([1, 0])
  })

  it('跟着界面语言走', () => {
    const zh = texts(chinese)
    expect(zh['sales_db/deal/countBy']).toBe('按成交阶段统计成交单数量')
    expect(zh['sales_db/deal/topBy']).toBe('成交金额最高的 10 条成交单')
    expect(zh['sales_db/deal/perMonth']).toBe('按月统计成交单数量（按成交日期）')
    expect(zh['sales_db/deal/averageBy']).toBe('按成交阶段看成交单的平均成交金额')
    expect(zh['sales_db/deal/whereIs']).toBe('列出区域为「华东」的成交单')
    expect(chinese).toHaveLength(english.length)
  })

  it('声明的取值和服务端读法一样（ai_sql_agent.catalog.profile.declared_values 对同一批写法的结果）', () => {
    const long = 'x'.repeat(120)
    const server: Array<[string, string[]]> = [
      ['洽谈 / 签约 / 回款', ['洽谈', '签约', '回款']],
      ['A/B', ['A', 'B']],
      ['yes / no / yes', ['yes', 'no']],
      ['0-1', []],
      ['正数，单位美元；不含税', []],
      ['> 0', []],
      ['a / / b', []],
      ['only', []],
      ['', []],
      ['trailing /', []],
      [`${long} / y`, [long, 'y']],
      [`${long}x / y`, []],
    ]
    for (const [value_range, expected] of server) {
      expect(declaredValues({ value_range, kind: 'text' }), value_range).toEqual(expected)
    }
    // 比服务端多的一条：数字字段上的清单不拿来拼问题
    expect(declaredValues({ value_range: '0 / 1', kind: 'number' })).toEqual([])
    expect(declaredValues(null)).toEqual([])
  })

  it('每一种问法都有自己的文案，而且都留着表名的位置；进度和看法也都有文案', () => {
    for (const kind of SUGGESTION_KINDS) {
      for (const dictionary of [SQ_ASK_EN, SQ_ASK_ZH] as Array<Record<string, string>>) {
        // 没有 {table} 那就是一句写死的问题了
        expect(dictionary[`sq.ask.suggest.${kind}`], kind).toContain('{table}')
      }
    }
    for (const step of ['understand', 'retrieve', 'plan', 'generate', 'execute', 'check']) {
      expect(Object.keys(SQ_ASK_EN)).toContain(`sq.ask.step.${step}`)
    }
    for (const view of ['figures', 'chart', 'table']) expect(Object.keys(SQ_ASK_EN)).toContain(`sq.ask.view.${view}`)
  })

  it('页面上没有写死的示例问题：建议全部来自 buildSuggestions', () => {
    for (const dictionary of [SQ_ASK_EN, SQ_ASK_ZH]) {
      expect(Object.keys(dictionary).filter((key) => key.includes('example'))).toEqual([])
    }
    const home = readFileSync(resolve(process.cwd(), 'src/components/data/ask/AskHome.tsx'), 'utf8')
    expect(home).toContain('buildSuggestions')
    expect(home).not.toMatch(/EXAMPLES/)
  })
})
