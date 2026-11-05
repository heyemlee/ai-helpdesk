import { useEffect, useRef } from 'react'
import type { RunEvent, RunRecord } from '@/api/sql/types'
import { SqlApiError } from '@/api/sql/client'
import { useSqlCatalog, useSqlIdentity, useSqlRun, useSqlRunEvents } from '@/api/sql/hooks'
import { useT } from '@/i18n'
import { setAskDraft } from '@/lib/sql/askDraft'
import { sourceLabel } from '@/lib/sql/catalog'
import { formatCount, formatDateTime, formatDuration } from '@/lib/sql/format'
import { closedSet, stageLabel } from '@/lib/sql/labels'
import { buildTimeline, numberAttr, rowDuration, rowStatus, runDuration, stringAttr, type TimelineRow } from '@/lib/sql/runTimeline'
import { href } from '@/lib/route'
import type { Key } from '@/i18n/catalog'
import { Icon } from '../Icon'
import { Chip, StateBlock, type Tone } from '../ui'
import { ServerText, SqlCode, SqlErrorBlock } from './shared'

const RUN_TONES: Record<string, Tone> = {
  running: 'info', completed: 'success', failed: 'danger', cancelled: 'caution', interrupted: 'warning',
}
const STEP_TONES: Record<string, Tone> = { ok: 'success', error: 'danger', cancelled: 'caution', running: 'info' }

/** 契约的 execution_status → 历史页同一套状态字和色调（不另起一套说法） */
const OUTCOME_STATUS: Record<string, [Key, Tone]> = {
  success: ['sq.hstatus.success', 'success'],
  no_data: ['sq.hstatus.no_data', 'info'],
  partial_success: ['sq.hstatus.partial', 'caution'],
  failed: ['sq.hstatus.failed', 'danger'],
  not_applicable: ['sq.hstatus.conversation', 'neutral'],
}

/**
 * 一次查询的经过（#/data/runs/<run_id>）：SQL Agent 记下的运行记录和事件日志。
 *
 * run_id 就是 A2A 的 taskId、/query 的 X-Run-Id，所以从查询历史、从对话轮次（客服记在 tool_call
 * 事件的 data.trace 里）都能跳过来。控制台的 Key 是管理员，看得到所有调用方的 run；
 * 不是管理员的 Key 看别人的 run 一律 404（SQL Agent 不区分"不存在"和"没权限"）。
 */
export function RunPage({ runId }: { runId: string }) {
  const { t } = useT()
  const run = useSqlRun(runId)
  const running = run.data?.status === 'running'
  const events = useSqlRunEvents(runId, running, run.data?.persisted === true)
  // 跑着的时候事件每两秒取一次；刚结束的那一刻再取一次，最后几条（run.finished）才不会漏
  const wasRunning = useRef(false)
  const refetchEvents = events.refetch
  useEffect(() => {
    if (wasRunning.current && !running) void refetchEvents()
    wasRunning.current = running
  }, [running, refetchEvents])

  return (
    <div className="page scroll"><div className="page-inner">
      <div className="page-heading">
        <div>
          <h2 className="page-title">{t('sq.run.title')}</h2>
        </div>
        <div className="page-heading-actions">
          <a className="btn btn-ghost" href={href.dataHistory()}>{t('sq.run.back')}</a>
        </div>
      </div>

      {run.isLoading && <StateBlock busy title={t('sq.run.loading')} />}
      {run.isError && (
        run.error instanceof SqlApiError && run.error.status === 404
          ? <StateBlock title={t('sq.run.notFound')} detail={runId} />
          : <SqlErrorBlock error={run.error} title={t('sq.run.loadFailed')} onRetry={() => void run.refetch()} />
      )}
      {run.data && (
        <>
          <RunSummary run={run.data} />
          {run.data.persisted
            ? <Timeline events={events.data?.events ?? []} loading={events.isLoading} error={events.error}
                        onRetry={() => void events.refetch()} />
            : <p className="note neutral">{t('sq.run.memoryOnly')}</p>}
        </>
      )}
    </div></div>
  )
}

function RunSummary({ run }: { run: RunRecord }) {
  const { t, lang } = useT()
  const { identity } = useSqlIdentity()
  const self = !!identity && run.caller === identity.caller && run.user_id === identity.user_id
  const outcome = run.outcome ?? {}
  const took = runDuration(run.created_at, run.finished_at)

  const askAgain = () => {
    if (!run.query) return
    setAskDraft({ question: run.query, note: t('sq.history.rerunNote', { time: formatDateTime(run.created_at, lang) }) })
    window.location.hash = href.dataAsk()
  }

  return (
    <section className="card"><div className="card-body sq-stack">
      {run.query && (
        <div className="sq-stack-sm">
          <span className="field-hint">{t('sq.run.question')}</span>
          <h3 className="sq-question">{run.query}</h3>
        </div>
      )}
      <div className="chip-row">
        <Chip tone={RUN_TONES[run.status] ?? 'neutral'} dot>{closedSet('sq.run.status', run.status, t)}</Chip>
        {outcome.execution_status && OUTCOME_STATUS[outcome.execution_status] && (
          <Chip tone={OUTCOME_STATUS[outcome.execution_status]![1]}>{t(OUTCOME_STATUS[outcome.execution_status]![0])}</Chip>
        )}
        {outcome.has_risk && <Chip tone="caution">{t('sq.run.outcome.risk')}</Chip>}
        {outcome.degraded && <Chip tone="caution">{t('sq.run.outcome.degraded')}</Chip>}
      </div>
      {run.status === 'running' && <p className="note neutral" role="status">{t('sq.run.live')}</p>}
      {run.error && (
        <div className="note danger"><strong>{t('sq.run.error')}</strong><ServerText text={run.error} /></div>
      )}
      {run.cancel_reason && (
        <div className="note caution"><strong>{t('sq.run.cancelReason')}</strong><ServerText text={run.cancel_reason} /></div>
      )}
      <dl className="sq-kv">
        {(run.caller || run.user_id) && (
          <div className="kv"><dt>{t('sq.run.asker')}</dt>
            <dd>{self ? t('sq.run.asker.self') : t('sq.run.asker.other', { caller: run.caller ?? '—', user: run.user_id ?? '—' })}</dd></div>
        )}
        <div className="kv"><dt>{t('sq.run.entry')}</dt><dd>{closedSet('sq.run.entry', run.entry, t)}</dd></div>
        {run.created_at && <div className="kv"><dt>{t('sq.run.started')}</dt><dd>{formatDateTime(run.created_at, lang)}</dd></div>}
        {took !== null && <div className="kv"><dt>{t('sq.run.duration')}</dt><dd>{formatDuration(took)}</dd></div>}
        {run.usage && (
          <div className="kv"><dt>{t('sq.run.usage')}</dt>
            <dd>{t('sq.run.usage.value', { calls: formatCount(run.usage.calls, lang), tokens: formatCount(run.usage.total_tokens, lang) })}</dd></div>
        )}
        {typeof outcome.steps === 'number' && (
          <div className="kv"><dt>{t('sq.run.outcome')}</dt>
            <dd>{[
              t('sq.run.outcome.steps', { n: outcome.steps }),
              outcome.failed_steps ? t('sq.run.outcome.failed', { n: outcome.failed_steps }) : '',
              outcome.repaired_steps ? t('sq.run.outcome.repaired', { n: outcome.repaired_steps }) : '',
            ].filter(Boolean).join(' · ')}</dd></div>
        )}
        <div className="kv"><dt>{t('sq.run.runId')}</dt><dd className="mono">{run.run_id}</dd></div>
        {run.trace_id && <div className="kv"><dt>{t('sq.history.traceId')}</dt><dd className="mono">{run.trace_id}</dd></div>}
      </dl>
      {run.query && (
        <div className="sq-actions">
          <button type="button" className="btn btn-secondary" onClick={askAgain}>
            <Icon name="refresh" size={14} />{t('sq.run.ask')}
          </button>
        </div>
      )}
    </div></section>
  )
}

function Timeline({ events, loading, error, onRetry }: {
  events: RunEvent[]; loading: boolean; error: unknown; onRetry: () => void
}) {
  const { t } = useT()
  if (loading) return <StateBlock busy title={t('sq.run.loading')} />
  if (error) return <SqlErrorBlock error={error} title={t('sq.run.loadFailed')} onRetry={onRetry} />
  const timeline = buildTimeline(events)
  return (
    <section className="sq-stack">
      <h3 className="sq-section-title">{t('sq.run.timeline')}</h3>
      {timeline.rows.length ? (
        <ol className="sq-timeline">
          {timeline.rows.map((row) => <TimelineStep key={row.key} row={row} />)}
          {timeline.cancelRequested && (
            <li className="sq-timeline-row"><div className="sq-timeline-head">
              <strong>{t('sq.run.cancelRequested')}</strong>
              <ServerText text={stringAttr(timeline.cancelRequested.attrs, 'reason')} />
            </div></li>
          )}
        </ol>
      ) : <p className="empty">{t('sq.run.timeline.empty')}</p>}
    </section>
  )
}

function TimelineStep({ row }: { row: TimelineRow }) {
  const { t } = useT()
  const status = rowStatus(row)
  const duration = rowDuration(row)
  const fields = row.finished?.attrs.fields
  const startedAttrs = row.started?.attrs ?? {}
  const details = {
    ...(Object.keys(startedAttrs).length ? { started: startedAttrs } : {}),
    ...(fields && typeof fields === 'object' ? { result: fields } : {}),
  }
  const error = row.finished ? stringAttr(row.finished.attrs, 'error') : ''
  return (
    <li className="sq-timeline-row">
      <div className="sq-timeline-head">
        <strong>{row.stage ? stageLabel(row.stage, t) : t('sq.run.otherEvent')}</strong>
        {row.step !== null && <span className="field-hint">{t('sq.run.stepN', { n: row.step })}</span>}
        <span className="spacer" />
        {duration !== null && <span className="mono field-hint">{formatDuration(duration)}</span>}
        <Chip tone={STEP_TONES[status] ?? 'neutral'}>{closedSet('sq.run.step', status, t)}</Chip>
      </div>
      {error && <ServerText text={error} />}
      {row.events.map((event) => <TimelineEvent key={event.seq} event={event} />)}
      {Object.keys(details).length > 0 && (
        <details className="raw">
          <summary>{t('sq.run.details')}</summary>
          <pre className="mono sq-json">{JSON.stringify(details, null, 2)}</pre>
        </details>
      )}
    </li>
  )
}

function TimelineEvent({ event }: { event: RunEvent }) {
  const { t, lang } = useT()
  const catalog = useSqlCatalog().data
  const attrs = event.attrs
  const dropped = Array.isArray(attrs._dropped) ? (attrs._dropped as string[]) : []
  const droppedNote = dropped.length > 0 && <p className="field-hint">{t('sq.run.dropped', { fields: dropped.join(', ') })}</p>

  if (event.type === 'tool.sql') {
    const database = stringAttr(attrs, 'database')
    const status = stringAttr(attrs, 'status') || 'ok'
    const rows = numberAttr(attrs, 'row_count')
    const duration = numberAttr(attrs, 'duration_ms')
    const sql = stringAttr(attrs, 'sql')
    return (
      <div className="sq-event">
        <div className="sq-event-head">
          <span>{t('sq.run.sql')}{database ? ` · ${sourceLabel(catalog, database, lang)}` : ''}</span>
          <Chip tone={STEP_TONES[status] ?? 'neutral'}>{closedSet('sq.run.step', status, t)}</Chip>
          {rows !== null && <span className="field-hint">{t('sq.run.sql.rows', { n: rows })}</span>}
          {attrs.truncated === true && <span className="field-hint">{t('sq.run.sql.truncated')}</span>}
          {duration !== null && <span className="mono field-hint">{formatDuration(duration)}</span>}
        </div>
        {sql && <SqlCode sql={sql} />}
        <ServerText text={stringAttr(attrs, 'error')} />
        {droppedNote}
      </div>
    )
  }
  if (event.type === 'model.call') {
    const status = stringAttr(attrs, 'status') || 'ok'
    const duration = numberAttr(attrs, 'duration_ms')
    const tokens = (numberAttr(attrs, 'prompt_tokens') ?? 0) + (numberAttr(attrs, 'completion_tokens') ?? 0)
    const parts = [stringAttr(attrs, 'component'), stringAttr(attrs, 'model')].filter(Boolean)
    return (
      <div className="sq-event">
        <div className="sq-event-head">
          <span>{t('sq.run.model')}{parts.length ? ` · ${parts.join(' · ')}` : ''}</span>
          <Chip tone={STEP_TONES[status] ?? 'neutral'}>{closedSet('sq.run.step', status, t)}</Chip>
          {tokens > 0 && <span className="field-hint">{t('sq.run.tokens', { n: formatCount(tokens, lang) })}</span>}
          {attrs.will_retry === true && <span className="field-hint">{t('sq.run.model.retry')}</span>}
          {duration !== null && <span className="mono field-hint">{formatDuration(duration)}</span>}
        </div>
        <ServerText text={stringAttr(attrs, 'error')} />
        {droppedNote}
      </div>
    )
  }
  return (
    <div className="sq-event">
      <div className="sq-event-head"><span>{t('sq.run.otherEvent')} · <span className="mono">{event.type}</span></span></div>
      <pre className="mono sq-json">{JSON.stringify(attrs, null, 2)}</pre>
    </div>
  )
}
