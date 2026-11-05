import { useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { SqlApiError, sqlApi } from '@/api/sql/client'
import type { Readiness } from '@/api/sql/types'
import { useT } from '@/i18n'
import { explainSqlError } from '@/lib/sql/errors'
import { Card, Chip, KV, StateBlock } from './ui'
import { ConfirmBar } from './ModelSettings'
import { Icon } from './Icon'

/**
 * 管理与诊断 → SQL Agent（移植自旧 SQL 控制台设置里的"诊断"和"运维操作"）。
 *
 * 只读的三块：进程和依赖项（/health、/readyz）、Schema 索引（/admin/schema/status）、对回答的反馈
 * （/feedback/stats）。两个会改动正在运行的服务的按钮——重建 Schema 索引、复位模型熔断器——
 * 点了先在页面里确认（不用 window.confirm），结果照服务端回的写。
 * 这些接口都要管理员：控制台的 nginx 带的就是管理员 Key。
 */
const PCT = (lang: string, value: number | null | undefined) =>
  (typeof value === 'number' && Number.isFinite(value)
    ? new Intl.NumberFormat(lang === 'zh' ? 'zh-CN' : 'en-US', { style: 'percent', maximumFractionDigits: 1 }).format(value)
    : '—')

const show = (value: unknown) => (typeof value === 'string' ? value : JSON.stringify(value))

export function SqlAdminPanel() {
  const { t } = useT()
  return (
    <div className="page scroll"><div className="page-inner">
      <div>
        <h2 className="page-title">{t('sq.admin.title')}</h2>
        <p className="page-lede">{t('sq.admin.lede')}</p>
      </div>
      <Diagnostics />
      <SchemaIndex />
      <Circuits />
      <Feedback />
    </div></div>
  )
}

function ErrorState({ error, title }: { error: unknown; title: string }) {
  const { t } = useT()
  const { body, detail } = explainSqlError(error, t)
  return <StateBlock tone="danger" title={title} detail={detail || undefined}>{body}</StateBlock>
}

function Diagnostics() {
  const { t } = useT()
  const health = useQuery({ queryKey: ['sql', 'health'], queryFn: () => sqlApi.health(), retry: false })
  // 没就绪时 /readyz 回 503，检查项在错误的 detail 里：照样列出来
  const ready = useQuery({
    queryKey: ['sql', 'readyz'],
    queryFn: async (): Promise<Readiness> => {
      try {
        return await sqlApi.ready()
      } catch (error) {
        const detail = error instanceof SqlApiError ? error.detail as Readiness | null : null
        if (detail && typeof detail === 'object' && 'checks' in detail) return detail
        throw error
      }
    },
    retry: false,
  })
  const refresh = () => { void health.refetch(); void ready.refetch() }
  const bad = (error: unknown) => (error instanceof SqlApiError && error.status ? `HTTP ${error.status}` : t('sq.err.network'))
  return (
    <Card title={t('sq.admin.diag.title')} actions={(
      <button type="button" className="btn btn-secondary" onClick={refresh} disabled={health.isFetching || ready.isFetching}>
        <Icon name="refresh" size={14} />{t('sq.admin.diag.refresh')}
      </button>
    )}>
      {(health.isLoading || ready.isLoading) && <StateBlock busy title={t('sq.admin.diag.loading')} />}
      <dl>
        {!health.isLoading && (
          <KV label={t('sq.admin.diag.health')}>
            {health.isError ? <Chip tone="danger">{bad(health.error)}</Chip> : <Chip tone="success" dot>{t('sq.admin.diag.ok')}</Chip>}
          </KV>
        )}
        {!ready.isLoading && (
          <KV label={t('sq.admin.diag.ready')}>
            {ready.isError ? <Chip tone="danger">{bad(ready.error)}</Chip>
              : ready.data?.ready ? <Chip tone="success" dot>{t('sq.admin.diag.ok')}</Chip>
                : <Chip tone="danger" dot>{t('sq.admin.diag.notReady')}</Chip>}
          </KV>
        )}
        {Object.entries(ready.data?.checks ?? {}).map(([name, value]) => (
          <KV key={name} label={<span className="mono">{name}</span>}><span className="mono">{show(value)}</span></KV>
        ))}
      </dl>
    </Card>
  )
}

/** 一个会改动服务的按钮：先在页面里确认，做完把服务端的话写出来 */
function Operation({ label, confirm, run, done }: {
  label: string
  confirm: string
  run: () => Promise<unknown>
  done: (result: unknown) => string
}) {
  const { t } = useT()
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [outcome, setOutcome] = useState<{ ok: boolean; text: string; raw?: unknown } | null>(null)
  const go = async () => {
    setAsking(false); setBusy(true); setOutcome(null)
    try {
      const result = await run()
      setOutcome({ ok: true, text: done(result), raw: result })
    } catch (error) {
      const { body, detail } = explainSqlError(error, t)
      setOutcome({ ok: false, text: detail ? `${body} · ${detail}` : body })
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="sq-admin-op">
      <div>
        <button type="button" className="btn btn-secondary" disabled={busy || asking} onClick={() => { setOutcome(null); setAsking(true) }}>
          {busy ? t('sq.admin.running') : label}
        </button>
      </div>
      {asking && (
        <ConfirmBar confirm={t('sq.admin.confirm')} onConfirm={() => void go()} onCancel={() => setAsking(false)}>
          {confirm}
        </ConfirmBar>
      )}
      {outcome && (
        <div className={`note ${outcome.ok ? 'success' : 'danger'}`} role="status">
          <p>{outcome.ok && <Icon name="check" size={14} />}{outcome.text}</p>
          {outcome.raw !== undefined && (
            <details className="raw">
              <summary>{t('sq.admin.output')}</summary>
              <pre className="mono">{JSON.stringify(outcome.raw, null, 2)}</pre>
            </details>
          )}
        </div>
      )}
    </div>
  )
}

function SchemaIndex() {
  const { t } = useT()
  const queryClient = useQueryClient()
  const status = useQuery({ queryKey: ['sql', 'schema-status'], queryFn: () => sqlApi.schemaStatus(), retry: false })
  let body: ReactNode
  if (status.isError) body = <ErrorState error={status.error} title={t('sq.admin.schema.title')} />
  else if (!status.data) body = <StateBlock busy title={t('sq.admin.diag.loading')} />
  else {
    const data = status.data
    const pending = (data.pending_changes ?? []).map(show)
    body = (
      <>
        <p className="field-hint">
          {data.watcher_enabled ? t('sq.admin.schema.watcher.on', { s: data.interval_seconds }) : t('sq.admin.schema.watcher.off')}
        </p>
        <p className={`note ${pending.length ? 'caution' : 'neutral'}`}>
          {pending.length ? t('sq.admin.schema.pending', { dbs: pending.join(', ') }) : t('sq.admin.schema.upToDate')}
        </p>
        <dl>
          {Object.entries(data.fingerprints ?? {}).map(([database, fingerprint]) => (
            <KV key={database} label={<span className="mono">{database}</span>}>
              <span className="mono" title={t('sq.admin.schema.fingerprint')}>{fingerprint}</span>
            </KV>
          ))}
        </dl>
      </>
    )
  }
  return (
    <Card title={t('sq.admin.schema.title')}>
      {body}
      <Operation label={t('sq.admin.schema.reload')} confirm={t('sq.admin.schema.confirm')}
        run={async () => {
          const result = await sqlApi.reloadSchema()
          void queryClient.invalidateQueries({ queryKey: ['sql', 'schema-status'] })
          return result
        }}
        done={(result) => {
          const changed = (result as { changed_databases?: unknown[] } | null)?.changed_databases ?? []
          return changed.length ? t('sq.admin.schema.done', { dbs: changed.map(show).join(', ') }) : t('sq.admin.schema.unchanged')
        }} />
    </Card>
  )
}

function Circuits() {
  const { t } = useT()
  const queryClient = useQueryClient()
  return (
    <Card title={t('sq.admin.circuit.title')}>
      <p className="field-hint">{t('sq.admin.circuit.hint')}</p>
      <Operation label={t('sq.admin.circuit.reset')} confirm={t('sq.admin.circuit.confirm')}
        run={async () => {
          const result = await sqlApi.resetCircuits()
          void queryClient.invalidateQueries({ queryKey: ['sql', 'readyz'] })
          void queryClient.invalidateQueries({ queryKey: ['sql', 'model-status'] })
          return result
        }}
        done={() => t('sq.admin.circuit.done')} />
    </Card>
  )
}

function Feedback() {
  const { t, lang } = useT()
  const stats = useQuery({ queryKey: ['sql', 'feedback-stats'], queryFn: () => sqlApi.feedbackStats(), retry: false })
  let body: ReactNode
  if (stats.isError) body = <ErrorState error={stats.error} title={t('sq.admin.feedback.title')} />
  else if (!stats.data) body = <StateBlock busy title={t('sq.admin.diag.loading')} />
  else if (!stats.data.stats.total) body = <p className="empty">{t('sq.admin.feedback.none')}</p>
  else {
    const s = stats.data.stats
    body = (
      <>
        <dl>
          <KV label={t('sq.admin.feedback.total')}>{s.total}</KV>
          <KV label={t('sq.admin.feedback.accuracy')}>{PCT(lang, s.accuracy)}</KV>
          <KV label={t('sq.admin.feedback.silent')}>{PCT(lang, s.silent_error_rate)}</KV>
          <KV label={t('sq.admin.feedback.unresolved')}>{s.unresolved_wrong}</KV>
        </dl>
        {stats.data.recent_wrong.length > 0 && (
          <>
            <p className="section-label">{t('sq.admin.feedback.recent')}</p>
            <ul className="sq-admin-wrong">
              {stats.data.recent_wrong.map((item) => (
                <li key={item.trace_id}>
                  <span>{item.query}</span>
                  {item.had_risk && <> <Chip tone="caution">{t('sq.admin.feedback.hadRisk')}</Chip></>}
                  {item.comment && <p className="field-hint">{item.comment}</p>}
                </li>
              ))}
            </ul>
          </>
        )}
      </>
    )
  }
  return (
    <Card title={t('sq.admin.feedback.title')}>
      <p className="field-hint">{t('sq.admin.feedback.hint')}</p>
      {body}
    </Card>
  )
}
