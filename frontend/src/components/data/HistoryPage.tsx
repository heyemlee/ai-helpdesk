import { useMemo, useState } from 'react'
import type { HistoryItem } from '@/api/sql/generated'
import { useSqlCatalog, useSqlHistory } from '@/api/sql/hooks'
import { useT } from '@/i18n'
import type { Key } from '@/i18n/catalog'
import { setAskDraft } from '@/lib/sql/askDraft'
import { sourceLabel } from '@/lib/sql/catalog'
import { formatDateTime, formatDuration } from '@/lib/sql/format'
import { historyStatus, type HistoryStatus } from '@/lib/sql/model'
import { href } from '@/lib/route'
import { Icon } from '../Icon'
import { StateBlock } from '../ui'
import { HistoryStatusChip, ServerText, SqlCode, SqlErrorBlock } from './shared'

/**
 * 员工实例调 SQL Agent 时用的身份：backend 的 SQL_AGENT_USER_ID（默认 ai-helpdesk），
 * 也是 deploy/secrets/sql_agent_keys.json 里那把 Key 绑定的 user_id。控制台的 Key 是管理员，
 * 能看它的历史——这就是"客服助手在对话里替客户查过什么"。
 */
export const ASSISTANT_USER_ID = 'ai-helpdesk'

const FILTERS = ['all', 'success', 'no_data', 'review', 'failed', 'other'] as const
type Filter = (typeof FILTERS)[number]

function matchesFilter(filter: Filter, status: HistoryStatus): boolean {
  if (filter === 'all') return true
  // "返回了数据"只收确实知道有数据的。老记录（只知道跑完了）、降级结果、对话回答归到"其他"——
  // 把"不知道"算成"有数据"，筛出来的就不是它说的那一类了
  if (filter === 'review') return status === 'review' || status === 'partial'
  if (filter === 'other') return status === 'completed' || status === 'degraded' || status === 'conversation'
  return status === filter
}

/** 一条记录里当时真正执行过的 SQL（审计里的 databases 是检索考虑过的候选，不是一回事） */
const executedSqls = (item: HistoryItem) =>
  (item.sqls ?? []).filter((row) => row && (row.sql || row.final_sql))

export function HistoryPage() {
  const { t, lang } = useT()
  const [whose, setWhose] = useState<'self' | 'assistant'>('self')
  const history = useSqlHistory(whose === 'assistant' ? ASSISTANT_USER_ID : '')
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const [selected, setSelected] = useState('')

  const items = useMemo(() => history.data?.items ?? [], [history.data])
  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase()
    return items
      .map((item) => ({ item, status: historyStatus(item) }))
      .filter(({ item, status }) => matchesFilter(filter, status)
        && (!needle || [item.query, item.trace_id, ...(item.databases ?? [])].join(' ').toLowerCase().includes(needle)))
  }, [items, search, filter])
  const entry = items.find((item) => item.trace_id === selected) ?? null

  return (
    <div className="page scroll"><div className="page-inner">
      <div className="page-heading">
        <div>
          <h2 className="page-title">{t('sq.history.title')}</h2>
          <p className="page-lede">{t('sq.history.lede')}</p>
        </div>
        <div className="page-heading-actions">
          <button type="button" className="btn btn-secondary" onClick={() => void history.refetch()}
                  disabled={history.isFetching}>
            <Icon name="refresh" size={14} />{t('sq.action.refresh')}
          </button>
        </div>
      </div>

      <div className="sq-filters">
        <label className="field-row">
          <span>{t('sq.history.whose')}</span>
          <select className="field field-compact" value={whose} onChange={(e) => {
            setWhose(e.target.value as 'self' | 'assistant')
            setSelected('')
          }}>
            <option value="self">{t('sq.history.whose.self')}</option>
            <option value="assistant">{t('sq.history.whose.assistant')}</option>
          </select>
        </label>
        <label className="field-row sq-grow">
          <span>{t('sq.history.search')}</span>
          <input className="field" type="search" value={search} autoComplete="off"
                 placeholder={t('sq.history.search.ph')} onChange={(e) => setSearch(e.target.value)} />
        </label>
        <label className="field-row">
          <span>{t('sq.history.filter')}</span>
          <select className="field field-compact" value={filter} onChange={(e) => setFilter(e.target.value as Filter)}>
            {FILTERS.map((id) => <option key={id} value={id}>{t(`sq.history.filter.${id}` as Key)}</option>)}
          </select>
        </label>
      </div>
      {whose === 'assistant' && <p className="field-hint">{t('sq.history.whose.hint')}</p>}

      {history.isLoading && <StateBlock busy title={t('sq.history.loading')} />}
      {history.isError && (
        <SqlErrorBlock error={history.error} title={t('sq.history.loadFailed')} onRetry={() => void history.refetch()} />
      )}
      {history.isSuccess && !items.length && (
        <StateBlock title={t('sq.history.empty')}
          action={whose === 'self' && <a className="btn btn-secondary" href={href.dataAsk()}>{t('sq.history.empty.action')}</a>} />
      )}
      {history.isSuccess && items.length > 0 && (
        <div className="sq-split">
          <div>
            <p className="list-summary" role="status">{t('sq.history.count', { n: visible.length, total: items.length })}</p>
            {visible.length ? (
              <ul className="sq-history-list">
                {visible.map(({ item, status }) => (
                  <li key={item.trace_id}>
                    <button type="button" className="sq-history-item" aria-current={item.trace_id === selected}
                            onClick={() => setSelected(item.trace_id)}>
                      <span className="sq-history-q">{item.query}</span>
                      <span className="sq-history-meta">
                        <span>{formatDateTime(item.created_at, lang)}</span>
                        <HistoryStatusChip status={status} />
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : <p className="empty">{t('sq.history.noMatch')}</p>}
          </div>
          <div className="sq-detail">
            {entry ? <HistoryDetail key={entry.trace_id} entry={entry} /> : <p className="empty">{t('sq.history.pick')}</p>}
          </div>
        </div>
      )}
    </div></div>
  )
}

function HistoryDetail({ entry }: { entry: HistoryItem }) {
  const { t, lang } = useT()
  const catalog = useSqlCatalog().data
  const status = historyStatus(entry)
  const sqls = executedSqls(entry)
  const asked = formatDateTime(entry.created_at, lang)
  const queried = [...new Set(sqls.map((row) => row.database).filter(Boolean))] as string[]

  const askAgain = () => {
    setAskDraft({ question: entry.query, note: t('sq.history.rerunNote', { time: asked }) })
    window.location.hash = href.dataAsk()
  }

  return (
    <div className="sq-stack">
      <div className="sq-stack-sm">
        <h3 className="sq-question" tabIndex={-1}>{entry.query}</h3>
        <div className="chip-row"><HistoryStatusChip status={status} /></div>
        <p className="field-hint">{t(`sq.hstatus.help.${status}` as Key)}</p>
        <dl className="sq-kv">
          <div className="kv"><dt>{t('sq.history.askedAt')}</dt><dd>{asked}</dd></div>
          {queried.length > 0 && (
            <div className="kv"><dt>{t('sq.history.domains')}</dt>
              <dd>{queried.map((database) => sourceLabel(catalog, database, lang)).join(', ')}</dd></div>
          )}
        </dl>
      </div>

      <div className="sq-actions">
        {entry.run_id
          ? <a className="btn btn-primary" href={href.dataRun(entry.run_id)}><Icon name="pulse" size={14} />{t('sq.history.openRun')}</a>
          : <span className="field-hint">{t('sq.history.noRun')}</span>}
        <button type="button" className="btn btn-secondary" onClick={askAgain}>
          <Icon name="refresh" size={14} />{t('sq.history.rerun')}
        </button>
      </div>

      <p className="note neutral">{t('sq.history.sqlOnly')}</p>

      {entry.error && (
        <div className="note danger"><strong>{t('sq.history.error')}</strong><ServerText text={entry.error} /></div>
      )}

      <section className="sq-stack-sm">
        <h4 className="sq-section-title">{t('sq.history.sql')}</h4>
        {sqls.length ? sqls.map((row, index) => (
          <SqlCode key={index} sql={row.sql || row.final_sql || ''}
            title={`${t('sq.history.step', { n: index + 1 })}${row.database ? ` · ${sourceLabel(catalog, row.database, lang)}` : ''}`} />
        )) : <p className="empty">{t('sq.history.noSql')}</p>}
      </section>

      <details className="raw">
        <summary>{t('sq.history.technical')}</summary>
        <dl className="sq-kv">
          <div className="kv"><dt>{t('sq.history.traceId')}</dt><dd className="mono">{entry.trace_id}</dd></div>
          {entry.run_id && <div className="kv"><dt>{t('sq.run.runId')}</dt><dd className="mono">{entry.run_id}</dd></div>}
          <div className="kv"><dt>{t('sq.history.route')}</dt><dd className="mono">{entry.route || '—'}</dd></div>
          <div className="kv"><dt>{t('sq.history.status')}</dt>
            <dd className="mono">{entry.execution_status || t('sq.history.statusUnknown')}</dd></div>
          {(entry.databases ?? []).length > 0 && (
            <div className="kv"><dt>{t('sq.history.candidates')}</dt>
              <dd>{(entry.databases ?? []).map((database) => sourceLabel(catalog, database, lang)).join(', ')}</dd></div>
          )}
          {(entry.tables ?? []).length > 0 && (
            <div className="kv"><dt>{t('sq.history.tables')}</dt><dd className="mono">{(entry.tables ?? []).join(', ')}</dd></div>
          )}
          <div className="kv"><dt>{t('sq.history.duration')}</dt><dd>{formatDuration(entry.duration_ms) || '—'}</dd></div>
          <div className="kv"><dt>{t('sq.history.tokens')}</dt><dd>{entry.total_tokens || '—'}</dd></div>
          {entry.session_id && <div className="kv"><dt>{t('sq.history.session')}</dt><dd className="mono">{entry.session_id}</dd></div>}
          {!!entry.repaired_steps && <div className="kv"><dt>{t('sq.history.repaired')}</dt><dd>{entry.repaired_steps}</dd></div>}
        </dl>
      </details>
    </div>
  )
}
