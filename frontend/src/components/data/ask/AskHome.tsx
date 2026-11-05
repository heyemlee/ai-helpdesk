/**
 * 问数页还没问过的时候：一个输入框，下面是"可以这样问""最近问过"——两样都来自服务端（目录、历史），
 * 页面自己不写任何一句业务问题（见 lib/sql/suggest.ts）。
 */
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import type { HistoryItem } from '@/api/sql/generated'
import type { Catalog } from '@/api/sql/types'
import { useT } from '@/i18n'
import { domainTitle, findTable, sortedDomains } from '@/lib/sql/catalog'
import { formatRelative, labelOf } from '@/lib/sql/format'
import { historyStatus } from '@/lib/sql/model'
import { buildSuggestions, pageCount, pageOf } from '@/lib/sql/suggest'
import { href } from '@/lib/route'
import { DbTile, databaseSlot } from '../DbTile'
import { HistoryStatusChip } from '../shared'

const RECENT_MAX = 4
const RELATIVE_REFRESH_MS = 60_000     // "5 分钟前"这类字样多久重算一次

/** 最近问过的几个问题（同一句话只留最近的一次） */
export function recentItems(items: HistoryItem[] | undefined): HistoryItem[] {
  const seen = new Set<string>()
  const out: HistoryItem[] = []
  for (const item of items ?? []) {
    const text = String(item.query ?? '').trim()
    if (!text || seen.has(text)) continue
    seen.add(text)
    out.push(item)
    if (out.length === RECENT_MAX) break
  }
  return out
}

/**
 * 可以这样问。上面一排是能问的业务域（点一个只看它的问法），下面是拼出来的问题。目录还没到时先占着
 * 位置，到了再换成真的——免得下面的内容跳一下。点一个只把那句话放进输入框，问什么、怎么改由人定。
 */
function Suggestions({ catalog, loading, onPick }: {
  catalog: Catalog | null | undefined
  loading: boolean
  onPick: (text: string) => void
}) {
  const { t, lang } = useT()
  const [scope, setScope] = useState('')
  const [page, setPage] = useState(0)
  const [announce, setAnnounce] = useState('')
  const domains = useMemo(() => sortedDomains(catalog), [catalog])
  const everything = useMemo(() => buildSuggestions(domains, lang, t), [domains, lang, t])
  const available = domains.filter((domain) => domain.status === 'available')
  // 换了目录之后，之前选的那个库可能已经不在了
  const activeScope = scope && available.some((domain) => domain.database === scope) ? scope : ''
  const list = activeScope ? everything.filter((item) => item.database === activeScope) : everything

  if (loading) {
    return (
      <div className="sq-ask-section" aria-hidden="true">
        <div className="sq-scope-row"><span className="sq-scope-chip is-ghost" /><span className="sq-scope-chip is-ghost" /></div>
        <div className="sq-suggest-grid">{[0, 1, 2, 3].map((index) => <span key={index} className="sq-suggest is-ghost" />)}</div>
      </div>
    )
  }
  if (!everything.length) {
    return <p className="sq-ask-fallback">{t('sq.ask.noSuggest')} <a href={href.dataExplore()}>{t('sq.ask.browseData')}</a></p>
  }
  const pages = pageCount(list)
  return (
    <section className="sq-ask-section" aria-labelledby="sqSuggestTitle">
      <div className="sq-ask-section-head">
        <h3 className="sq-ask-section-title" id="sqSuggestTitle">{t('sq.ask.suggestTitle')}</h3>
        {pages > 1 && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => {
            const next = (page + 1) % pages
            setPage(next)
            setAnnounce(t('sq.ask.suggestPage', { page: next + 1, pages }))
          }}>{t('sq.ask.shuffle')}</button>
        )}
        <span className="sr-only" role="status">{announce}</span>
      </div>
      {available.length > 1 && (
        <div className="sq-scope-row" role="group" aria-label={t('sq.ask.scope')}>
          <button type="button" className="sq-scope-chip" aria-pressed={!activeScope}
                  onClick={() => { setScope(''); setPage(0) }}>{t('sq.ask.scopeAll')}</button>
          {available.map((domain) => (
            <button key={domain.database} type="button" className="sq-scope-chip" aria-pressed={activeScope === domain.database}
                    onClick={() => { setScope(domain.database); setPage(0) }}>
              <DbTile catalog={catalog} database={domain.database} size="is-xs" />
              <span>{domainTitle(domain, lang)}</span>
            </button>
          ))}
        </div>
      )}
      {list.length ? (
        <div className="sq-suggest-grid">
          {pageOf(list, page).map((item) => {
            const table = findTable(catalog, item.database, item.table)
            return (
              // 填进去的就是卡片上写着的那句话
              <button key={item.id} type="button" className={`sq-suggest db-c${databaseSlot(catalog, item.database)}`}
                      onClick={() => onPick(item.text)}>
                <span className="sq-suggest-src">
                  <DbTile catalog={catalog} database={item.database} size="is-xs" />
                  <span>{table ? labelOf(table, lang) : item.table}</span>
                </span>
                <span className="sq-suggest-text">{item.text}</span>
              </button>
            )
          })}
        </div>
      ) : <p className="sq-ask-empty">{t('sq.ask.scopeEmpty')}</p>}
      <p className="sq-ask-note">
        {t('sq.ask.suggestNote')}{' '}
        <a href={activeScope ? href.dataExplore(activeScope) : href.dataExplore()}>{t('sq.ask.browseData')}</a>
      </p>
    </section>
  )
}

/**
 * 最近问过。认的是"这个身份的历史取成过"，不是"此刻状态正常"：之后某一次刷新失败了，手里那份照样是
 * 真的，不让整段跟着消失。换了身份时这份跟着换（查询的 key 里带着身份）。
 */
function Recent({ items, onPick }: { items: HistoryItem[]; onPick: (text: string) => void }) {
  const { t, lang } = useT()
  // 页面一直开着的话，"刚刚"不能一直是"刚刚"
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), RELATIVE_REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [])
  if (!items.length) return null
  return (
    <section className="sq-ask-section" aria-labelledby="sqRecentTitle">
      <div className="sq-ask-section-head">
        <h3 className="sq-ask-section-title" id="sqRecentTitle">{t('sq.ask.recentTitle')}</h3>
        <a className="btn btn-ghost btn-sm" href={href.dataHistory()}>{t('sq.ask.recentAll')}</a>
      </div>
      <ul className="sq-recent-list">
        {items.map((item) => (
          <li key={item.trace_id}>
            <button type="button" className="sq-recent-item" onClick={() => onPick(String(item.query).trim())}>
              <span className="sq-recent-q">{item.query}</span>
              <span className="sq-recent-meta">
                <HistoryStatusChip status={historyStatus(item)} />
                <span className="sq-recent-time">{formatRelative(item.created_at, lang, now)}</span>
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}

export function AskHome({ catalog, catalogLoading, recent, composer, offline, onPick }: {
  catalog: Catalog | null | undefined
  catalogLoading: boolean
  recent: HistoryItem[]
  composer: ReactNode
  /** 服务连不上时在输入框下面说一声：这时候发问只会等来一条"请求没成功" */
  offline: ReactNode
  onPick: (text: string) => void
}) {
  const { t } = useT()
  return (
    <div className="sq-ask-home">
      <div className="sq-ask-hero">
        <h2 className="sq-ask-title">{t('sq.ask.title')}</h2>
        <p className="sq-ask-lead">{t('sq.ask.lead')}</p>
      </div>
      {composer}
      {offline}
      <Suggestions catalog={catalog} loading={catalogLoading} onPick={onPick} />
      <Recent items={recent} onPick={onPick} />
    </div>
  )
}
