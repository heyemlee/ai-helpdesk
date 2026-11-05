/**
 * 表页上的概览（移植自旧控制台 exploreTable.js 的 insights）：多少条、状态怎么分布、落在哪些月份。
 *
 * 筛选条件管它下面的一切——概览里的数字和表格里的行说的永远是同一批记录。概览里的每个取值、
 * 每根柱子都能点，点一下就是加一个筛选条件，不用自己去拼"字段 / 条件 / 值"。
 */
import { useLayoutEffect, useRef, useState } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'
import { SqlApiError } from '@/api/sql/client'
import type { CatalogTable, TableProfile } from '@/api/sql/types'
import { useT } from '@/i18n'
import { rebucket, type Bar } from '@/lib/sql/charts'
import type { ViewFilter } from '@/lib/sql/dataView'
import { explainSqlError } from '@/lib/sql/errors'
import { formatCompact, formatCount, formatNumber, formatShare, labelOf } from '@/lib/sql/format'
import { hasKey } from '@/lib/sql/labels'
import { facetValues, statsOf, type ProfileSupport } from '@/lib/sql/profile'
import { SqlCode } from '../shared'
import { ColumnChart, StackedBar, ValueList, type VizItem } from './charts'
import { dateRange, sameValue, slotsFor, worthShowing } from './tableLogic'

const TOP_VALUES = 6          // 取值很多的字段，清单里先列最多的这几个

function SummaryCard({ table, profile, base }: { table: CatalogTable; profile: TableProfile; base: TableProfile | null }) {
  const { t, lang } = useT()
  const total = profile.total_rows
  const whole = base && typeof base.total_rows === 'number' ? base.total_rows : null
  const stats = new Map(statsOf(profile).map((column) => [column.name, column]))
  // 合计只给"语义层写了单位"的数字：单位都没有的数加在一起，没人知道加出来的是什么
  const measures = (table.columns ?? [])
    .filter((column) => column.unit && typeof stats.get(column.name)?.sum === 'number')
    .slice(0, 2)
  return (
    <section className="sq-insight sq-insight-summary">
      <h4 className="sq-insight-title">{t(profile.filtered ? 'sq.explore.viz.matching' : 'sq.explore.viz.records')}</h4>
      <p className="sq-hero" title={typeof total === 'number' ? formatCount(total, lang) : ''}>
        {typeof total === 'number' ? formatCompact(total, lang) : '—'}
      </p>
      {profile.filtered && whole !== null && (
        <p className="sq-muted sq-insight-sub">
          {t('sq.explore.viz.ofTotal', { total: formatCount(whole, lang), share: formatShare(total || 0, whole, lang) || '0%' })}
        </p>
      )}
      {measures.length > 0 && (
        <dl className="sq-measures">
          {measures.map((column) => {
            const item = stats.get(column.name)!
            const average = item.filled ? item.sum! / item.filled : null
            return (
              <div key={column.name}>
                <dt>{labelOf(column, lang)} <span className="sq-th-unit">({column.unit})</span></dt>
                <dd title={formatNumber(item.sum, lang, { unit: column.unit })}>
                  <span className="sq-muted">{t('sq.explore.viz.sum')}</span> {formatCompact(item.sum, lang, { unit: column.unit })}
                  {average !== null && (
                    <><span className="sq-muted sq-measure-avg">{t('sq.explore.viz.average')}</span> {formatCompact(average, lang, { unit: column.unit })}</>
                  )}
                </dd>
              </div>
            )
          })}
        </dl>
      )}
    </section>
  )
}

function FacetCard({ facet, rows, title, base, filters, onPick }: {
  facet: TableProfile['facets'][number]
  rows: number
  title: string
  base: TableProfile | null
  filters: ViewFilter[]
  onPick: (column: string, value: string) => void
}) {
  const { t, lang } = useT()
  const slots = slotsFor(facet.column, base)
  const active = filters.find((filter) => filter.column === facet.column && filter.op === 'eq')
  const values = facetValues(facet.values)
  const listed = values.reduce((sum, item) => sum + item.count, 0)
  const colored = slots.size > 0 && values.every((item) => slots.has(String(item.value)))
  const shown = colored ? values : values.slice(0, TOP_VALUES)
  const items: VizItem[] = shown.map((item) => ({
    value: item.value,
    text: String(item.value),
    count: item.count,
    slot: colored ? slots.get(String(item.value)) ?? 0 : 0,
    active: !!active && sameValue(active.value, item.value),
    truncated: !!item.truncated,
    muted: !!active && !sameValue(active.value, item.value),
  }))
  // 占比按"列出来的这些取值"算；没列全的（只列了最多的几个）按这批记录的总数算
  const whole = facet.complete ? listed : Math.max(rows || 0, listed)
  const more = typeof facet.distinct === 'number' && facet.distinct > shown.length
  return (
    <section className="sq-insight">
      <h4 className="sq-insight-title">
        {title}
        {facet.own_filters_excluded && (
          <span className="sq-insight-flag" title={t('sq.explore.viz.ownFilterHint')}>{t('sq.explore.viz.ownFilter')}</span>
        )}
      </h4>
      {colored && <StackedBar items={items} label={title} />}
      <ValueList items={items} total={whole} bars={!colored} onPick={(item) => onPick(facet.column, String(item.value))} />
      {more && (
        <p className="sq-muted sq-insight-note">
          {t('sq.explore.viz.topValues', { shown: shown.length, total: formatCount(facet.distinct, lang) })}
        </p>
      )}
    </section>
  )
}

function TimelineCard({ line, title, filters, onPick }: {
  line: TableProfile['timelines'][number]
  title: string
  filters: ViewFilter[]
  onPick: (column: string, bar: Bar) => void
}) {
  const { t, lang } = useT()
  const { grain, bars } = rebucket(line.buckets)
  const peak = bars.reduce((best, bar) => (bar.count > best.count ? bar : best), bars[0]!)
  const grainKey = `sq.explore.viz.grain.${grain}` as const
  return (
    <section className="sq-insight">
      <h4 className="sq-insight-title">{title}<span className="sq-insight-flag">{t(grainKey)}</span></h4>
      <ColumnChart bars={bars} label={t('sq.explore.viz.timelineLabel', { column: title })}
                   selected={dateRange(filters, line.column)} onPick={(bar) => onPick(line.column, bar)} />
      <p className="sq-muted sq-insight-note">
        {t('sq.explore.viz.peak', { label: peak.label, n: formatCount(peak.count, lang) })}
        {line.unparsed ? ` · ${t('sq.explore.viz.unparsed', { n: formatCount(line.unparsed, lang) })}` : ''}
        {line.truncated ? ` · ${t('sq.explore.viz.truncated')}` : ''}
      </p>
    </section>
  )
}

export function Insights({ table, support, enabled, query, base, filters, onFacet, onPeriod }: {
  table: CatalogTable
  support: ProfileSupport
  /** 用户自己关掉了概览 */
  enabled: boolean
  /** 当前这组条件下的概况（没有条件时就是整张表的那一份） */
  query: UseQueryResult<TableProfile>
  base: TableProfile | null
  filters: ViewFilter[]
  onFacet: (column: string, value: string) => void
  onPeriod: (column: string, bar: Bar) => void
}) {
  const { t, lang } = useT()
  const [expanded, setExpanded] = useState(false)
  const [fits, setFits] = useState(false)
  const grid = useRef<HTMLDivElement>(null)
  const profile = query.data

  // 概览收着的时候只露第一排。一排就放得下全部卡片时，"显示全部"这个按钮没有意义，收起来
  useLayoutEffect(() => {
    const node = grid.current
    if (!node || expanded) return
    setFits(node.scrollHeight <= node.clientHeight + 4)
  })

  if (support === 'unsupported') return <p className="sq-muted sq-insight-hint">{t('sq.explore.viz.unsupported')}</p>
  if (support !== 'on' || !enabled) return null
  if (query.isError) {
    // 这组条件下的统计没算出来：不拿上一组条件的数字顶在这里
    const error = query.error
    const code = error instanceof SqlApiError ? `sq.explore.error.${error.code}` : ''
    const reason = hasKey(code) ? t(code) : error instanceof SqlApiError && error.status === 0
      ? explainSqlError(error, t).body : t('sq.explore.viz.failed')
    return (
      <div className="sq-insight-hint">
        <span className="sq-muted">{t('sq.explore.viz.failedTitle')} {reason}</span>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => void query.refetch()}>{t('sq.action.retry')}</button>
      </div>
    )
  }
  if (!profile) return <p className="sq-muted sq-insight-hint" role="status">{t('sq.explore.viz.loading')}</p>

  const entries = new Map((table.columns ?? []).map((column) => [column.name, column]))
  const titleOf = (name: string) => { const entry = entries.get(name); return entry ? labelOf(entry, lang) : name }
  const rows = profile.total_rows || 0
  const facets = profile.facets.filter((facet) => worthShowing(facet, rows, filters))
  const timelines = profile.timelines.filter((line) => line.buckets.length)
  // 声明过取值的（状态、类型）先出，其次时间轴，再是别的分布
  const declared = facets.filter((facet) => facet.ordered_by === 'declared')
  const others = facets.filter((facet) => facet.ordered_by !== 'declared')
  const facetCard = (facet: TableProfile['facets'][number]) => (
    <FacetCard key={`f:${facet.column}`} facet={facet} rows={rows} title={titleOf(facet.column)} base={base}
               filters={filters} onPick={onFacet} />
  )
  const cards = [
    <SummaryCard key="summary" table={table} profile={profile} base={base} />,
    ...declared.map(facetCard),
    ...timelines.map((line) => (
      <TimelineCard key={`t:${line.column}`} line={line} title={titleOf(line.column)} filters={filters} onPick={onPeriod} />
    )),
    ...others.map(facetCard),
  ]
  const skipped = (profile.skipped ?? []).length
  const busy = query.isFetching && query.isPlaceholderData

  return (
    <section className="sq-insights" aria-label={t('sq.explore.insights')} aria-busy={busy}>
      <div ref={grid} className={`sq-insight-grid${expanded ? '' : ' is-collapsed'}`}>{cards}</div>
      <div className="sq-insight-foot">
        <span className="sq-muted">
          {t(profile.filtered ? 'sq.explore.viz.scopeFiltered' : 'sq.explore.viz.scopeAll')}
          {skipped ? ` · ${t('sq.explore.viz.skipped', { n: skipped })}` : ''}
        </span>
        {cards.length > 1 && (expanded || !fits) && (
          <button type="button" className="btn btn-ghost btn-sm" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
            {expanded ? t('sq.explore.viz.showLess') : t('sq.explore.viz.showAll', { n: cards.length })}
          </button>
        )}
      </div>
      {profile.sql?.length > 0 && (
        <details className="raw">
          <summary>{t('sq.explore.sql.profile')}</summary>
          <p className="sq-muted">{t('sq.explore.sql.profileNote', { n: profile.sql.length, dialect: profile.dialect })}</p>
          <SqlCode sql={profile.sql.join('\n\n')} />
        </details>
      )}
    </section>
  )
}
