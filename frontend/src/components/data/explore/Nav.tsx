/**
 * 数据浏览左边的导航：库 → 表，带一个查找框（移植自旧控制台 explore.js 的 tree）。
 * 从任何一张表到另一个库的任何一张表都只要点一下。
 */
import { useEffect, useState } from 'react'
import type { Catalog, CatalogDomain } from '@/api/sql/types'
import { useT } from '@/i18n'
import { domainTitle } from '@/lib/sql/catalog'
import { exploreHref } from '@/lib/sql/dataView'
import { labelOf } from '@/lib/sql/format'
import { Icon } from '../../Icon'
import { Chip } from '../../ui'
import { DbTile } from '../DbTile'
import { CountChip, type ProfileIndex } from './parts'

const matches = (term: string, texts: Array<string | undefined>) =>
  texts.some((text) => String(text ?? '').toLowerCase().includes(term))

export function ExploreNav({ catalog, domains, database, table, profiles }: {
  catalog: Catalog
  domains: CatalogDomain[]
  database: string
  table: string
  profiles: ProfileIndex
}) {
  const { t, lang } = useT()
  const [needle, setNeedle] = useState('')
  const [open, setOpen] = useState(false)
  // 窄屏上点了一个库或一张表：导航收起来，让出位置给内容
  useEffect(() => { setOpen(false) }, [database, table])
  const term = needle.trim().toLowerCase()

  const groups = domains.map((domain) => {
    const domainHit = !term || matches(term, [domainTitle(domain, lang), domain.database, ...(domain.aliases ?? [])])
    const tables = domain.tables.map((entry) => {
      const tableHit = !term || matches(term, [labelOf(entry, lang), entry.name, ...(entry.aliases ?? [])])
      // 表名对不上时再看字段：找"手机号在哪张表"的人打的是字段名
      const column = tableHit ? null : (entry.columns ?? [])
        .find((item) => matches(term, [labelOf(item, lang), item.name, ...(item.aliases ?? [])])) ?? null
      return { entry, column, show: domainHit || tableHit || !!column }
    }).filter((item) => item.show)
    return { domain, tables, show: domainHit || tables.length > 0 }
  }).filter((group) => group.show)

  return (
    <>
      <button type="button" className="sq-dx-side-toggle btn btn-secondary" aria-expanded={open}
              onClick={() => setOpen((value) => !value)}>
        <Icon name="grid" size={14} /><span>{t('sq.explore.nav.browse')}</span>
      </button>
      <div className="sq-dx-side-body" data-open={open}>
        <label className="sq-dx-find">
          <Icon name="search" size={14} />
          <span className="sr-only">{t('sq.explore.nav.find')}</span>
          <input className="field" type="search" maxLength={60} value={needle} autoComplete="off"
                 placeholder={t('sq.explore.nav.find')} onChange={(event) => setNeedle(event.target.value)} />
        </label>
        <a className="sq-dx-home" href={exploreHref()} aria-current={!database ? 'page' : undefined}>
          {t('sq.explore.nav.all')}
        </a>
        <nav aria-label={t('sq.explore.nav.label')}>
          {groups.length ? (
            <ul className="sq-dx-tree">
              {groups.map(({ domain, tables }) => {
                const here = database === domain.database
                return (
                  <li key={domain.database}>
                    <a className="sq-dx-db-link" href={exploreHref(domain.database)}
                       aria-current={here && !table ? 'page' : undefined}>
                      <DbTile catalog={catalog} database={domain.database} size="is-sm" />
                      <span className="sq-dx-nav-name">{domainTitle(domain, lang)}</span>
                      {domain.status !== 'available' && <Chip tone="danger">{t('sq.explore.unavailable')}</Chip>}
                    </a>
                    {tables.length > 0 && (
                      <ul>
                        {tables.map(({ entry, column }) => (
                          <li key={entry.name}>
                            <a className="sq-dx-table-link" href={exploreHref(domain.database, entry.name)}
                               aria-current={here && table === entry.name ? 'page' : undefined}>
                              <span className="sq-dx-nav-name">
                                {labelOf(entry, lang)}
                                {column && (
                                  <span className="sq-dx-nav-hint">
                                    {t('sq.explore.nav.hasField', { name: labelOf(column, lang) })}
                                  </span>
                                )}
                              </span>
                              {entry.status === 'missing'
                                ? <Chip tone="caution">{t('sq.explore.missing')}</Chip>
                                : <CountChip index={profiles} database={domain.database} table={entry.name} />}
                            </a>
                          </li>
                        ))}
                      </ul>
                    )}
                  </li>
                )
              })}
            </ul>
          ) : <p className="sq-muted sq-dx-nav-empty">{t('sq.explore.nav.noMatch')}</p>}
        </nav>
      </div>
    </>
  )
}
