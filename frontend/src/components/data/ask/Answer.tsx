/**
 * 一轮问答里"答案"那一块（移植自旧控制台的 result.js）。
 *
 * 从上到下：状态（一行）→ 结论 → 需要人注意的提示 → 每一步结果一块面板 → 数据日期 → 操作与反馈 →
 * 查询过程（普通模式默认收起；学习模式换成逐步讲解）。等待中、停止了、请求没成功各是一种样子。
 */
import { useState, type ReactNode } from 'react'
import { SqlApiError, sqlApi } from '@/api/sql/client'
import type { Catalog, ServiceInfo } from '@/api/sql/types'
import { useT } from '@/i18n'
import type { Key } from '@/i18n/catalog'
import { conclusion, summaryText } from '@/lib/sql/ask-summary'
import { domainTitleOf, sourceLabel } from '@/lib/sql/catalog'
import { explainSqlError } from '@/lib/sql/errors'
import { formatDuration, formatRange, formatTime, recordDates } from '@/lib/sql/format'
import { hasKey } from '@/lib/sql/labels'
import { classify, splitSteps, type Classification, type QueryResult, type ResultStep } from '@/lib/sql/model'
import { anomalyHelp, anomalyLabel, failureReason, stepColumns } from '@/lib/sql/steps'
import { href } from '@/lib/route'
import { Icon } from '../../Icon'
import { Chip } from '../../ui'
import { CopyButton } from '../shared'
import { forgetFeedback, recordFeedback, useSentFeedback, type Verdict } from './feedbackStore'
import { LearnPanel, ProcessDetails } from './Process'
import { RichText } from './RichText'
import { StageTrack } from './StageTrack'
import { StepPanel } from './StepPanel'
import type { CancelState, Turn } from './turn'

const VERDICTS: Verdict[] = ['correct', 'wrong', 'unsure']

/** 提示块。role 是 note：页面重画时不该把每一条旧提示都再念一遍，新结果由页面上的播报区念一次 */
function Notice({ tone, title, children }: { tone: 'info' | 'caution' | 'danger' | 'neutral'; title: string; children?: ReactNode }) {
  return (
    <div className={`note ${tone} sq-notice`} role="note">
      <p className="sq-notice-title">{title}</p>
      {children}
    </div>
  )
}

function AnswerHead({ children, aside }: { children: ReactNode; aside?: ReactNode }) {
  return (
    <div className="sq-answer-head">
      <span className="sq-agent-mark" aria-hidden="true"><Icon name="grid" size={14} /></span>
      <div className="sq-status-line">{children}</div>
      {aside}
    </div>
  )
}

/** 状态标签。每种状态都是"文字 + 色调"，颜色只是第二重提示 */
function StatusChips({ view }: { view: Classification }) {
  const { t } = useT()
  const map: Record<Classification['kind'], ['success' | 'info' | 'caution' | 'danger' | 'neutral', Key]> = {
    success: ['success', 'sq.ask.status.success'],
    no_data: ['info', 'sq.ask.status.no_data'],
    partial: ['caution', 'sq.ask.status.partial'],
    failed: ['danger', 'sq.ask.status.failed'],
    conversation: ['neutral', 'sq.ask.status.conversation'],
  }
  const [tone, key] = map[view.kind]
  return (
    <span className="chip-row">
      <Chip tone={tone} dot>{t(key)}</Chip>
      {view.needsReview && <Chip tone="caution">{t('sq.ask.status.review')}</Chip>}
      {view.truncated && <Chip tone="caution">{t('sq.ask.status.truncated')}</Chip>}
      {view.degraded && <Chip tone="caution">{t('sq.ask.status.degraded')}</Chip>}
    </span>
  )
}

function Notices({ result, view, catalog }: { result: QueryResult; view: Classification; catalog: Catalog | null | undefined }) {
  const { t, lang } = useT()
  const out: ReactNode[] = []

  if (view.kind === 'failed') {
    const reasons = view.failedSteps.map((step) => failureReason(step.error))
    const first = reasons[0] ?? { key: result.degraded ? 'sq.ask.fail.modelDown' as const : 'sq.ask.fail.generic' as const, detail: '' }
    out.push(
      <Notice key="failed" tone="danger" title={t('sq.ask.notice.failed.title')}>
        <p>{t(first.key)}{first.detail && <> <span className="mono">{first.detail}</span></>}</p>
        <p>{t('sq.ask.notice.failed.hint')}</p>
      </Notice>,
    )
  }

  if (view.kind === 'no_data') {
    const notes = view.findings.filter((finding) => finding.anomaly === 'empty_result')
    out.push(
      <Notice key="no-data" tone="info" title={t('sq.ask.notice.noData.title')}>
        <p>{t('sq.ask.notice.noData.body')}</p>
        {notes.map((finding, index) => typeof finding.detail === 'string' && finding.detail
          ? <p key={index} className="sq-server-text" lang="zh">{t('sq.ask.notice.validatorNote')} {finding.detail}</p> : null)}
      </Notice>,
    )
  }

  if (view.kind === 'partial') {
    out.push(
      <Notice key="partial" tone="caution" title={t('sq.ask.notice.partial.title')}>
        <p>{t('sq.ask.notice.partial.body')}</p>
        <ul>
          {view.failedSteps.map((step) => {
            const reason = failureReason(step.error)
            return (
              <li key={step.index}>
                {t('sq.ask.process.step', { n: step.index + 1 })} · {sourceLabel(catalog, step.database, lang)}: {t(reason.key)}
                {reason.detail && <> <span className="mono">{reason.detail}</span></>}
              </li>
            )
          })}
        </ul>
      </Notice>,
    )
  }

  if (view.needsReview) {
    const findings = view.findings.filter((finding) => !(view.kind === 'no_data' && finding.anomaly === 'empty_result'))
    out.push(
      <Notice key="review" tone="caution" title={t('sq.ask.notice.review.title')}>
        <p>{t('sq.ask.notice.review.body')}</p>
        {findings.length ? (
          <ul>
            {findings.map((finding, index) => (
              <li key={index}>
                <strong>{anomalyLabel(finding.anomaly, t)}</strong>
                {finding.severity === 'block' ? ` · ${t('sq.ask.notice.review.block')}` : ''}
                {anomalyHelp(finding.anomaly, t) ? ` — ${anomalyHelp(finding.anomaly, t)}` : ''}
                {typeof finding.detail === 'string' && finding.detail && (
                  <><br /><span className="sq-server-text" lang="zh">{t('sq.ask.notice.validatorNote')} {finding.detail}</span></>
                )}
              </li>
            ))}
          </ul>
        ) : <p>{t('sq.ask.notice.review.unspecified')}</p>}
      </Notice>,
    )
  }

  if (view.truncated) {
    const limits = [...new Set(result.steps.filter((step) => step.truncated && step.rowLimit).map((step) => step.rowLimit))]
    out.push(
      <Notice key="truncated" tone="caution" title={t('sq.ask.notice.truncated.title')}>
        <p>{limits.length
          ? t('sq.ask.notice.truncated.body', { limit: limits.join(' / ') })
          : t('sq.ask.notice.truncated.bodyNoLimit')}</p>
      </Notice>,
    )
  }

  if (view.degraded && view.kind !== 'failed') {
    out.push(
      <Notice key="degraded" tone="caution" title={t('sq.ask.notice.degraded.title')}>
        <p>{t('sq.ask.notice.degraded.body')}</p>
      </Notice>,
    )
  }
  return <>{out}</>
}

/**
 * 这批数据是什么时候的。来源库写在每块面板的头上；有哪一步没有面板（它没有返回行）时，在这里把来源
 * 列全——查过的库不能因为没查到东西就从页面上消失。
 */
function MetaLine({ result, answers, withSources, catalog }: {
  result: QueryResult; answers: ResultStep[]; withSources: boolean; catalog: Catalog | null | undefined
}) {
  const { t, lang } = useT()
  const sources = [...new Set(answers.map((step) => sourceLabel(catalog, step.database, lang)))]
  const dates = answers.flatMap((step) => {
    const columns = stepColumns(step, catalog, lang)
    const byKey = new Map(columns.map((column) => [column.key, column]))
    return recordDates(columns.map((column) => column.key), step.rows, (row, key) => row[key])
      .map((range) => `${formatRange(range)} (${byKey.get(range.column)?.label ?? range.column})`)
  })
  const read = !result.asOf
    ? t('sq.ask.meta.readUnknown')
    : result.asOfSource === 'query_time'
      ? t('sq.ask.meta.readLive', { time: formatTime(result.asOf, lang) })
      : result.asOfSource === 'snapshot'
        ? t('sq.ask.meta.readSnapshot', { time: formatTime(result.asOf, lang) })
        : t('sq.ask.meta.readUnknown')
  return (
    <div className="sq-result-meta" data-testid="result-meta">
      {withSources && sources.length > 0 && (
        <span><strong>{t('sq.ask.meta.source')}:</strong> {sources.join(', ')}</span>
      )}
      <span><strong>{t('sq.ask.meta.recordDates')}:</strong> {dates.length ? dates.join('; ') : t('sq.ask.meta.noRecordDates')}</span>
      <span title={t('sq.ask.meta.readHint')}>{read}</span>
    </div>
  )
}

/** 反馈：只发 trace_id、判定和补充说明——问题原文和 SQL 由服务端按审计记录回填，不由页面说了算 */
function Feedback({ traceId }: { traceId: string }) {
  const { t } = useT()
  const sent = useSentFeedback(traceId)
  const [open, setOpen] = useState(false)
  const [comment, setComment] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')

  if (sent) {
    return (
      <div className="sq-feedback is-sent" role="status">
        <Icon name="check" size={14} /><span>{t(`sq.ask.feedback.sent.${sent}`)}</span>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => forgetFeedback(traceId)}>{t('sq.ask.feedback.change')}</button>
      </div>
    )
  }

  const submit = async (verdict: Verdict) => {
    setSending(true)
    setError('')
    try {
      await sqlApi.feedback({ trace_id: traceId, verdict, comment: comment.trim() })
      recordFeedback(traceId, verdict)
    } catch (caught) {
      setError(caught instanceof SqlApiError ? caught.serverMessage || caught.message : String(caught))
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="sq-feedback-block">
      <div className="sq-feedback">
        <span>{t('sq.ask.feedback.ask')}</span>
        {VERDICTS.map((verdict) => (
          <button key={verdict} type="button" className="btn btn-secondary btn-sm" disabled={sending}
                  onClick={() => void submit(verdict)}>
            {t(`sq.ask.feedback.${verdict}`)}
          </button>
        ))}
        <button type="button" className="btn btn-ghost btn-sm" aria-expanded={open} onClick={() => setOpen((was) => !was)}>
          {t('sq.ask.feedback.addComment')}
        </button>
      </div>
      {open && (
        <textarea className="field sq-feedback-comment" rows={2} maxLength={2000} value={comment}
                  aria-label={t('sq.ask.feedback.commentLabel')} placeholder={t('sq.ask.feedback.commentPlaceholder')}
                  onChange={(event) => setComment(event.target.value)} />
      )}
      {error && <p className="note danger" role="alert">{t('sq.ask.feedback.failed')}: {error}</p>}
    </div>
  )
}

interface TurnActions {
  onAskAgain: () => void
  onFollowUp: () => void
}

/** 有了结果的一轮 */
function ResultAnswer({ turn, catalog, service, learning, actions }: {
  turn: Turn; catalog: Catalog | null | undefined; service: ServiceInfo | null; learning: boolean; actions: TurnActions
}) {
  const { t, lang } = useT()
  const result = turn.result!
  const view = classify(result)
  const { answers } = splitSteps(result, turn.question)
  const ctx = { t, lang, catalog }
  let content: ReactNode

  if (view.kind === 'conversation') {
    content = (
      <>
        <RichText text={result.answer} />
        <Notice tone="info" title={t('sq.ask.notice.conversation.title')}><p>{t('sq.ask.notice.conversation.body')}</p></Notice>
        {view.degraded && (
          <Notice tone="caution" title={t('sq.ask.notice.degraded.title')}><p>{t('sq.ask.notice.degradedChat.body')}</p></Notice>
        )}
      </>
    )
  } else {
    const summary = conclusion(result, view, answers, ctx)
    const showTables = view.kind === 'success' || view.kind === 'partial'
    const panels = showTables ? answers.filter((step) => step.rowCount > 0) : []
    content = (
      <>
        <p className="sq-conclusion">
          {summary.headline}{summary.names && <>: <strong>{summary.names}</strong></>}
          {result.currency && showTables && (
            <> <Chip title={t('sq.ask.meta.currencyHint')}>{t('sq.ask.meta.currency', { currency: result.currency })}</Chip></>
          )}
        </p>
        <Notices result={result} view={view} catalog={catalog} />
        {panels.map((step) => <StepPanel key={step.index} step={step} multiple={panels.length > 1} catalog={catalog} />)}
        {view.kind !== 'failed' && (
          <MetaLine result={result} answers={answers} withSources={panels.length !== answers.length} catalog={catalog} />
        )}
      </>
    )
  }

  const clientMs = result.clientMs
  const serverMs = turn.backfill?.serverMs ?? null
  const elapsed = clientMs !== null && (
    <span className="sq-answer-time"
          title={t('sq.ask.process.clientTime', { time: formatDuration(clientMs) })
            + (serverMs !== null ? ` · ${t('sq.ask.process.serverTime', { time: formatDuration(serverMs) })}` : '')}>
      {formatDuration(clientMs)}
    </span>
  )

  return (
    <div className="sq-answer">
      <AnswerHead aside={elapsed}><StatusChips view={view} /></AnswerHead>
      <div className="sq-answer-body">
        {turn.rerun && (
          <Notice tone="info" title={t('sq.ask.origin.rerun.title')}>
            <p>{t(result.queriedDatabase ? 'sq.ask.origin.rerun.body' : 'sq.ask.origin.rerun.bodyNoQuery')}</p>
          </Notice>
        )}
        {content}
        <div className="sq-answer-foot">
          <div className="sq-answer-actions">
            <CopyButton text={summaryText(turn.question, result, ctx)} label={t('sq.ask.action.copyAnswer')} />
            <button type="button" className="btn btn-ghost btn-sm" onClick={actions.onAskAgain}>
              <Icon name="refresh" size={14} />{t('sq.ask.action.askAgain')}
            </button>
            {view.kind !== 'failed' && (
              <button type="button" className="btn btn-ghost btn-sm" onClick={actions.onFollowUp}>
                {t('sq.ask.action.followUp')}
              </button>
            )}
            {turn.runId && (
              <a className="btn btn-ghost btn-sm" href={href.dataRun(turn.runId)}>
                <Icon name="pulse" size={14} />{t('sq.history.openRun')}
              </a>
            )}
          </div>
          {result.traceId && service?.feedback_enabled !== false && <Feedback traceId={result.traceId} />}
        </div>
        {learning
          ? <LearnPanel question={turn.question} transport={turn.transport} stages={turn.stages} result={result}
                        failed={false} backfill={turn.backfill} catalog={catalog} />
          : <ProcessDetails question={turn.question} result={result} backfill={turn.backfill} catalog={catalog} />}
      </div>
    </div>
  )
}

/** 等结果时那句"现在在做什么"。有阶段事件就照事件说，没有就只说在处理 */
export function pendingStage(turn: Pick<Turn, 'stageNow'>, catalog: Catalog | null | undefined,
  t: (key: Key, vars?: Record<string, string | number>) => string, lang: 'en' | 'zh'): string {
  const stage = turn.stageNow
  const key = stage ? `sq.stage.${stage.stage}` : ''
  return stage && hasKey(key)
    ? t(key) + (stage.database ? ` (${domainTitleOf(catalog, stage.database, lang)})` : '')
    : t('sq.ask.working')
}

/**
 * 等待中。走流式时有六段进度（真实的阶段事件）；走 POST /query 时服务端什么都不回报，只有一条不表示
 * 进度的动效——不拿定时器假装在一步步往前走。
 */
function PendingAnswer({ turn, elapsedSeconds, catalog, learning, onStop }: {
  turn: Turn; elapsedSeconds: number; catalog: Catalog | null | undefined; learning: boolean; onStop: () => void
}) {
  const { t, lang } = useT()
  return (
    <div className="sq-answer is-pending">
      <div className="sq-answer-head">
        <span className="sq-agent-mark is-busy" aria-hidden="true"><Icon name="grid" size={14} /></span>
        <span className="sq-pending-text" role="status">{pendingStage(turn, catalog, t, lang)}</span>
        <span className="sq-answer-time" aria-live="off">{elapsedSeconds >= 2 ? t('sq.ask.elapsed', { n: elapsedSeconds }) : ''}</span>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onStop}>{t('sq.ask.stop')}</button>
      </div>
      <div className="sq-answer-body">
        {turn.transport === 'stream'
          ? <StageTrack stages={turn.stages} />
          : <div className="sq-progress-indet" aria-hidden="true" />}
        <div className="sq-skeleton" aria-hidden="true"><i /><i /><i /></div>
        {learning && (
          <LearnPanel question={turn.question} transport={turn.transport} stages={turn.stages} result={null}
                      failed={false} backfill={null} catalog={catalog} />
        )}
      </div>
    </div>
  )
}

/**
 * 停止之后说清楚服务端那边怎样了。流式请求停止时会显式请服务端取消，cancel 记着结果：请求还在路上或
 * 已受理、服务端已经跑完（409）。其余情况（/query 没有运行号、取消请求本身失败）只能靠断开连接：
 * 服务端默认在断开后停下（AI_SQL_CANCEL_ON_DISCONNECT），但这个开关可以关。
 */
const STOPPED_BODY: Partial<Record<CancelState, Key>> = {
  sent: 'sq.ask.notice.cancelled.body',
  accepted: 'sq.ask.notice.cancelled.body',
  finished: 'sq.ask.notice.cancelLate.body',
}

export function StoppedAnswer({ cancel, onAskAgain }: { cancel: CancelState; onAskAgain: () => void }) {
  const { t } = useT()
  return (
    <div className="sq-answer">
      <AnswerHead><Chip>{t('sq.ask.status.stopped')}</Chip></AnswerHead>
      <div className="sq-answer-body">
        <Notice tone="neutral" title={t('sq.ask.notice.stopped.title')}>
          <p>{t(STOPPED_BODY[cancel] ?? 'sq.ask.notice.stopped.body')}</p>
        </Notice>
        <div className="sq-answer-actions">
          <button type="button" className="btn btn-ghost btn-sm" onClick={onAskAgain}>
            <Icon name="refresh" size={14} />{t('sq.ask.action.askAgain')}
          </button>
        </div>
      </div>
    </div>
  )
}

/** 请求本身没成功（没连上、被拒、服务端出错）——和"查询跑了但失败"是两回事 */
function ServiceErrorAnswer({ turn, catalog, learning, onAskAgain }: {
  turn: Turn; catalog: Catalog | null | undefined; learning: boolean; onAskAgain: () => void
}) {
  const { t } = useT()
  const error = turn.error
  const code = error instanceof SqlApiError ? error.code : ''
  const explained = explainSqlError(error, t)
  const body = code === 'stream_interrupted' ? t('sq.ask.error.interrupted')
    : code === 'query_failed' ? t('sq.ask.error.queryFailed')
      : explained.body
  const status = error instanceof SqlApiError ? error.status : 0
  const serverMessage = error instanceof SqlApiError ? error.serverMessage : ''
  return (
    <div className="sq-answer">
      <AnswerHead><Chip tone="danger" dot>{t('sq.ask.status.error')}</Chip></AnswerHead>
      <div className="sq-answer-body">
        <Notice tone="danger" title={t('sq.ask.notice.service.title')}>
          <p>{body}</p>
          <p className="sq-server-text">
            {status ? `HTTP ${status}` : t('sq.ask.error.noResponse')}
            {serverMessage && <> · <span lang="zh">{serverMessage}</span></>}
          </p>
        </Notice>
        <div className="sq-answer-actions">
          <button type="button" className="btn btn-ghost btn-sm" onClick={onAskAgain}>
            <Icon name="refresh" size={14} />{t('sq.ask.action.tryAgain')}
          </button>
        </div>
        {learning && (
          <LearnPanel question={turn.question} transport={turn.transport} stages={turn.stages} result={null}
                      failed backfill={null} catalog={catalog} />
        )}
      </div>
    </div>
  )
}

/** 一轮：右边是问题，左边是答案 */
export function TurnView({ turn, catalog, service, learning, now, onStop, onAskAgain, onFollowUp }: {
  turn: Turn
  catalog: Catalog | null | undefined
  service: ServiceInfo | null
  learning: boolean
  /** performance.now()，等待时每秒更新一次 */
  now: number
  onStop: () => void
  onAskAgain: () => void
  onFollowUp: () => void
}) {
  const { t, lang } = useT()
  const elapsedSeconds = Math.max(0, Math.floor((now - turn.startedAt) / 1000))
  return (
    <article className="sq-turn" data-turn={turn.id}>
      <div className="sq-turn-q">
        <div className="sq-q-bubble"><h3 className="sq-question-text">{turn.question}</h3></div>
        <div className="sq-q-meta">
          <span>{t(turn.rerun ? 'sq.ask.askedRerun' : 'sq.ask.asked', { time: formatTime(turn.askedAt, lang) })}</span>
          <CopyButton text={turn.question} label={t('sq.ask.action.copyQuestion')} />
        </div>
      </div>
      <div className="sq-turn-a">
        {turn.status === 'pending' && (
          <PendingAnswer turn={turn} elapsedSeconds={elapsedSeconds} catalog={catalog} learning={learning} onStop={onStop} />
        )}
        {turn.status === 'stopped' && <StoppedAnswer cancel={turn.cancel} onAskAgain={onAskAgain} />}
        {turn.status === 'error' && (
          <ServiceErrorAnswer turn={turn} catalog={catalog} learning={learning} onAskAgain={onAskAgain} />
        )}
        {turn.status === 'done' && turn.result && (
          <ResultAnswer turn={turn} catalog={catalog} service={service} learning={learning}
                        actions={{ onAskAgain, onFollowUp }} />
        )}
      </div>
    </article>
  )
}
