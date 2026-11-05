/**
 * 数据浏览（#/data/explore[/库[/表]]）：不提问，先看看系统里有什么（移植自 SQL Agent 旧控制台的
 * views/explore.js、exploreTable.js、exploreParts.js）。
 *
 * 三层，从粗到细：
 * - 全部数据：有哪几个库、各有多少张表多少条记录、库与库之间靠什么连着；
 * - 一个库：它的每张表长什么样——多少条、状态怎么分布、记录落在哪段时间；
 * - 一张表：概览 + 记录 + 字段说明（explore/TablePage.tsx）。
 *
 * 左边的导航一直在：从任何一张表到另一个库的任何一张表都只要点一下。
 *
 * 目录来自 /catalog，统计来自 /catalog/{库}/{表}/profile——都是服务端按当前身份算出来的。
 * 这个文件里没有任何表名、字段名或样例数据：服务端没给的，这里画不出来。
 */
import { useEffect, useMemo, useRef } from 'react'
import { useSqlCatalog } from '@/api/sql/hooks'
import { useT } from '@/i18n'
import { findDomain, findTable, sortedDomains } from '@/lib/sql/catalog'
import { profileSupport, useTableProfiles, type TablePair } from '@/lib/sql/profile'
import { DatabasePage, OverviewPage } from './explore/CatalogPages'
import { ExploreNav } from './explore/Nav'
import { CatalogProblem, COUNT_UP_TO, RICH_CARDS_UP_TO, countable, indexProfiles } from './explore/parts'
import { TablePage } from './explore/TablePage'
import './explore/explore.css'

export function ExplorePage({ database, table, query }: { database: string; table: string; query: string }) {
  const { t } = useT()
  const catalogQuery = useSqlCatalog()
  const catalog = catalogQuery.data ?? null
  const domains = useMemo(() => sortedDomains(catalog), [catalog])
  const main = useRef<HTMLDivElement>(null)

  // 先把要的统计发出去：全部表各数一遍行数（太多就不数），正在看的库再要一份带分布的
  const on = profileSupport(catalog?.service) === 'on'
  const all = useMemo(() => domains.flatMap((domain) => domain.tables
    .filter((entry) => countable(domain, entry))
    .map((entry): TablePair => ({ database: domain.database, table: entry.name }))), [domains])
  const here = all.filter((pair) => pair.database === database)
  const countPairs = !on ? [] : all.length <= COUNT_UP_TO ? all : here.length <= COUNT_UP_TO ? here : []
  const domain = database ? findDomain(catalog, database) : null
  const richPairs = on && domain && !table && domain.tables.length <= RICH_CARDS_UP_TO ? here : []
  const counts = useTableProfiles(catalog, countPairs, { breakdowns: false })
  const rich = useTableProfiles(catalog, richPairs)
  const profiles = indexProfiles(
    rich.map((result, index) => ({ ...richPairs[index]!, result })),
    indexProfiles(counts.map((result, index) => ({ ...countPairs[index]!, result }))),
  )

  // 新的一页从头看起，焦点交给新页面的标题：读屏会念出到了哪里，Tab 也从这里往下走
  useEffect(() => {
    main.current?.closest('.scroll')?.scrollTo?.({ top: 0 })
    main.current?.querySelector<HTMLElement>('.page-title')?.focus({ preventScroll: true })
  }, [database, table])

  const refresh = () => { void catalogQuery.refetch() }
  const problem = catalog ? null : <CatalogProblem query={catalogQuery} onRetry={refresh} />

  let content
  if (!catalog) {
    content = (
      <div className="sq-stack">
        <h2 className="page-title" tabIndex={-1}>{t('sq.explore.title')}</h2>
        {problem}
      </div>
    )
  } else if (table) {
    content = (
      <TablePage key={`${database}\u0000${table}`} catalog={catalog} domain={domain}
                 entry={findTable(catalog, database, table)} database={database} tableName={table} query={query}
                 onRefresh={refresh} />
    )
  } else if (database) {
    content = <DatabasePage catalog={catalog} domain={domain} profiles={profiles} rich={richPairs.length > 0} onRefresh={refresh} />
  } else {
    content = (
      <OverviewPage catalog={catalog} domains={domains} profiles={profiles} onRefresh={refresh}
                    refreshing={catalogQuery.isFetching} />
    )
  }

  const side = !!catalog && domains.length > 0
  return (
    <div className="page scroll">
      <div className="sq-dx" data-side={side ? 'on' : 'off'}>
        {side && (
          <aside className="sq-dx-side">
            <ExploreNav catalog={catalog!} domains={domains} database={database} table={table} profiles={profiles} />
          </aside>
        )}
        <div className="sq-dx-main" ref={main}>{content}</div>
      </div>
    </div>
  )
}
