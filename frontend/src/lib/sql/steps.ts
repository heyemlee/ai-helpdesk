/**
 * 一步结果的列、失败归类、校验发现的叫法（移植自旧控制台 steps.js 里问数用得到的部分）。
 *
 * 结果列只认服务端给的单位（column_units），不按列名猜；说明来自数据字典（lookupColumn），
 * 对不上的列只显示字段名，不把另一张表的说明安到它头上。
 */
import type { Catalog, CatalogColumn, CatalogTable } from '@/api/sql/types'
import type { Lang } from '@/i18n'
import type { Key } from '@/i18n/catalog'
import { lookupColumn } from './catalog'
import { descriptionOf, humanize, isNumericColumn, labelOf } from './format'
import { hasKey } from './labels'
import type { ResultStep } from './model'

type T = (key: Key, vars?: Record<string, string | number>) => string

/** 结果里的一列：名字、单位、是不是数字、数据字典里对应的条目 */
export interface StepColumn {
  key: string
  label: string
  unit: string
  numeric: boolean
  /** 连接键、主键：数字不加千分位，也不当成"量"画图 */
  plain: boolean
  hint: string
  entry: CatalogColumn | null
  table: CatalogTable | null
}

export function stepColumns(step: ResultStep, catalog: Catalog | null | undefined, lang: Lang): StepColumn[] {
  return step.columns.map((name) => {
    const match = lookupColumn(catalog, step.database, name, step.finalSql)
    const column = match?.column ?? null
    return {
      key: name,
      label: column ? labelOf(column, lang) : humanize(name),
      unit: step.units[name] ?? '',
      numeric: isNumericColumn(step.rows, (row) => row[name]),
      plain: Boolean(column && (column.role === 'join_key' || column.primary_key)),
      hint: column ? descriptionOf(column, lang).text : '',
      entry: column,
      table: match?.table ?? null,
    }
  })
}

/** 一步结果的行，按列的顺序排成二维数组（表格、复制用） */
export const stepMatrix = (step: ResultStep, columns: Array<{ key: string }>): unknown[][] =>
  step.rows.map((row) => columns.map((column) => row[column.key]))

export type FailureKey =
  | 'sq.ask.fail.permission' | 'sq.ask.fail.schema' | 'sq.ask.fail.timeout' | 'sq.ask.fail.blocked'
  | 'sq.ask.fail.generic' | 'sq.ask.fail.modelDown'

/**
 * 服务端的报错原文是中文句子。页面按其中稳定的片段归成几种人能处理的情况，原文留在查询过程里。
 * 认不出的归为一般失败，不猜。
 */
export function failureReason(error: string | null | undefined): { key: FailureKey; detail: string } {
  const text = String(error ?? '')
  if (text.includes('没有访问权限')) {
    const names = (text.split('没有访问权限：')[1] ?? '').split('。')[0]!.trim()
    return { key: 'sq.ask.fail.permission', detail: names }
  }
  if (text.includes('当前可见的表不足')) return { key: 'sq.ask.fail.schema', detail: '' }
  if (text.includes('超时')) return { key: 'sq.ask.fail.timeout', detail: '' }
  if (text.includes('授权上下文') || text.includes('无法解析')) return { key: 'sq.ask.fail.blocked', detail: '' }
  return { key: 'sq.ask.fail.generic', detail: '' }
}

/** 校验发现的名字：认识的翻成人话，不认识的原样显示服务端给的代码 */
export function anomalyLabel(code: string | undefined, t: T): string {
  const key = `sq.ask.anomaly.${code ?? ''}`
  return code && hasKey(key) ? t(key) : String(code ?? '')
}

export function anomalyHelp(code: string | undefined, t: T): string {
  const key = `sq.ask.anomaly.${code ?? ''}.help`
  return code && hasKey(key) ? t(key) : ''
}
