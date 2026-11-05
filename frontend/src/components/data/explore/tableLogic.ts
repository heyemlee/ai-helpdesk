/**
 * 表页上的几段纯逻辑（移植自旧控制台 exploreTable.js）：哪些条件能用、筛选标签怎么写、哪些字段收起、
 * 表格的列怎么配、一条记录能去哪些表里找对得上的记录。
 */
import type { CatalogColumn, CatalogTable, PreviewOperator, RowsPage, TableProfile } from '@/api/sql/types'
import type { Lang } from '@/i18n'
import { periodName, seriesSlots } from '@/lib/sql/charts'
import type { ViewFilter } from '@/lib/sql/dataView'
import { descriptionOf, labelOf } from '@/lib/sql/format'
import { relatedTables, type LinkNode, type Links } from '@/lib/sql/links'
import { facetValues } from '@/lib/sql/profile'
import type { TableColumn } from '../DataTable'

const OPERATORS_BY_KIND: Record<string, PreviewOperator[]> = {
  text: ['contains', 'eq', 'ne', 'starts_with', 'is_empty', 'not_empty'],
  number: ['eq', 'ne', 'gte', 'lte', 'gt', 'lt', 'is_empty', 'not_empty'],
  date: ['eq', 'gte', 'lte', 'gt', 'lt', 'is_empty', 'not_empty'],
  binary: ['is_empty', 'not_empty'],     // 二进制字段只能问"有没有内容"
}

/** 这个字段能用哪些条件：按字段类型，再和服务端认的取交集（服务端没说就不限） */
export function operatorsFor(column: CatalogColumn | null | undefined, allowed: string[] | null | undefined): PreviewOperator[] {
  const own = OPERATORS_BY_KIND[column && OPERATORS_BY_KIND[column.kind] ? column.kind : 'text']!
  return own.filter((op) => !allowed || allowed.includes(op))
}

export const sameValue = (left: unknown, right: unknown) => String(left).toLowerCase() === String(right).toLowerCase()

/** 时间轴点出来的是一对条件（不早于 start、早于 end）。认出这样的一对，当成一个范围来显示 */
export function dateRange(filters: ViewFilter[], column: string): { start: string; end: string } | null {
  const own = filters.filter((filter) => filter.column === column)
  const from = own.find((filter) => filter.op === 'gte')
  const until = own.find((filter) => filter.op === 'lt')
  return own.length === 2 && from && until ? { start: String(from.value), end: String(until.value) } : null
}

export interface ChipItem {
  label: string
  /** 一对日期条件合成的那个：按字段整体去掉 */
  column?: string
  /** 单个条件：按位置去掉 */
  index?: number
}

/** 要显示的筛选标签。一对日期条件合成一个 */
export function chipItems(filters: ViewFilter[], nameOf: (column: string) => string,
  opLabel: (op: PreviewOperator) => string, rangeLabel: (from: string, to: string) => string): ChipItem[] {
  const items: ChipItem[] = []
  const ranged = new Set<string>()
  filters.forEach((filter, index) => {
    if (ranged.has(filter.column)) return
    const range = dateRange(filters, filter.column)
    if (range) {
      ranged.add(filter.column)
      items.push({ label: `${nameOf(filter.column)}: ${periodName(range) ?? rangeLabel(range.start, range.end)}`, column: filter.column })
    } else {
      const valueless = filter.op === 'is_empty' || filter.op === 'not_empty'
      items.push({ label: `${nameOf(filter.column)} ${opLabel(filter.op)}${valueless ? '' : ` ${filter.value}`}`, index })
    }
  })
  return items
}

/** 整张表里一条内容都没有的字段 */
export function emptyColumns(base: TableProfile | null | undefined): string[] {
  if (!base || !base.total_rows) return []
  return base.columns.filter((column) => column.filled === 0).map((column) => column.name)
}

/** 这个字段的取值各是第几个颜色。按整张表的取值清单定，筛选之后不变 */
export function slotsFor(name: string, base: TableProfile | null | undefined): Map<string, number> {
  return seriesSlots(base?.facets.find((item) => item.column === name))
}

/** 表格里的列：收起的不画；带色点的取值不按数字对齐 */
export function gridColumns(table: CatalogTable, data: RowsPage, concealed: Set<string>,
  base: TableProfile | null | undefined, lang: Lang): TableColumn[] {
  const entries = new Map((table.columns ?? []).map((column) => [column.name, column]))
  return data.columns
    .map((column, index) => ({ column, index }))
    .filter(({ column }) => !concealed.has(column.name))
    .map(({ column, index }) => {
      const entry = entries.get(column.name)
      const slots = slotsFor(column.name, base)
      return {
        key: column.name,
        index,
        label: entry ? labelOf(entry, lang) : column.name,
        unit: entry?.unit ?? '',
        numeric: column.kind === 'number' && !slots.size,
        sortable: column.sortable !== false,
        plain: !!entry && (entry.role === 'join_key' || entry.primary_key),
        hint: entry ? descriptionOf(entry, lang).text : '',
        ...(slots.size ? { slots } : {}),
        clip: column.kind !== 'number' && column.kind !== 'date',
      }
    })
}

/** 一个字段的分布值不值得占一张卡片。正筛着的字段一定留着——得让人能取消 */
export function worthShowing(facet: TableProfile['facets'][number], rows: number, filters: ViewFilter[]): boolean {
  if (filters.some((filter) => filter.column === facet.column)) return true
  if (!facet.values.some((item) => item.count > 0)) return false
  if (facet.complete) return true
  return (facet.values[0]?.count ?? 0) >= Math.max(2, rows * 0.005)
}

/** 这个字段在整张表里有哪些取值（做成输入提示，少打字也少打错）：截过的、数量是 0 的不算 */
export function knownValues(base: TableProfile | null | undefined, column: string): string[] {
  const facet = base?.facets.find((item) => item.column === column)
  return facet ? facetValues(facet.values).filter((item) => !item.truncated && item.count > 0).map((item) => String(item.value)) : []
}

export interface RelatedTarget {
  /** 这张表上用来对的字段 */
  column: string
  value: unknown
  other: LinkNode
}

/** 这条记录能去哪些表里找对得上的记录 */
export function relatedTargets(links: Links, database: string, table: string, row: unknown[], names: string[]): RelatedTarget[] {
  const targets: RelatedTarget[] = []
  for (const item of relatedTables(links, database, table)) {
    const position = names.indexOf(item.column)
    const value = position === -1 ? null : row[position]
    if (value === null || value === undefined || value === '') continue
    for (const other of item.others) targets.push({ column: item.column, value, other })
  }
  return targets
}

// 相关表里先看哪类字段：状态与类别最先，其次是名称编号这类标识，再是日期、数字，备注最后
const BRIEF_RANK: Record<string, number> = {
  filter: 0, dimension: 0, output_dimension: 0, join_key: 1, identifier: 1, time: 2, metric: 3, metric_filter: 3,
}
const RELATED_COLUMNS = 5

/** 相关表里挑几个字段出来看（抽屉里放不下整张表）。用来对上的那个字段不用再列——每一行都是同一个值 */
export function briefColumns(tableEntry: CatalogTable, data: RowsPage, skip: string, lang: Lang): TableColumn[] {
  const entries = new Map((tableEntry.columns ?? []).map((column) => [column.name, column]))
  const rank = (entry: CatalogColumn | undefined) => (entry && entry.role in BRIEF_RANK ? BRIEF_RANK[entry.role]! : 4)
  return data.columns
    .map((column, index) => ({ column, index, entry: entries.get(column.name) }))
    .filter(({ column }) => column.name !== skip && column.kind !== 'binary')
    .sort((a, b) => rank(a.entry) - rank(b.entry) || a.index - b.index)
    .slice(0, RELATED_COLUMNS)
    .sort((a, b) => a.index - b.index)
    .map(({ column, index, entry }) => ({
      key: column.name,
      index,
      label: entry ? labelOf(entry, lang) : column.name,
      unit: entry?.unit ?? '',
      numeric: column.kind === 'number',
      plain: !!entry && (entry.role === 'join_key' || entry.primary_key),
      clip: column.kind !== 'number' && column.kind !== 'date',
    }))
}
