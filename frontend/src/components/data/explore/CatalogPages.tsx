/**
 * 数据浏览的上两层（移植自旧控制台 explore.js）：
 * - 全部数据（#/data/explore）：有哪几个库、各有多少张表多少条记录、库与库之间靠什么连着；
 * - 一个库（#/data/explore/<库>）：它的每张表长什么样——多少条、状态怎么分布、记录落在哪段时间。
 *
 * 目录来自 /catalog，统计来自 /catalog/{库}/{表}/profile——都是服务端按当前身份算出来的。
 */
import type { Catalog, CatalogDomain, CatalogTable, TableProfile } from '@/api/sql/types'
import { useT } from '@/i18n'
import { domainTitle } from '@/lib/sql/catalog'
import { rebucket, seriesSlots } from '@/lib/sql/charts'
import { exploreHref } from '@/lib/sql/dataView'
import { formatCompact, formatCount, formatRange, formatTime, labelOf, localized } from '@/lib/sql/format'
import { buildLinks, crossDatabaseLinks, type Links } from '@/lib/sql/links'
import { setAskDraft } from '@/lib/sql/askDraft'
import { profileSupport, statsOf } from '@/lib/sql/profile'
import { href } from '@/lib/route'
import { Icon } from '../../Icon'
import { Chip } from '../../ui'
import { DbTile, databaseSlot } from '../DbTile'
import { BarRow, ColumnChart, StackedBar, ValueList, type VizItem } from './charts'
import {
  BackToCatalog, CountChip, DomainSummary, Kpi, Notice, TableSummary, UnavailableNotice, countable, fieldCount,
  recordCountText, rowCount, sumCounts, type ProfileIndex,
} from './parts'

/** 记录数这一格。还没数完时先给占位；有表没数出来时照实说"至少" */
function RecordKpi({ catalog, profiles, pairs }: {
  catalog: Catalog; profiles: ProfileIndex; pairs: Array<{ domain: CatalogDomain; table: CatalogTable }>
}) {
  const { t, lang } = useT()
  if (profileSupport(catalog.service) !== 'on') return null
  const counts = sumCounts(profiles, pairs)
  // 表太多、没有逐张去数的时候不给总数：给一个只加了一部分的数，比不给更糟
  if (!counts.expected || counts.unasked) return null
  if (counts.known + counts.failed < counts.expected) {
    return <Kpi label={t('sq.explore.kpi.records')} value="···" sub={t('sq.explore.kpi.counting')} />
  }
  const short = formatCompact(counts.total, lang)
  const full = formatCount(counts.total, lang)
  // 大数写短了才在下面补一行完整的；本来就没缩写时不重复一遍
  const sub = counts.failed
    ? t('sq.explore.kpi.partial', { n: counts.failed })
    : short === full ? undefined : t('sq.explore.kpi.exact', { n: full })
  return <Kpi label={t('sq.explore.kpi.records')} value={(counts.failed ? '≥ ' : '') + short} sub={sub} title={full} />
}

function TableRows({ domain, profiles, peak }: { domain: CatalogDomain; profiles: ProfileIndex; peak: number }) {
  const { t, lang } = useT()
  return (
    <ul className="sq-tbl-rows">
      {domain.tables.map((table) => (
        <li key={table.name}>
          <a className="sq-tbl-row" href={exploreHref(domain.database, table.name)}>
            <span className="sq-tbl-name">{labelOf(table, lang)} <span className="sq-tech">{table.name}</span></span>
            {table.status === 'missing'
              ? <Chip tone="caution">{t('sq.explore.missing')}</Chip>
              : <>
                <BarRow count={rowCount(profiles, domain.database, table.name) ?? 0} peak={peak} />
                <CountChip index={profiles} database={domain.database} table={table.name} />
              </>}
          </a>
        </li>
      ))}
    </ul>
  )
}

function DatabaseCard({ catalog, domain, profiles, peak }: {
  catalog: Catalog; domain: CatalogDomain; profiles: ProfileIndex; peak: number
}) {
  const { t, lang } = useT()
  const unavailable = domain.status !== 'available'
  const counts = sumCounts(profiles, domain.tables.map((table) => ({ domain, table })))
  const facts = [t('sq.explore.tableCount', { n: domain.tables.length })]
  if (profileSupport(catalog.service) === 'on' && counts.expected && counts.known === counts.expected) {
    facts.push(recordCountText(counts.total, t, lang))
  }
  const titleId = `sq-db-${domain.database}`
  return (
    <section className={`sq-db-card db-c${databaseSlot(catalog, domain.database)}`} aria-labelledby={titleId}>
      <header className="sq-db-card-head">
        <DbTile catalog={catalog} database={domain.database} size="is-lg" />
        <div className="sq-grow">
          <h3 className="sq-db-card-title" id={titleId}>
            <a href={exploreHref(domain.database)}>{domainTitle(domain, lang)}</a>
          </h3>
          <p className="sq-tech">{domain.database}{unavailable ? '' : ` · ${facts.join(' · ')}`}</p>
        </div>
        {unavailable && <Chip tone="danger">{t('sq.explore.unavailable')}</Chip>}
      </header>
      <p className="sq-db-card-desc"><DomainSummary domain={domain} /></p>
      {unavailable
        ? <UnavailableNotice domain={domain} />
        : domain.tables.length
          ? <TableRows domain={domain} profiles={profiles} peak={peak} />
          : <p className="sq-muted">{t('sq.explore.noTables')}</p>}
    </section>
  )
}

/** 库与库靠什么连着：一行一个"共用的键"，一列一个库，格子里是用到这个键的表 */
function KeyMatrix({ catalog, links, domains }: { catalog: Catalog; links: Links; domains: CatalogDomain[] }) {
  const { t, lang } = useT()
  if (!links.groups.length) return null
  return (
    <section className="card" aria-labelledby="sq-links-title">
      <div className="card-body sq-stack-sm">
        <div>
          <h3 className="sq-section-title" id="sq-links-title">{t('sq.explore.links.title')}</h3>
          <p className="sq-muted">{t('sq.explore.links.lead')}</p>
        </div>
        <div className="sq-matrix-wrap">
          <table className="sq-key-matrix">
            <thead>
              <tr>
                <th scope="col">{t('sq.explore.links.key')}</th>
                {domains.map((domain) => (
                  <th scope="col" key={domain.database}>
                    <a className="sq-matrix-db" href={exploreHref(domain.database)}>
                      <DbTile catalog={catalog} database={domain.database} size="is-xs" />{domainTitle(domain, lang)}
                    </a>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {links.groups.map((group) => {
                const hubLabel = labelOf(group.hub.columnEntry, lang)
                return (
                  <tr key={group.id}>
                    <th scope="row">
                      <span className="sq-key-name">{hubLabel}</span>{' '}
                      <span className="sq-tech">{group.hub.column}</span>
                      <span className="sq-muted sq-key-span">
                        {group.databases.length > 1
                          ? t('sq.explore.links.span', { tables: group.members.length, databases: group.databases.length })
                          : t('sq.explore.links.spanSame', { tables: group.members.length })}
                      </span>
                    </th>
                    {domains.map((domain) => (
                      <td key={domain.database}>
                        {group.members.filter((member) => member.database === domain.database).map((member) => {
                          const own = labelOf(member.columnEntry, lang)
                          return (
                            <a key={`${member.table}.${member.column}`} className="sq-chip-link"
                               href={exploreHref(member.database, member.table)}>
                              {labelOf(member.tableEntry, lang)}
                              {own !== hubLabel && <span className="sq-muted"> · {own}</span>}
                            </a>
                          )
                        })}
                      </td>
                    ))}
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  )
}

function Glossary({ catalog }: { catalog: Catalog }) {
  const { t, lang } = useT()
  if (!catalog.glossary?.length) return null
  return (
    <details className="raw sq-glossary">
      <summary>{t('sq.explore.glossary', { n: catalog.glossary.length })}</summary>
      <p className="sq-muted">{t('sq.explore.glossaryNote')}</p>
      <dl>
        {catalog.glossary.map((item, index) => {
          const term = localized(item, 'term', lang)
          const definition = localized(item, 'definition', lang)
          return (
            <div key={index}>
              <dt lang={term.original ? 'zh' : undefined}>{term.text}</dt>
              <dd lang={definition.original ? 'zh' : undefined} className={definition.original ? 'sq-original' : undefined}
                  title={definition.original ? t('sq.explore.originalNote') : undefined}>{definition.text}</dd>
            </div>
          )
        })}
      </dl>
    </details>
  )
}

export function OverviewPage({ catalog, domains, profiles, onRefresh, refreshing }: {
  catalog: Catalog
  domains: CatalogDomain[]
  profiles: ProfileIndex
  onRefresh: () => void
  refreshing: boolean
}) {
  const { t, lang } = useT()
  const pairs = domains.flatMap((domain) => domain.tables.map((table) => ({ domain, table })))
  const fields = pairs.reduce((sum, { table }) => sum + fieldCount(table), 0)
  // 所有条形共用一把尺：最长的那条是全部表里记录最多的一张
  const peak = Math.max(0, ...pairs.map(({ domain, table }) => rowCount(profiles, domain.database, table.name) ?? 0))
  return (
    <div className="sq-stack">
      <div className="page-heading">
        <div>
          <h2 className="page-title" tabIndex={-1}>{t('sq.explore.title')}</h2>
          <p className="page-lede">{t('sq.explore.lead')}</p>
        </div>
        <div className="page-heading-actions">
          <span className="sq-muted">{t('sq.explore.readAt', { time: formatTime(catalog.generated_at, lang) })}</span>
          <button type="button" className="btn btn-secondary" onClick={onRefresh} disabled={refreshing}>
            <Icon name="refresh" size={14} />{t('sq.action.refresh')}
          </button>
        </div>
      </div>
      {catalog.access_limited && (
        <Notice title={t('sq.explore.limited.title')}><p>{t('sq.explore.limited.body')}</p></Notice>
      )}
      {domains.length ? (
        <>
          <div className="sq-kpis">
            <Kpi label={t('sq.explore.kpi.databases')} value={formatCount(domains.length, lang)} />
            <Kpi label={t('sq.explore.kpi.tables')} value={formatCount(pairs.length, lang)} />
            <Kpi label={t('sq.explore.kpi.fields')} value={formatCount(fields, lang)} />
            <RecordKpi catalog={catalog} profiles={profiles} pairs={pairs} />
          </div>
          <div className="sq-db-grid">
            {domains.map((domain) => (
              <DatabaseCard key={domain.database} catalog={catalog} domain={domain} profiles={profiles} peak={peak} />
            ))}
          </div>
          <KeyMatrix catalog={catalog} links={buildLinks(catalog.domains)} domains={domains} />
        </>
      ) : (
        <Notice title={t('sq.explore.empty.title')}><p>{t('sq.explore.empty.body')}</p></Notice>
      )}
      <Glossary catalog={catalog} />
    </div>
  )
}

/** 表卡片上的那张小图：先找声明过取值的分布（状态、类型），没有就找最常见的几个取值。只看不点 */
function Headline({ profile, nameOf }: { profile: TableProfile; nameOf: (column: string) => string }) {
  const rows = profile.total_rows || 0
  const live = profile.facets.filter((facet) => facet.values.some((item) => item.count > 0))
  const series = live.find((facet) => seriesSlots(facet).size > 0)
  if (series) {
    const slots = seriesSlots(series)
    const items: VizItem[] = series.values.map((item) => ({
      value: item.value, text: String(item.value), count: item.count, slot: slots.get(String(item.value)) ?? 0,
    }))
    const total = items.reduce((sum, item) => sum + item.count, 0)
    return (
      <div className="sq-tbl-card-viz">
        <p className="sq-viz-caption">{nameOf(series.column)}</p>
        <StackedBar items={items} label={nameOf(series.column)} />
        <ValueList items={items} total={total} />
      </div>
    )
  }
  const most = (values: Array<{ count: number }>) => Math.max(...values.map((item) => item.count))
  const top = live.find((facet) => facet.complete || most(facet.values) >= Math.max(2, rows * 0.005))
  if (!top) return null
  const items: VizItem[] = [...top.values].sort((a, b) => b.count - a.count).slice(0, 4)
    .map((item) => ({ value: item.value, text: String(item.value), count: item.count, slot: 0 }))
  return (
    <div className="sq-tbl-card-viz">
      <p className="sq-viz-caption">{nameOf(top.column)}</p>
      <ValueList items={items} total={rows} bars />
    </div>
  )
}

function TableCard({ domain, table, profiles, rich }: {
  domain: CatalogDomain; table: CatalogTable; profiles: ProfileIndex; rich: boolean
}) {
  const { t, lang } = useT()
  const entry = profiles.get(`${domain.database}\u0000${table.name}`)
  const profile = entry?.status === 'success' ? entry.profile : undefined
  const labels = new Map((table.columns ?? []).map((column) => [column.name, labelOf(column, lang)]))
  const nameOf = (name: string) => labels.get(name) ?? name
  const line = profile && rich ? profile.timelines.find((item) => item.buckets.length) : undefined
  const span = profile && line ? statsOf(profile).find((column) => column.name === line.column && column.min !== undefined) : undefined
  return (
    <article className="sq-tbl-card">
      <header className="sq-tbl-card-head">
        <div className="sq-grow">
          <h3 className="sq-tbl-card-title">
            <a className="sq-stretched" href={exploreHref(domain.database, table.name)}>{labelOf(table, lang)}</a>
          </h3>
          <p className="sq-tech">{table.name} · {t('sq.explore.fieldCount', { n: fieldCount(table) })}</p>
        </div>
        {table.status === 'missing'
          ? <Chip tone="caution">{t('sq.explore.missing')}</Chip>
          : profile && typeof profile.total_rows === 'number'
            ? (
              <p className="sq-tbl-card-count" title={formatCount(profile.total_rows, lang)}>
                {formatCompact(profile.total_rows, lang)}<span>{t('sq.explore.recordsUnit')}</span>
              </p>
            )
            : entry?.status === 'pending' ? <span className="pulse" aria-hidden="true" /> : null}
      </header>
      <p className="sq-tbl-card-desc"><TableSummary table={table} /></p>
      {profile && rich && <Headline profile={profile} nameOf={nameOf} />}
      {line && (
        <div className="sq-tbl-card-viz">
          <p className="sq-viz-caption">
            {nameOf(line.column)}
            {span && (
              <span className="sq-muted"> · {formatRange({ min: String(span.min).slice(0, 10), max: String(span.max).slice(0, 10) })}</span>
            )}
          </p>
          <ColumnChart bars={rebucket(line.buckets).bars}
                       label={t('sq.explore.viz.timelineLabel', { column: nameOf(line.column) })} />
        </div>
      )}
    </article>
  )
}

function DatabaseLinks({ catalog, domain }: { catalog: Catalog; domain: CatalogDomain }) {
  const { t, lang } = useT()
  const groups = crossDatabaseLinks(buildLinks(catalog.domains), domain.database)
  if (!groups.length) return null
  return (
    <section className="card" aria-labelledby="sq-db-links-title">
      <div className="card-body sq-stack-sm">
        <div>
          <h3 className="sq-section-title" id="sq-db-links-title">{t('sq.explore.links.fromHere')}</h3>
          <p className="sq-muted">{t('sq.explore.links.fromHereLead')}</p>
        </div>
        <ul className="sq-link-list">
          {groups.map((group) => (
            <li key={group.domain.database}>
              <a className="sq-link-db" href={exploreHref(group.domain.database)}>
                <DbTile catalog={catalog} database={group.domain.database} size="is-sm" />{domainTitle(group.domain, lang)}
              </a>
              <ul>
                {group.links.map(({ mine, other }) => (
                  <li key={`${mine.table}.${mine.column}>${other.table}.${other.column}`}>
                    <a href={exploreHref(mine.database, mine.table)}>{labelOf(mine.tableEntry, lang)}</a>
                    <span className="sq-muted">{labelOf(mine.columnEntry, lang)}</span>
                    <span className="sq-link-arrow" aria-hidden="true">↔</span>
                    <span className="sr-only">{t('sq.explore.links.matches')}</span>
                    <a href={exploreHref(other.database, other.table)}>{labelOf(other.tableEntry, lang)}</a>
                    <span className="sq-muted">{labelOf(other.columnEntry, lang)}</span>
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}

/** 带去问数页的话头：问数页把它填进输入框，接着打问题 */
export function askAbout(name: string, t: (key: 'sq.explore.askPrefill', vars: { table: string }) => string) {
  setAskDraft({ question: t('sq.explore.askPrefill', { table: name }), send: false })
  window.location.hash = href.dataAsk()
}

export function DatabasePage({ catalog, domain, profiles, rich, onRefresh }: {
  catalog: Catalog
  domain: CatalogDomain | null
  profiles: ProfileIndex
  rich: boolean
  onRefresh: () => void
}) {
  const { t, lang } = useT()
  if (!domain) {
    return (
      <div className="sq-stack">
        <nav className="sq-breadcrumbs" aria-label={t('sq.explore.breadcrumb')}><a href={exploreHref()}>{t('sq.explore.title')}</a></nav>
        <h2 className="page-title" tabIndex={-1}>{t('sq.explore.dbNotFound.title')}</h2>
        <Notice title={t('sq.explore.dbNotFound.title')}><p>{t('sq.explore.dbNotFound.body')}</p></Notice>
        <div><BackToCatalog href={exploreHref()} /></div>
      </div>
    )
  }
  const unavailable = domain.status !== 'available'
  const pairs = domain.tables.map((table) => ({ domain, table }))
  const fields = pairs.reduce((sum, { table }) => sum + fieldCount(table), 0)
  const linked = crossDatabaseLinks(buildLinks(catalog.domains), domain.database).length
  return (
    <div className="sq-stack">
      <div>
        <nav className="sq-breadcrumbs" aria-label={t('sq.explore.breadcrumb')}>
          <a href={exploreHref()}>{t('sq.explore.title')}</a><span aria-hidden="true">›</span><span>{domainTitle(domain, lang)}</span>
        </nav>
        <div className="page-heading">
          <div className="sq-dx-title">
            <DbTile catalog={catalog} database={domain.database} size="is-lg" />
            <div>
              <h2 className="page-title" tabIndex={-1}>{domainTitle(domain, lang)}</h2>
              <p className="sq-tech">{domain.database} · {domain.dialect} · {t('sq.explore.readOnly')}</p>
            </div>
          </div>
          <div className="page-heading-actions">
            <button type="button" className="btn btn-secondary" onClick={() => askAbout(domainTitle(domain, lang), t)}>
              <Icon name="spark" size={14} />{t('sq.explore.askAbout')}
            </button>
          </div>
        </div>
        <p className="page-lede"><DomainSummary domain={domain} /></p>
      </div>
      {unavailable ? (
        <>
          <UnavailableNotice domain={domain} />
          <div>
            <button type="button" className="btn btn-secondary" onClick={onRefresh}>
              <Icon name="refresh" size={14} />{t('sq.action.retry')}
            </button>
          </div>
        </>
      ) : (
        <>
          <div className="sq-kpis">
            <Kpi label={t('sq.explore.kpi.tables')} value={formatCount(domain.tables.length, lang)} />
            <Kpi label={t('sq.explore.kpi.fields')} value={formatCount(fields, lang)} />
            <RecordKpi catalog={catalog} profiles={profiles} pairs={pairs} />
            <Kpi label={t('sq.explore.kpi.linked')} value={formatCount(linked, lang)} />
          </div>
          {domain.tables.length
            ? (
              <div className="sq-tbl-grid">
                {domain.tables.map((table) => (
                  <TableCard key={table.name} domain={domain} table={table} profiles={profiles}
                             rich={rich && countable(domain, table)} />
                ))}
              </div>
            )
            : <p className="sq-muted">{t('sq.explore.noTables')}</p>}
          <DatabaseLinks catalog={catalog} domain={domain} />
        </>
      )}
    </div>
  )
}
