/**
 * 数据目录（/catalog）上的查找：有哪些业务域、表、字段，各自的业务名、单位、说明
 * （移植自旧控制台的 catalog.js；请求和缓存交给 TanStack Query，见 api/sql/hooks.ts）。
 *
 * 三页都用它：数据浏览直接展示它，问数用它给结果列配上业务名与说明，历史用它把库名写成业务名。
 * 页面里没有任何一份写死的表清单：这里拿不到，页面就说"拿不到"。
 */
import type { Lang } from '@/i18n'
import type { Catalog, CatalogColumn, CatalogDomain, CatalogTable, DomainTitle } from '@/api/sql/types'
import { labelOf } from './format'

// 常见业务域的阅读顺序：先订单采购，再项目，再生产，再库存；认不出类型的排在后面
const KIND_ORDER = ['erp', 'pm', 'mes', 'wms']
const rank = (domain: CatalogDomain) => {
  const index = KIND_ORDER.indexOf(domain.kind)
  return index === -1 ? KIND_ORDER.length : index
}

/** 目录里的业务域，按阅读顺序排好（不改原数组） */
export const sortedDomains = (catalog: Catalog | null | undefined): CatalogDomain[] =>
  [...(catalog?.domains ?? [])].sort((a, b) => rank(a) - rank(b))

export const findDomain = (catalog: Catalog | null | undefined, database: string): CatalogDomain | null =>
  catalog?.domains.find((domain) => domain.database === database) ?? null

export function findTable(catalog: Catalog | null | undefined, database: string, table: string): CatalogTable | null {
  return findDomain(catalog, database)?.tables.find((item) => item.name === table) ?? null
}

/** 服务端按语言给的名字取当前语言那一份；只有一个字符串时原样用 */
export function pick(title: DomainTitle | null | undefined, lang: Lang): string {
  if (!title) return ''
  if (typeof title === 'string') return title
  return title[lang] || title.en || title.zh || ''
}

/** 业务域的名字：语义层写的 / 服务端按系统类型给的，都没有就退回别名或标识符。 */
export function domainTitle(domain: CatalogDomain | null | undefined, lang: Lang): string {
  if (!domain) return ''
  return pick(domain.title, lang) || labelOf({ name: domain.database, aliases: domain.aliases }, lang)
}

export const domainTitleOf = (catalog: Catalog | null | undefined, database: string, lang: Lang): string => {
  const domain = findDomain(catalog, database)
  return domain ? domainTitle(domain, lang) : database
}

/** "业务域的名字 (库标识符)"：业务名在前，技术库名跟在后面；目录里没有这个库就只写标识符。 */
export function sourceLabel(catalog: Catalog | null | undefined, database: string, lang: Lang): string {
  const title = domainTitleOf(catalog, database, lang)
  return title === database ? database : `${title} (${database})`
}

const escapeRegExp = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/**
 * 给结果列找数据字典里的条目，用来显示业务名与说明。
 *
 * 结果列只有一个名字，不知道出自哪张表。同一个库里同名字段可能不止一个：优先取 SQL 里提到的那张表上的；
 * 仍然不唯一、而且各处含义不同的，就不配——宁可只显示字段名，也不把另一张表的说明安到它头上。
 * 这是给人看的注解，不参与任何权限或数值判断。
 */
export function lookupColumn(catalog: Catalog | null | undefined, database: string, name: string, sql = ''):
  { table: CatalogTable; column: CatalogColumn } | null {
  const domain = findDomain(catalog, database)
  if (!domain) return null
  const matches: Array<{ table: CatalogTable; column: CatalogColumn }> = []
  for (const table of domain.tables) {
    for (const column of table.columns ?? []) if (column.name === name) matches.push({ table, column })
  }
  if (!matches.length) return null
  if (matches.length === 1) return matches[0]!

  const mentioned = matches.filter(({ table }) => new RegExp(`\\b${escapeRegExp(table.name)}\\b`, 'i').test(sql))
  if (mentioned.length === 1) return mentioned[0]!

  const pool = mentioned.length ? mentioned : matches
  const first = pool[0]!.column
  const same = pool.every(({ column }) => column.description === first.description && column.unit === first.unit)
  return same ? pool[0]! : null
}
