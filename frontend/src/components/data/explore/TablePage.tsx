/**
 * 一张表的工作区（移植自旧控制台 exploreTable.js）：先看整体（多少条、状态怎么分布、落在哪些月份），
 * 再看记录，点开一条记录还能看到别的库里和它对得上的记录。
 *
 * 从上到下是一条线：搜索与筛选 → 概览 → 记录。筛选条件管它下面的一切。
 *
 * "正在怎么看"（搜索、筛选、排序、页码、标签页）都在地址里（lib/sql/dataView.ts）：这一页从地址读，
 * 改的时候换掉地址（不产生新的历史记录），刷新、复制链接都还是现在这个样子。
 *
 * 数据全部来自服务端：记录来自 /catalog/{库}/{表}/rows，统计来自 …/profile，两者都按当前身份算。
 * 这个文件里没有任何表名、字段名或样例数据。
 */
import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { keepPreviousData, useQuery, type UseQueryResult } from '@tanstack/react-query'
import { sqlApi, SqlApiError } from '@/api/sql/client'
import type { Catalog, CatalogDomain, CatalogTable, PreviewOperator, RowsPage, TableProfile } from '@/api/sql/types'
import { useT } from '@/i18n'
import type { Key } from '@/i18n/catalog'
import { domainTitle } from '@/lib/sql/catalog'
import type { Bar } from '@/lib/sql/charts'
import {
  exploreHref, MAX_FILTERS, PAGE_SIZES, parseView, replaceHash, VALUELESS, type TableView, type ViewFilter,
} from '@/lib/sql/dataView'
import { explainSqlError } from '@/lib/sql/errors'
import { formatCount, formatRange, formatTime, labelOf, toTsv } from '@/lib/sql/format'
import { hasKey } from '@/lib/sql/labels'
import { buildLinks, relatedTables, type LinkNode } from '@/lib/sql/links'
import { profileSupport, useTableProfile } from '@/lib/sql/profile'
import { Icon } from '../../Icon'
import { StateBlock } from '../../ui'
import { DataTable, type TableSort } from '../DataTable'
import { DbTile } from '../DbTile'
import { CopyButton, SqlCode } from '../shared'
import { askAbout } from './CatalogPages'
import { FieldsTab } from './FieldsTab'
import { Insights } from './Insights'
import { BackToCatalog, Notice, TableSummary, UnavailableNotice, fieldCount, recordCountText } from './parts'
import { RecordDrawer } from './RecordDrawer'
import {
  chipItems, dateRange, emptyColumns, gridColumns, knownValues, operatorsFor, sameValue,
} from './tableLogic'

const SEARCH_DELAY_MS = 350
const STEP_DELAY_MS = 350     // 用"下一条"快速翻的时候，停下来才去查相关记录
const INSIGHTS_KEY = 'helpdesk.sql.insights'

const readInsights = () => {
  try { return globalThis.localStorage?.getItem(INSIGHTS_KEY) !== 'off' } catch { return true }
}
const writeInsights = (on: boolean) => {
  try { globalThis.localStorage?.setItem(INSIGHTS_KEY, on ? 'on' : 'off') } catch { /* 存不下就只在这一页有效 */ }
}

/** 表页：库读不到、表不存在时说清楚；否则进工作区 */
export function TablePage({ catalog, domain, entry, database, tableName, query, onRefresh }: {
  catalog: Catalog
  domain: CatalogDomain | null
  entry: CatalogTable | null
  database: string
  tableName: string
  query: string
  onRefresh: () => void
}) {
  const { t, lang } = useT()
  if (domain && domain.status !== 'available' && !entry) {
    // 库读不到时目录里没有它的表清单：这不是"没有这张表"，是"现在看不了"
    return (
      <div className="sq-stack">
        <nav className="sq-breadcrumbs" aria-label={t('sq.explore.breadcrumb')}>
          <a href={exploreHref()}>{t('sq.explore.title')}</a><span aria-hidden="true">›</span>
          <a href={exploreHref(domain.database)}>{domainTitle(domain, lang)}</a>
        </nav>
        <h2 className="page-title" tabIndex={-1}>{tableName}</h2>
        <UnavailableNotice domain={domain} />
        <div className="sq-actions">
          <button type="button" className="btn btn-secondary" onClick={onRefresh}>
            <Icon name="refresh" size={14} />{t('sq.action.retry')}
          </button>
          <BackToCatalog href={exploreHref()} />
        </div>
      </div>
    )
  }
  if (!domain || !entry) {
    return (
      <div className="sq-stack">
        <nav className="sq-breadcrumbs" aria-label={t('sq.explore.breadcrumb')}><a href={exploreHref()}>{t('sq.explore.title')}</a></nav>
        <h2 className="page-title" tabIndex={-1}>{t('sq.explore.notFound.title')}</h2>
        <Notice title={t('sq.explore.notFound.title')}><p>{t('sq.explore.notFound.body')}</p></Notice>
        <div><BackToCatalog href={exploreHref()} /></div>
      </div>
    )
  }
  return <TableWorkspace catalog={catalog} domain={domain} entry={entry} database={database} tableName={tableName} query={query} />
}

function TableWorkspace({ catalog, domain, entry, database, tableName, query }: {
  catalog: Catalog
  domain: CatalogDomain
  entry: CatalogTable
  database: string
  tableName: string
  query: string
}) {
  const { t, lang } = useT()
  const view = useMemo(() => parseView(query), [query])
  const viewRef = useRef(view)
  viewRef.current = view
  // 改的总是最新的那份：防抖的搜索晚 350ms 才提交，不能拿旧的视图把这期间的翻页盖回去
  const applyView = (change: Partial<TableView>) =>
    replaceHash(exploreHref(database, tableName, { ...viewRef.current, ...change }))

  // 表很多的部署里 /catalog 不带字段，用到这张表时再取。取不到字段说明时预览照常
  const needColumns = !Array.isArray(entry.columns)
  const detail = useQuery({
    queryKey: ['sql', 'table', catalog.generated_at, database, tableName],
    queryFn: () => sqlApi.table(database, tableName),
    enabled: needColumns,
    staleTime: Infinity,
    retry: false,
  })
  const table: CatalogTable = needColumns && detail.data ? { ...entry, ...detail.data } : entry

  const available = table.status === 'available'
  const unavailableDomain = domain.status !== 'available'
  const usable = available && !unavailableDomain && table.previewable
  const filtered = !!(view.search || view.filters.length)
  const support = profileSupport(catalog.service)
  const base = useTableProfile(catalog, database, tableName, {}, usable)
  const scoped = useTableProfile(catalog, database, tableName, { search: view.search, filters: view.filters }, usable, true)
  const baseData = base.data ?? null

  const rows = useQuery({
    queryKey: ['sql', 'rows', catalog.generated_at, database, tableName, view.page, view.pageSize, view.search,
      view.filters, view.sort],
    queryFn: ({ signal }) => sqlApi.rows(database, tableName, {
      page: view.page,
      page_size: view.pageSize,
      search: view.search,
      filters: view.filters,
      sort: view.sort ? { column: view.sort.key, direction: view.sort.direction } : null,
    }, signal),
    enabled: usable && view.tab === 'records',
    placeholderData: keepPreviousData,
    retry: false,
  })

  const [panel, setPanel] = useState<'' | 'filter' | 'columns'>('')
  const [hidden, setHidden] = useState<Set<string>>(() => new Set())
  const [showEmpty, setShowEmpty] = useState(false)
  const [insightsOn, setInsightsOn] = useState(readInsights)
  const [notice, setNotice] = useState('')
  const [detailIndex, setDetailIndex] = useState<number | null>(null)
  const [settled, setSettled] = useState(true)
  const settleTimer = useRef<number | undefined>(undefined)
  const results = useRef<HTMLDivElement>(null)

  const entries = useMemo(() => new Map((table.columns ?? []).map((column) => [column.name, column])), [table.columns])
  const nameOf = (name: string) => { const column = entries.get(name); return column ? labelOf(column, lang) : name }
  const blank = emptyColumns(baseData).filter((name) => entries.has(name))
  const concealed = new Set([...hidden, ...(showEmpty ? [] : blank)].filter((name) => entries.has(name)))
  const maxFilters = catalog.service?.preview?.max_filters || MAX_FILTERS

  // 换了一页、换了条件：打开着的那条记录不再是这一页上的那一条
  const rowsKey = JSON.stringify([view.page, view.pageSize, view.search, view.filters, view.sort])
  useEffect(() => { setDetailIndex(null) }, [rowsKey])
  useEffect(() => () => window.clearTimeout(settleTimer.current), [])
  useEffect(() => { if (notice) { const timer = window.setTimeout(() => setNotice(''), 4000); return () => window.clearTimeout(timer) } }, [notice])

  const closeRow = () => {
    const index = detailIndex
    setDetailIndex(null)
    window.clearTimeout(settleTimer.current)
    // 焦点还给刚才那一行：用键盘的人关掉详情后接着往下看，不用从头再找
    if (index !== null) {
      window.setTimeout(() => {
        const row = results.current?.querySelectorAll<HTMLTableRowElement>('tbody tr')[index]
        row?.focus({ preventScroll: true })
      }, 0)
    }
  }
  const openRow = (index: number, settle = 0) => {
    window.clearTimeout(settleTimer.current)
    setDetailIndex(index)
    if (settle) {
      setSettled(false)
      settleTimer.current = window.setTimeout(() => setSettled(true), settle)
    } else {
      setSettled(true)
    }
  }

  // 关掉记录详情：焦点不一定在表格里（点过页面空白处就在 body 上），所以挂在整个文档上
  useEffect(() => {
    if (detailIndex === null) return
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape' || document.querySelector('dialog[open]')) return
      event.preventDefault()
      closeRow()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  })

  const replaceFilters = (column: string, next: ViewFilter[]) => {
    const rest = view.filters.filter((filter) => filter.column !== column)
    if (rest.length + next.length > maxFilters) {
      setNotice(t('sq.explore.filter.tooMany', { n: maxFilters }))
      return
    }
    applyView({ filters: [...rest, ...next], page: 1 })
  }
  const onFacet = (column: string, value: string) => {
    const active = view.filters.find((filter) => filter.column === column && filter.op === 'eq')
    replaceFilters(column, active && sameValue(active.value, value) ? [] : [{ column, op: 'eq', value }])
  }
  const onPeriod = (column: string, bar: Bar) => {
    const range = dateRange(view.filters, column)
    replaceFilters(column, range && range.start === bar.start && range.end === bar.end ? [] : [
      { column, op: 'gte', value: bar.start }, { column, op: 'lt', value: bar.end },
    ])
  }

  const links = buildLinks(catalog.domains)
  const relatedOthers = [...new Map(relatedTables(links, database, table.name)
    .flatMap((item) => item.others)
    .map((other): [string, LinkNode] => [`${other.database}\u0000${other.table}`, other])).values()]
  const headMeta = [
    baseData && typeof baseData.total_rows === 'number' ? recordCountText(baseData.total_rows, t, lang) : '',
    t('sq.explore.fieldCount', { n: fieldCount(table) }),
  ].filter(Boolean).join(' · ')

  const selectTab = (tab: TableView['tab']) => applyView({ tab })
  const onTabKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
    event.preventDefault()
    const next = view.tab === 'records' ? 'fields' : 'records'
    selectTab(next)
    window.setTimeout(() => document.getElementById(`sq-tab-${next}`)?.focus(), 0)
  }

  let body
  if (view.tab === 'fields') {
    body = <FieldsTab catalog={catalog} table={table} base={baseData} />
  } else if (!available || unavailableDomain) {
    body = <p className="sq-muted">{t('sq.explore.noPreview')}</p>
  } else if (!table.previewable) {
    body = (
      <Notice title={t('sq.explore.previewUnsupported.title')}>
        <p>{t(domain.preview_supported ? 'sq.explore.previewUnsupported.names' : 'sq.explore.previewUnsupported.body')}</p>
      </Notice>
    )
  } else {
    const columnsTotal = (table.columns ?? []).filter((column) => column.status === 'available' && column.previewable !== false).length
    const chips = chipItems(view.filters, nameOf, (op) => t(`sq.explore.filter.op.${op}` as Key),
      (from, to) => t('sq.explore.filter.range', { from, to }))
    body = (
      <div className="sq-records sq-stack-sm">
        <div className="sq-tb">
          <SearchBox search={view.search} onCommit={(term) => { if (term !== viewRef.current.search) applyView({ search: term, page: 1 }) }} />
          <div className="sq-tb-tools">
            <button type="button" className="btn btn-secondary btn-sm" aria-expanded={panel === 'filter'}
                    onClick={() => setPanel(panel === 'filter' ? '' : 'filter')}>
              {t('sq.explore.filter.title')}{chips.length > 0 && <span className="sq-badge">{chips.length}</span>}
            </button>
            <span className="sq-tb-space" />
            {support === 'on' && (
              <button type="button" className="btn btn-ghost btn-sm" aria-pressed={insightsOn}
                      onClick={() => { setInsightsOn(!insightsOn); writeInsights(!insightsOn) }}>
                {t('sq.explore.insights')}
              </button>
            )}
            <button type="button" className="btn btn-ghost btn-sm" aria-expanded={panel === 'columns'}
                    onClick={() => setPanel(panel === 'columns' ? '' : 'columns')}>
              {t('sq.explore.columns')}
              {concealed.size > 0 && <span className="sq-badge sq-badge-soft">{columnsTotal - concealed.size}/{columnsTotal}</span>}
            </button>
            {rows.data && rows.data.rows.length > 0 && (
              <CopyButton label={t('sq.explore.copyPage')} text={(() => {
                const columns = gridColumns(table, rows.data, concealed, baseData, lang)
                return toTsv(columns.map((column) => (column.unit ? `${column.label} (${column.unit})` : column.label)),
                  rows.data.rows.map((row) => columns.map((column) => row[column.index!])))
              })()} />
            )}
          </div>
        </div>
        {panel === 'filter' && (
          <FilterForm table={table} filters={view.filters} maxFilters={maxFilters} base={baseData}
                      allowed={catalog.service?.preview?.operators ?? null}
                      onAdd={(filter) => applyView({ filters: [...view.filters, filter], page: 1 })} />
        )}
        {panel === 'columns' && (
          <ColumnsForm table={table} hidden={hidden} blank={blank} showEmpty={showEmpty} concealed={concealed}
                       onToggle={(name, on) => {
                         const next = new Set(hidden)
                         if (on) {
                           next.delete(name)
                           // 勾上一个空字段，就是想看空字段了
                           if (blank.includes(name)) setShowEmpty(true)
                         } else next.add(name)
                         setHidden(next)
                       }}
                       onShowAll={() => { setHidden(new Set()); setShowEmpty(true) }} />
        )}
        {(chips.length > 0 || view.search) && (
          <div className="sq-filter-chips" aria-label={t('sq.explore.filter.active')}>
            {view.search && (
              <span className="sq-filter-chip">
                {t('sq.explore.filter.searchChip', { term: view.search })}
                <button type="button" title={t('sq.explore.filter.remove')}
                        aria-label={t('sq.explore.filter.removeNamed', { name: t('sq.explore.filter.searchChip', { term: view.search }) })}
                        onClick={() => applyView({ search: '', page: 1 })}><Icon name="close" size={12} /></button>
              </span>
            )}
            {chips.map((item) => (
              <span key={item.column ?? `i${item.index}`} className="sq-filter-chip">
                {item.label}
                <button type="button" title={t('sq.explore.filter.remove')}
                        aria-label={t('sq.explore.filter.removeNamed', { name: item.label })}
                        onClick={() => applyView({
                          filters: item.column !== undefined
                            ? view.filters.filter((filter) => filter.column !== item.column)
                            : view.filters.filter((_, position) => position !== item.index),
                          page: 1,
                        })}><Icon name="close" size={12} /></button>
              </span>
            ))}
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => applyView({ search: '', filters: [], page: 1 })}>
              {t('sq.explore.filter.clear')}
            </button>
          </div>
        )}
        {notice && <p className="note caution" role="status">{notice}</p>}
        <Insights table={table} support={support} enabled={insightsOn} query={scoped} base={baseData}
                  filters={view.filters} onFacet={onFacet} onPeriod={onPeriod} />
        <div ref={results}>
          <Results table={table} query={rows} view={view} filtered={filtered} concealed={concealed} base={baseData}
                   blankHidden={showEmpty ? [] : blank.filter((name) => !hidden.has(name))}
                   selected={detailIndex ?? -1}
                   onShowEmpty={() => setShowEmpty(true)}
                   onApply={applyView}
                   onRow={(index) => {
                     // 在单元格里拖选文字不算点开这一行
                     if (String(window.getSelection?.() ?? '')) return
                     if (detailIndex === index) closeRow()
                     else openRow(index)
                   }} />
        </div>
      </div>
    )
  }

  return (
    <div className="sq-stack">
      <div>
        <nav className="sq-breadcrumbs" aria-label={t('sq.explore.breadcrumb')}>
          <a href={exploreHref()}>{t('sq.explore.title')}</a><span aria-hidden="true">›</span>
          <a href={exploreHref(domain.database)}>{domainTitle(domain, lang)}</a><span aria-hidden="true">›</span>
          <span>{labelOf(table, lang)}</span>
        </nav>
        <div className="page-heading">
          <div className="sq-dx-title">
            <DbTile catalog={catalog} database={domain.database} size="is-lg" />
            <div>
              <h2 className="page-title" tabIndex={-1}>{labelOf(table, lang)}</h2>
              <p className="sq-tech">{domain.database}.{table.name} · {t('sq.explore.readOnly')} · {headMeta}</p>
            </div>
          </div>
          <div className="page-heading-actions">
            <button type="button" className="btn btn-secondary" onClick={() => askAbout(labelOf(table, lang), t)}>
              <Icon name="spark" size={14} />{t('sq.explore.askAbout')}
            </button>
          </div>
        </div>
        <p className="page-lede"><TableSummary table={table} /></p>
      </div>
      {relatedOthers.length > 0 && (
        <p className="sq-rel-chips">
          <span className="sq-muted">{t('sq.explore.relatedTables')}</span>
          {relatedOthers.map((other) => (
            <a key={`${other.database}.${other.table}`} className="sq-chip-link" href={exploreHref(other.database, other.table)}>
              <DbTile catalog={catalog} database={other.database} size="is-xs" />{labelOf(other.tableEntry, lang)}
            </a>
          ))}
        </p>
      )}
      {!available && <Notice tone="caution" title={t('sq.explore.missing.title')}><p>{t('sq.explore.missing.body')}</p></Notice>}
      {unavailableDomain && <Notice tone="danger" title={t('sq.explore.unavailable.title')}><p>{t('sq.explore.unavailable.body')}</p></Notice>}
      <div className="tabs" role="tablist" aria-label={labelOf(table, lang)} onKeyDown={onTabKey}>
        {(['records', 'fields'] as const).map((tab) => (
          <button key={tab} type="button" role="tab" id={`sq-tab-${tab}`} aria-controls="sq-tabpanel"
                  aria-selected={view.tab === tab} tabIndex={view.tab === tab ? 0 : -1}
                  className={`tab${view.tab === tab ? ' is-active' : ''}`} onClick={() => selectTab(tab)}>
            {tab === 'records' ? t('sq.explore.tab.records') : t('sq.explore.tab.fields', { n: (table.columns ?? []).length })}
          </button>
        ))}
      </div>
      <div id="sq-tabpanel" role="tabpanel" aria-labelledby={`sq-tab-${view.tab}`}>{body}</div>
      {detailIndex !== null && rows.data?.rows[detailIndex] && (
        <RecordDrawer catalog={catalog} database={database} table={table} data={rows.data} index={detailIndex}
                      base={baseData} settled={settled} onClose={closeRow}
                      onStep={(delta) => openRow(detailIndex + delta, STEP_DELAY_MS)} />
      )}
    </div>
  )
}

/** 边打边搜：停手一小会儿再发，不是每敲一个字发一次；回车马上发 */
function SearchBox({ search, onCommit }: { search: string; onCommit: (term: string) => void }) {
  const { t } = useT()
  const [text, setText] = useState(search)
  const timer = useRef<number | undefined>(undefined)
  useEffect(() => { setText(search) }, [search])
  useEffect(() => () => window.clearTimeout(timer.current), [])
  const commit = (value: string) => {
    window.clearTimeout(timer.current)
    onCommit(value.trim())
  }
  return (
    <form className="sq-tb-search" role="search" onSubmit={(event: FormEvent) => { event.preventDefault(); commit(text) }}>
      <label className="sr-only" htmlFor="sq-table-search">{t('sq.explore.search')}</label>
      <Icon name="search" size={14} />
      <input className="field" id="sq-table-search" type="search" maxLength={100} value={text} autoComplete="off"
             placeholder={t('sq.explore.searchPlaceholder')}
             onChange={(event) => {
               const value = event.target.value
               setText(value)
               window.clearTimeout(timer.current)
               timer.current = window.setTimeout(() => commit(value), SEARCH_DELAY_MS)
             }} />
    </form>
  )
}

function FilterForm({ table, filters, maxFilters, base, allowed, onAdd }: {
  table: CatalogTable
  filters: ViewFilter[]
  maxFilters: number
  base: TableProfile | null
  allowed: string[] | null
  onAdd: (filter: ViewFilter) => void
}) {
  const { t, lang } = useT()
  const columns = (table.columns ?? []).filter((column) => column.status === 'available' && column.previewable !== false)
  const [columnName, setColumnName] = useState('')
  const [op, setOp] = useState<PreviewOperator | ''>('')
  const [value, setValue] = useState('')
  const valueInput = useRef<HTMLInputElement>(null)
  const chosen = columns.find((column) => column.name === columnName) ?? columns[0]
  const operators = operatorsFor(chosen, allowed)
  const current = operators.includes(op as PreviewOperator) ? (op as PreviewOperator) : operators[0]
  const valueless = !!current && VALUELESS.includes(current)
  const full = filters.length >= maxFilters
  const known = chosen ? knownValues(base, chosen.name) : []

  useEffect(() => { valueInput.current?.focus() }, [])

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const typed = valueless ? '' : value.trim()
    if (!chosen || !current || (!valueless && !typed)) {
      valueInput.current?.focus()
      return
    }
    if (full) return
    onAdd({ column: chosen.name, op: current, value: typed })
    setValue('')
    valueInput.current?.focus()
  }

  return (
    <form className="sq-filters sq-panel" onSubmit={submit}>
      <label className="field-row">
        <span>{t('sq.explore.filter.field')}</span>
        <select className="field field-compact" value={chosen?.name ?? ''} onChange={(event) => { setColumnName(event.target.value); setOp('') }}>
          {columns.map((column) => <option key={column.name} value={column.name}>{labelOf(column, lang)}</option>)}
        </select>
      </label>
      <label className="field-row">
        <span>{t('sq.explore.filter.condition')}</span>
        <select className="field field-compact" value={current ?? ''} onChange={(event) => setOp(event.target.value as PreviewOperator)}>
          {operators.map((item) => <option key={item} value={item}>{t(`sq.explore.filter.op.${item}` as Key)}</option>)}
        </select>
      </label>
      <label className="field-row sq-grow" hidden={valueless}>
        <span>{t('sq.explore.filter.value')}</span>
        <input ref={valueInput} className="field" maxLength={200} autoComplete="off" value={value}
               onChange={(event) => setValue(event.target.value)}
               type={chosen?.kind === 'date' ? 'date' : 'text'}
               inputMode={chosen?.kind === 'number' ? 'decimal' : undefined}
               list={known.length ? 'sq-filter-values' : undefined}
               placeholder={chosen?.value_range && !known.length ? chosen.value_range : ''} />
        {known.length > 0 && (
          <datalist id="sq-filter-values">{known.map((item) => <option key={item} value={item} />)}</datalist>
        )}
      </label>
      <button type="submit" className="btn btn-secondary" disabled={full}>
        <Icon name="plus" size={14} />{t('sq.explore.filter.add')}
      </button>
      {full && <p className="sq-muted sq-panel-note">{t('sq.explore.filter.tooMany', { n: maxFilters })}</p>}
    </form>
  )
}

function ColumnsForm({ table, hidden, blank, showEmpty, concealed, onToggle, onShowAll }: {
  table: CatalogTable
  hidden: Set<string>
  blank: string[]
  showEmpty: boolean
  concealed: Set<string>
  onToggle: (name: string, on: boolean) => void
  onShowAll: () => void
}) {
  const { t, lang } = useT()
  const columns = (table.columns ?? []).filter((column) => column.status === 'available' && column.previewable !== false)
  return (
    <div className="sq-panel">
      <fieldset className="sq-column-picks">
        <legend className="sr-only">{t('sq.explore.columns')}</legend>
        {columns.map((column) => {
          const off = hidden.has(column.name) || (!showEmpty && blank.includes(column.name))
          return (
            <label key={column.name} className="sq-check">
              <input type="checkbox" checked={!off} onChange={(event) => onToggle(column.name, event.target.checked)} />
              <span>{labelOf(column, lang)}</span>
              {blank.includes(column.name) && <span className="sq-muted">{t('sq.explore.columnEmpty')}</span>}
            </label>
          )
        })}
      </fieldset>
      <div className="sq-actions">
        {concealed.size > 0 && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={onShowAll}>{t('sq.explore.showAllColumns')}</button>
        )}
        <span className="sq-muted">{t('sq.explore.columnsNote')}</span>
      </div>
    </div>
  )
}

/** 预览出错：按服务端的错误码说人话（数据浏览接口的 detail 是 {code, message}） */
function PreviewError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const { t } = useT()
  const code = error instanceof SqlApiError ? error.code : ''
  const key = `sq.explore.error.${code}`
  const general = explainSqlError(error, t)
  const body = hasKey(key) ? t(key) : error instanceof SqlApiError && [0, 401, 403, 429, 502, 503, 504].includes(error.status)
    ? general.body : t('sq.explore.error.failed')
  const detail = error instanceof SqlApiError
    ? [error.status ? `HTTP ${error.status}` : '', code, error.serverMessage].filter(Boolean).join(' · ')
    : general.detail
  return (
    <StateBlock tone="danger" title={t('sq.explore.error.previewTitle')} detail={detail || undefined}
      action={<button type="button" className="btn btn-secondary" onClick={onRetry}><Icon name="refresh" size={14} />{t('sq.action.retry')}</button>}>
      {body}
    </StateBlock>
  )
}

function Results({ table, query, view, filtered, concealed, base, blankHidden, selected, onShowEmpty, onApply, onRow }: {
  table: CatalogTable
  query: UseQueryResult<RowsPage>
  view: TableView
  filtered: boolean
  concealed: Set<string>
  base: TableProfile | null
  blankHidden: string[]
  selected: number
  onShowEmpty: () => void
  onApply: (change: Partial<TableView>) => void
  onRow: (index: number) => void
}) {
  const { t, lang } = useT()
  if (query.isError) return <PreviewError error={query.error} onRetry={() => void query.refetch()} />
  const data = query.data
  if (!data) return <StateBlock busy title={t('sq.explore.loadingRows')} />

  const loading = query.isFetching && query.isPlaceholderData
  const columns = gridColumns(table, data, concealed, base, lang)
  const entries = new Map((table.columns ?? []).map((column) => [column.name, column]))
  const from = data.offset + 1
  const to = data.offset + data.rows.length
  const dates = (data.date_ranges ?? []).map((range) =>
    `${entries.has(range.column) ? labelOf(entries.get(range.column), lang) : range.column} ${formatRange(range)}`)
  const pages = data.total_rows != null ? Math.max(1, Math.ceil(data.total_rows / data.page_size)) : null
  const count = !data.rows.length ? '' : data.total_rows != null
    ? t('sq.explore.range', { from: formatCount(from, lang), to: formatCount(to, lang), total: formatCount(data.total_rows, lang) })
    : t('sq.explore.rangeUnknown', { from: formatCount(from, lang), to: formatCount(to, lang) })
  const sort: TableSort | null = view.sort ? { key: view.sort.key, direction: view.sort.direction } : null

  let body
  if (!data.rows.length && filtered) {
    body = (
      <StateBlock title={t('sq.explore.noMatch.title')}
        action={<button type="button" className="btn btn-secondary" onClick={() => onApply({ search: '', filters: [], page: 1 })}>{t('sq.explore.filter.clear')}</button>}>
        {t('sq.explore.noMatch.body')}
      </StateBlock>
    )
  } else if (!data.rows.length && data.offset > 0) {
    body = (
      <StateBlock title={t('sq.explore.pastEnd.title')}
        action={<button type="button" className="btn btn-secondary" onClick={() => onApply({ page: 1 })}>{t('sq.explore.firstPage')}</button>} />
    )
  } else if (!data.rows.length) {
    body = <StateBlock title={t('sq.explore.emptyTable.title')}>{t('sq.explore.emptyTable.body')}</StateBlock>
  } else {
    body = (
      <DataTable columns={columns} rows={data.rows} label={t('sq.explore.tableLabel', { table: labelOf(table, lang) })}
        sort={sort} selected={selected} onRowClick={onRow}
        onSort={(key) => {
          let next: TableView['sort'] = null
          if (!view.sort || view.sort.key !== key) next = { key, direction: 'asc' }
          else if (view.sort.direction === 'asc') next = { key, direction: 'desc' }
          onApply({ sort: next, page: 1 })
        }} />
    )
  }

  return (
    <div className={`sq-stack-sm${loading ? ' is-stale' : ''}`} aria-busy={loading}>
      <div className="sq-result-meta" role="status">
        {count && <span><strong>{count}</strong></span>}
        {(data.rows.length > 0 || dates.length > 0) && (
          <span><strong>{t('sq.explore.meta.recordDates')}:</strong> {dates.length ? dates.join('; ') : t('sq.explore.noDateFields')}</span>
        )}
        <span title={t('sq.explore.meta.readHint')}>
          {data.as_of_source === 'snapshot'
            ? t('sq.explore.meta.readSnapshot', { time: formatTime(data.as_of, lang) })
            : data.as_of ? t('sq.explore.meta.readLive', { time: formatTime(data.as_of, lang) }) : t('sq.explore.meta.readUnknown')}
        </span>
        {blankHidden.length > 0 && (
          <span>
            {t('sq.explore.emptyHidden', { n: blankHidden.length })}{' '}
            <button type="button" className="btn btn-ghost btn-sm" onClick={onShowEmpty}>{t('sq.explore.showThem')}</button>
          </span>
        )}
        {loading && <span>{t('sq.explore.loadingRows')}</span>}
      </div>
      {data.columns_hidden && <p className="sq-muted">{t('sq.explore.columnsHidden')}</p>}
      {(data.columns_unsupported ?? []).length > 0 && (
        <p className="sq-muted">{t('sq.explore.columnsUnsupported', { names: data.columns_unsupported.join(', ') })}</p>
      )}
      {data.search_columns_limited && (
        <p className="sq-muted">{t('sq.explore.searchLimited', { n: (data.search_columns ?? []).length })}</p>
      )}
      {data.cells_truncated && <p className="sq-muted">{t('sq.explore.cellsTruncated', { n: data.cell_limit })}</p>}
      {data.stable_order === false && (data.has_more || data.page > 1) && <p className="sq-muted">{t('sq.explore.unstableOrder')}</p>}
      {body}
      <div className="sq-table-foot">
        <span className="sq-actions">
          <label className="sq-actions">
            {t('sq.explore.pageSize')}
            <select className="field field-compact" value={view.pageSize}
                    onChange={(event) => onApply({ pageSize: Number(event.target.value) || PAGE_SIZES[0], page: 1 })}>
              {PAGE_SIZES.map((size) => <option key={size} value={size}>{size}</option>)}
            </select>
          </label>
          {data.rows.length > 0 && <span className="sq-muted">{t('sq.explore.rowHint')}</span>}
        </span>
        <span className="sq-pager">
          <button type="button" className="btn btn-secondary btn-sm" disabled={data.page <= 1}
                  onClick={() => onApply({ page: Math.max(1, data.page - 1) })}>{t('sq.table.prev')}</button>
          <span>
            {pages
              ? t('sq.table.pageOf', { page: formatCount(data.page, lang), pages: formatCount(pages, lang) })
              : t('sq.explore.pageNumber', { page: data.page })}
          </span>
          <button type="button" className="btn btn-secondary btn-sm" disabled={!data.has_more}
                  onClick={() => onApply({ page: data.page + 1 })}>{t('sq.table.next')}</button>
        </span>
      </div>
      {data.sql && (
        <details className="raw">
          <summary>{t('sq.explore.sql.preview')}</summary>
          <p className="sq-muted">{t('sq.explore.sql.previewNote', { dialect: data.dialect })}</p>
          <SqlCode sql={data.sql} />
          <p className="sq-muted">{t('sq.explore.traceId')}: <span className="mono">{data.trace_id}</span></p>
        </details>
      )}
    </div>
  )
}
