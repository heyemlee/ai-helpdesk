/**
 * 数据浏览各处共用的小件（移植自旧控制台 exploreParts.js）：数据字典里的说明文字、记录数、
 * 目录没拿到时的几种状态、提示块、统计格子。
 *
 * 和整个数据页一样，这里没有任何表名、字段名或样例数据：服务端没给的，这里画不出来。
 */
import type { ReactNode } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'
import { SqlApiError } from '@/api/sql/client'
import type { Catalog, CatalogDomain, CatalogTable, TableProfile } from '@/api/sql/types'
import { useT, type Lang } from '@/i18n'
import type { Key } from '@/i18n/catalog'
import { descriptionOf, formatCount, labelOf } from '@/lib/sql/format'
import { hasKey } from '@/lib/sql/labels'
import { tableKey } from '@/lib/sql/links'
import { Icon } from '../../Icon'
import { StateBlock } from '../../ui'
import { SqlErrorBlock } from '../shared'

// 一口气给多少张表各数一遍行数。超过这个数就不自动数了（每张表是一个请求、一遍全表扫描），
// 行数到那张表自己的页面上再看
export const COUNT_UP_TO = 60
// 一个库里的表不超过这么多时，库页面上每张表都画分布和时间轴；再多就只给行数
export const RICH_CARDS_UP_TO = 12

/** 表很多的部署里 /catalog 不带字段，那时用服务端给的字段数 */
export const fieldCount = (table: CatalogTable): number =>
  (table.columns ?? []).length || (table as CatalogTable & { column_count?: number }).column_count || 0

export const countable = (domain: CatalogDomain, table: CatalogTable): boolean =>
  domain.status === 'available' && table.status === 'available' && table.previewable

/** 每张表的概况在这一页上的状态：已经要过的才在里面 */
export type ProfileIndex = Map<string, { status: 'pending' | 'success' | 'error'; profile: TableProfile | undefined }>

export function indexProfiles(entries: Array<{ database: string; table: string; result: UseQueryResult<TableProfile> }>,
  into: ProfileIndex = new Map()): ProfileIndex {
  for (const { database, table, result } of entries) {
    const key = tableKey(database, table)
    const known = into.get(key)
    // 已经有一份取到了的（比如带分布的那份）就不让还在数的那份盖掉
    if (known?.status === 'success' && result.status !== 'success') continue
    into.set(key, { status: result.status, profile: result.data })
  }
  return into
}

export const rowCount = (index: ProfileIndex, database: string, table: string): number | null => {
  const total = index.get(tableKey(database, table))?.profile?.total_rows
  return typeof total === 'number' ? total : null
}

/**
 * 几张表的记录数加起来。known 是数出来的，failed 是没数出来的，unasked 是根本没去数的
 * （表太多时不自动数）。
 */
export function sumCounts(index: ProfileIndex, pairs: Array<{ domain: CatalogDomain; table: CatalogTable }>) {
  const sum = { total: 0, known: 0, failed: 0, unasked: 0, expected: 0 }
  for (const { domain, table } of pairs) {
    if (!countable(domain, table)) continue
    sum.expected += 1
    const entry = index.get(tableKey(domain.database, table.name))
    const count = rowCount(index, domain.database, table.name)
    if (count !== null) { sum.total += count; sum.known += 1; continue }
    if (!entry) sum.unasked += 1
    else if (entry.status === 'error') sum.failed += 1
  }
  return sum
}

/** "1,200 条记录"：数字带千分位，所以单复数分成两个键（`单数|复数` 的写法只认未格式化的 n） */
export const recordCountText = (n: number, t: (key: Key, vars?: Record<string, string | number>) => string, lang: Lang) =>
  (n === 1 ? t('sq.explore.recordCount.one') : t('sq.explore.recordCount', { count: formatCount(n, lang) }))

/** 一张表的记录数。还在数时是一个占位；没去数、没数出来时什么都不写 */
export function CountChip({ index, database, table }: { index: ProfileIndex; database: string; table: string }) {
  const { lang } = useT()
  const count = rowCount(index, database, table)
  if (count !== null) return <span className="sq-dx-count">{formatCount(count, lang)}</span>
  return index.get(tableKey(database, table))?.status === 'pending'
    ? <span className="sq-dx-count is-pending" aria-hidden="true">···</span>
    : null
}

/** 说明文字。数据字典没有当前语言的版本时照原文给，并标明是原文 */
export function Description({ entity }: { entity: object }) {
  const { t, lang } = useT()
  const { text, original } = descriptionOf(entity, lang)
  if (!text) return <span className="sq-muted">{t('sq.explore.noDescription')}</span>
  return original
    ? <span className="sq-original" lang="zh" title={t('sq.explore.originalNote')}>{text}</span>
    : <span>{text}</span>
}

/**
 * 英文界面而数据字典只有中文说明时，先给一句英文概要，原文跟在后面。概要只是把目录里已有的名字
 * 列出来（这张表有哪些字段、这个域有哪些表），不是对原文的翻译——口径这种东西译错一个词，
 * 比让人看到一段中文危险。
 */
function Summarized({ entity, keyName, names }: { entity: object; keyName: Key; names: string[] }) {
  const { t, lang } = useT()
  const { original } = descriptionOf(entity, lang)
  if (!original || !names.length) return <Description entity={entity} />
  return <><span>{t(keyName, { names: names.join(t('sq.explore.listSep')) })} </span><Description entity={entity} /></>
}

const columnNames = (table: CatalogTable, lang: Lang) => (table.columns ?? [])
  .filter((column) => column.status === 'available')
  .map((column) => (column.unit ? `${labelOf(column, lang)} (${column.unit})` : labelOf(column, lang)))

export function TableSummary({ table }: { table: CatalogTable }) {
  const { lang } = useT()
  return <Summarized entity={table} keyName="sq.explore.holds" names={columnNames(table, lang)} />
}

export function DomainSummary({ domain }: { domain: CatalogDomain }) {
  const { lang } = useT()
  return <Summarized entity={domain} keyName="sq.explore.contains" names={domain.tables.map((table) => labelOf(table, lang))} />
}

export function unavailableReason(domain: CatalogDomain, t: (key: Key) => string): string {
  const key = `sq.explore.reason.${domain.status_reason}`
  return hasKey(key) ? t(key) : t('sq.explore.reason.unknown')
}

/** 提示块：标题说发生了什么，正文说接下来能做什么 */
export function Notice({ tone = 'neutral', title, children }: {
  tone?: 'neutral' | 'caution' | 'danger'; title: ReactNode; children?: ReactNode
}) {
  return (
    <div className={`note ${tone} sq-notice`} role="note">
      <p className="sq-notice-title">{title}</p>
      {children && <div className="sq-notice-body">{children}</div>}
    </div>
  )
}

export function UnavailableNotice({ domain }: { domain: CatalogDomain }) {
  const { t } = useT()
  return (
    <Notice tone="danger" title={t('sq.explore.unavailable.title')}>
      <p>{unavailableReason(domain, t)}</p>
      <p>{t('sq.explore.unavailable.body')}</p>
    </Notice>
  )
}

export function Kpi({ label, value, sub, title }: { label: string; value: string; sub?: string; title?: string }) {
  return (
    <div className="sq-kpi">
      <p className="sq-kpi-label">{label}</p>
      <p className="sq-kpi-value" title={title}>{value}</p>
      {sub && <p className="sq-kpi-sub">{sub}</p>}
    </div>
  )
}

/**
 * 目录还没拿到时的几种状态：加载中、这台服务没有这组接口（旧版本）、管理员关了、出错。
 * 拿到了返回 null。
 */
export function CatalogProblem({ query, onRetry }: { query: UseQueryResult<Catalog>; onRetry: () => void }) {
  const { t } = useT()
  if (query.isPending) return <StateBlock busy title={t('sq.explore.loading')} />
  if (!query.isError) return null
  const error = query.error
  // 404 有两种：这台服务关掉了数据浏览，或者它是没有这组接口的旧版本进程
  if (error instanceof SqlApiError && error.status === 404) {
    return error.code === 'disabled'
      ? <Notice title={t('sq.explore.disabled.title')}><p>{t('sq.explore.disabled.body')}</p></Notice>
      : <Notice title={t('sq.explore.unsupported.title')}><p>{t('sq.explore.unsupported.body')}</p></Notice>
  }
  return <SqlErrorBlock error={error} title={t('sq.explore.error.title')} onRetry={onRetry} />
}

/** 返回全部数据 */
export function BackToCatalog({ href }: { href: string }) {
  const { t } = useT()
  return <a className="btn btn-secondary" href={href}><Icon name="grid" size={14} />{t('sq.explore.backToCatalog')}</a>
}
