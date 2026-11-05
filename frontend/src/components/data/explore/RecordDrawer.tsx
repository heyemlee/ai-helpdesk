/**
 * 一条记录（移植自旧控制台 exploreTable.js 的 drawer）：这条记录的全部字段，以及别的表里键值和它
 * 相同的记录——线只按目录登记的关联走（lib/sql/links.ts），不按字段名猜。
 */
import { useEffect, useRef, type ReactElement } from 'react'
import { useQueries, useQueryClient } from '@tanstack/react-query'
import { sqlApi } from '@/api/sql/client'
import type { Catalog, CatalogTable, RowsPage, TableProfile } from '@/api/sql/types'
import { useT } from '@/i18n'
import { exploreHref } from '@/lib/sql/dataView'
import { formatCell, formatCount, labelOf } from '@/lib/sql/format'
import { buildLinks } from '@/lib/sql/links'
import { peekProfile, profileSupport, useTableProfiles } from '@/lib/sql/profile'
import { Icon } from '../../Icon'
import { DataTable, ValuePill } from '../DataTable'
import { DbTile } from '../DbTile'
import { relatedTargets, briefColumns, slotsFor, type RelatedTarget } from './tableLogic'

const RELATED_ROWS = 5        // 每张相关的表先看几条
const MAX_RELATED = 8         // 一条记录最多同时去几张表里找
const RELATED_TTL_MS = 60_000 // 同一个键值刚查过就不再查：来回看两条记录不该把浏览的限流额度用掉

function RelatedSection({ catalog, target, result, mineLabel }: {
  catalog: Catalog
  target: RelatedTarget
  result: { status: 'pending' | 'success' | 'error'; data: RowsPage | undefined; refetch: () => void }
  mineLabel: string
}) {
  const { t, lang } = useT()
  const client = useQueryClient()
  const { other, value } = target
  const to = exploreHref(other.database, other.table, { filters: [{ column: other.column, op: 'eq', value: String(value) }] })
  // 两边的字段叫法一样就只写一次（订单号）；不一样才写成"物料名称 = 补件名称"
  const theirs = labelOf(other.columnEntry, lang)
  const via = mineLabel === theirs ? mineLabel : `${mineLabel} = ${theirs}`
  let body
  let count = ''
  if (result.status === 'error') {
    body = (
      <p className="sq-muted">
        {t('sq.explore.error.failed')}{' '}
        <button type="button" className="btn btn-ghost btn-sm" onClick={result.refetch}>{t('sq.action.retry')}</button>
      </p>
    )
  } else if (result.status !== 'success' || !result.data) {
    body = <p className="sq-muted" role="status">{t('sq.explore.loadingRows')}</p>
  } else if (!result.data.rows.length) {
    body = <p className="sq-muted">{t('sq.explore.related.none')}</p>
    count = formatCount(0, lang)
  } else {
    const data = result.data
    // 那张表的配色到了就用上：同一个状态在它自己的页面和这里是同一个颜色
    const profile = peekProfile(client, catalog, other.database, other.table)
    const columns = briefColumns(other.tableEntry, data, other.column, lang).map((item) => {
      const slots = profile ? slotsFor(item.key, profile) : null
      return slots && slots.size ? { ...item, slots, numeric: false } : item
    })
    const total = data.total_rows ?? data.rows.length
    count = `${formatCount(total, lang)}${data.total_rows == null && data.has_more ? '+' : ''}`
    body = (
      <>
        <DataTable columns={columns} rows={data.rows} label={t('sq.explore.tableLabel', { table: labelOf(other.tableEntry, lang) })} />
        {(total > data.rows.length || data.has_more) && (
          <p className="sq-rel-more"><a href={to}>{t('sq.explore.related.all', { n: count })} ›</a></p>
        )}
      </>
    )
  }
  return (
    <section className="sq-rel">
      <header className="sq-rel-head">
        <a className="sq-rel-name" href={to}>
          <DbTile catalog={catalog} database={other.database} size="is-xs" />{labelOf(other.tableEntry, lang)}
        </a>
        <span className="sq-muted sq-rel-via">{t('sq.explore.related.via', { key: via })}</span>
        {count && <span className="sq-badge">{count}</span>}
      </header>
      {body}
    </section>
  )
}

export function RecordDrawer({ catalog, database, table, data, index, base, settled, onStep, onClose }: {
  catalog: Catalog
  database: string
  table: CatalogTable
  data: RowsPage
  index: number
  base: TableProfile | null
  /** 连着翻的时候等人停下来再去查相关记录；字段内容是现成的，马上就画 */
  settled: boolean
  onStep: (delta: -1 | 1) => void
  onClose: () => void
}) {
  const { t, lang } = useT()
  const title = useRef<HTMLHeadingElement>(null)
  const row = data.rows[index] ?? []
  const names = data.columns.map((column) => column.name)
  const entries = new Map((table.columns ?? []).map((column) => [column.name, column]))
  const keyIndex = names.findIndex((name) => entries.get(name)?.primary_key)
  const lead = formatCell(row[keyIndex === -1 ? 0 : keyIndex], lang).text
  const targets = relatedTargets(buildLinks(catalog.domains), database, table.name, row, names)
  const shown = targets.slice(0, MAX_RELATED)

  useEffect(() => { title.current?.focus({ preventScroll: true }) }, [index])

  const related = useQueries({
    queries: shown.map((target) => ({
      queryKey: ['sql', 'related', catalog.generated_at, target.other.database, target.other.table, target.other.column,
        String(target.value)],
      queryFn: ({ signal }: { signal: AbortSignal }) => sqlApi.rows(target.other.database, target.other.table, {
        page: 1,
        page_size: RELATED_ROWS,
        filters: [{ column: target.other.column, op: 'eq', value: String(target.value) }],
      }, signal),
      enabled: settled,
      staleTime: RELATED_TTL_MS,
      retry: false,
    })),
  })
  // 相关表的配色：要一份那张表的概况（已经有了就不再取）
  const otherTables = [...new Map(shown.map((target) => [`${target.other.database}\u0000${target.other.table}`,
    { database: target.other.database, table: target.other.table }])).values()]
  useTableProfiles(settled && profileSupport(catalog.service) === 'on' ? catalog : null, otherTables)

  const filled: ReactElement[] = []
  const blank: string[] = []
  names.forEach((name, position) => {
    const entry = entries.get(name)
    const label = entry ? labelOf(entry, lang) : name
    const cell = formatCell(row[position], lang, {
      unit: entry?.unit ?? '',
      plain: !!entry && (entry.role === 'join_key' || entry.primary_key),
      kind: data.columns[position]?.kind === 'number' ? 'number' : '',
    })
    if (cell.empty) { blank.push(label); return }
    const slot = slotsFor(name, base).get(String(row[position]))
    filled.push(
      <div key={name}>
        <dt>{label}{entry?.unit && <span className="sq-th-unit"> ({entry.unit})</span>}<span className="sq-tech">{name}</span></dt>
        <dd className={cell.numeric ? 'sq-num-value' : undefined}>{slot ? <ValuePill text={cell.text} slot={slot} /> : cell.text}</dd>
      </div>,
    )
  })

  return (
    <aside className="sq-drawer" aria-label={t('sq.explore.record.title')}>
      <header className="sq-drawer-head">
        <div className="sq-grow">
          <p className="sq-drawer-kicker">
            <DbTile catalog={catalog} database={database} size="is-xs" />
            {labelOf(table, lang)} · {t('sq.explore.record.position', { n: formatCount(data.offset + index + 1, lang) })}
          </p>
          <h3 className="sq-drawer-title" tabIndex={-1} ref={title}>{lead}</h3>
        </div>
        <div className="sq-drawer-tools">
          <button type="button" className="icon-btn" title={t('sq.explore.record.prev')} aria-label={t('sq.explore.record.prev')}
                  disabled={index <= 0} onClick={() => onStep(-1)}>‹</button>
          <button type="button" className="icon-btn" title={t('sq.explore.record.next')} aria-label={t('sq.explore.record.next')}
                  disabled={index >= data.rows.length - 1} onClick={() => onStep(1)}>›</button>
          <button type="button" className="icon-btn" title={t('sq.explore.record.close')} aria-label={t('sq.explore.record.close')}
                  onClick={onClose}><Icon name="close" /></button>
        </div>
      </header>
      <div className="sq-drawer-body">
        <dl className="sq-rec">{filled}</dl>
        {blank.length > 0 && (
          <p className="sq-muted sq-rec-blank">{t('sq.explore.record.blank', { names: blank.join(t('sq.explore.listSep')) })}</p>
        )}
        {targets.length > 0 && (
          <>
            <h4 className="sq-drawer-section">{t('sq.explore.related.title')}</h4>
            <p className="sq-muted sq-drawer-lead">{t('sq.explore.related.lead')}</p>
            {shown.map((target, position) => {
              const entry = entries.get(target.column)
              const result = related[position]!
              return (
                <RelatedSection key={`${target.column}\u0000${target.other.database}\u0000${target.other.table}\u0000${target.other.column}`}
                  catalog={catalog} target={target} mineLabel={entry ? labelOf(entry, lang) : target.column}
                  result={{ status: settled ? result.status : 'pending', data: result.data, refetch: () => void result.refetch() }} />
              )
            })}
            {targets.length > MAX_RELATED && (
              <p className="sq-muted">{t('sq.explore.related.more', { n: targets.length - MAX_RELATED })}</p>
            )}
          </>
        )}
      </div>
    </aside>
  )
}
