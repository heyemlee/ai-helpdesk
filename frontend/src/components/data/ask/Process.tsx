/**
 * 查询过程（普通模式，默认收起）与学习模式的逐步讲解（移植自旧控制台的 process.js）。
 *
 * 两者展示的是同一批事实，只是讲法不同。**事实只有两个来源**：
 * 1. 这一轮响应里的字段（route、steps[].final_sql、repair_attempts、risk_findings…）；
 * 2. 走 A2A 流时，服务端真实发出的阶段事件（哪个阶段、第几步、哪个库、相对时刻）。
 *
 * 没有第三个来源。响应里看不出来的事（比如这次有没有单独调规划模型），在没有阶段事件时就写
 * "这条路径不回报"，不按"通常会这样"补一句。每一步后面那段讲解说的是设计，不是这次请求的经过，
 * 两者在版面上分开。
 */
import { useState, type ReactNode } from 'react'
import type { StreamStage } from '@/api/sql/client'
import type { Catalog } from '@/api/sql/types'
import { useT } from '@/i18n'
import type { Key } from '@/i18n/catalog'
import { STAGE_TO_STEP, type LearnStep } from '@/lib/sql/ask-progress'
import { sourceLabel } from '@/lib/sql/catalog'
import { formatDuration, formatTime } from '@/lib/sql/format'
import { hasKey } from '@/lib/sql/labels'
import { classify, passedOn, splitSteps, type QueryResult, type ResultStep } from '@/lib/sql/model'
import { anomalyLabel, stepColumns, stepMatrix } from '@/lib/sql/steps'
import { Chip } from '../../ui'
import { DataTable } from '../DataTable'
import { CopyButton, SqlCode } from '../shared'
import type { Backfill, Transport } from './turn'

const RAW_LIMIT = 30000
const INTERMEDIATE_ROWS = 10

/**
 * 每一步讲解对应的源码位置。第一个路径从仓库根算，后面的相对 ai-sql-agent/src/ai_sql_agent/；
 * 括号里是符号名。__tests__/ask-learning.test.ts 核对这些文件都还在。路径不翻译。
 */
export const LEARN_SOURCES: Record<LearnStep, string> = {
  understand: 'ai-sql-agent/src/ai_sql_agent/pipeline/routing.py · pipeline/dynamic_service.py · normalization/normalizer.py',
  retrieve: 'ai-sql-agent/src/ai_sql_agent/retrieval/hierarchical_service.py · retrieval/rrf_fusion_client.py · pipeline/schema_budget.py · retrieval/access.py',
  plan: 'ai-sql-agent/src/ai_sql_agent/planning/cot_planner.py · pipeline/short_path.py · pipeline/text2sql_pipeline.py (_as_sql_literals)',
  generate: 'ai-sql-agent/src/ai_sql_agent/generation/sql_generator.py · generation/prompt_builder.py · execution/dialects.py',
  execute: 'ai-sql-agent/src/ai_sql_agent/execution/sqlite_executor.py · execution/sql_guard.py · auth/enforcement.py · repair/repairer.py',
  check: 'ai-sql-agent/src/ai_sql_agent/validation/validator.py · validation/cardinality.py · validation/self_check.py · api/result_summary.py · api/trust.py',
}

type T = (key: Key, vars?: Record<string, string | number>) => string

const routeLabel = (route: string, t: T) => {
  const key = `sq.ask.route.${route}`
  return hasKey(key) ? `${t(key)} (${route})` : route || '—'
}
const transportLabel = (transport: Transport, t: T) =>
  t(transport === 'stream' ? 'sq.ask.process.transport.stream' : 'sq.ask.process.transport.query')
const offset = (stage: StreamStage) => (stage.offsetMs === null ? '' : `+${(stage.offsetMs / 1000).toFixed(2)}s`)

function KvRow({ label, children }: { label: ReactNode; children: ReactNode }) {
  return <div className="kv"><dt>{label}</dt><dd>{children}</dd></div>
}

function SqlBlocks({ step }: { step: ResultStep }) {
  const { t } = useT()
  return (
    <>
      {step.finalSql
        ? <SqlCode sql={step.finalSql} title={t('sq.ask.process.sql.final')} />
        : <p className="field-hint">{t('sq.ask.process.noSql')}</p>}
      {step.repairs.length > 0 && step.sql && step.sql !== step.finalSql && (
        <SqlCode sql={step.sql} title={t('sq.ask.process.sql.original')} />
      )}
    </>
  )
}

function StepChips({ step, intermediate }: { step: ResultStep; intermediate: boolean }) {
  const { t } = useT()
  return (
    <span className="chip-row">
      {step.success
        ? <Chip>{t('sq.ask.result.rows', { n: step.rowCount })}</Chip>
        : <Chip tone="danger">{t('sq.ask.process.stepFailed')}</Chip>}
      {step.truncated && <Chip tone="caution">{t('sq.ask.status.truncated')}{step.rowLimit ? ` · ${step.rowLimit}` : ''}</Chip>}
      {step.repairs.length > 0 && <Chip tone="caution">{t('sq.ask.process.repaired', { n: step.repairs.length })}</Chip>}
      {step.findings.length > 0 && <Chip tone="caution">{t('sq.ask.process.findings', { n: step.findings.length })}</Chip>}
      {intermediate && <Chip tone="info">{t('sq.ask.process.intermediate')}</Chip>}
    </span>
  )
}

interface RepairAttempt {
  attempt_no?: number
  success?: boolean
  error_type?: string
  failing_clause?: string
  strategy?: string
  error_after_repair?: string
}

function RepairList({ step }: { step: ResultStep }) {
  const { t } = useT()
  if (!step.repairs.length) return null
  return (
    <div>
      <p className="field-hint">{t('sq.ask.process.repairs')}</p>
      <ol className="sq-plain-list">
        {(step.repairs as RepairAttempt[]).map((attempt, index) => (
          <li key={index}>
            <Chip tone={attempt.success ? 'neutral' : 'danger'}>
              #{attempt.attempt_no ?? ''} {attempt.success ? t('sq.ask.process.repairOk') : t('sq.ask.process.repairFailed')}
            </Chip>{' '}
            <span className="sq-tech">
              {attempt.error_type ?? ''}{attempt.failing_clause ? ` · ${attempt.failing_clause}` : ''}
              {attempt.strategy ? ` · ${attempt.strategy}` : ''}
            </span>
            {attempt.error_after_repair && <p lang="zh" className="sq-server-text">{attempt.error_after_repair}</p>}
          </li>
        ))}
      </ol>
    </div>
  )
}

function FindingList({ step }: { step: ResultStep }) {
  const { t } = useT()
  if (!step.findings.length) return null
  return (
    <ul className="sq-plain-list">
      {step.findings.map((finding, index) => (
        <li key={index}>
          <Chip tone="caution">{anomalyLabel(finding.anomaly, t)}</Chip>{' '}
          <span className="sq-tech">{finding.anomaly}{finding.severity ? ` · ${finding.severity}` : ''}</span>
          {typeof finding.detail === 'string' && finding.detail && <p lang="zh">{finding.detail}</p>}
        </li>
      ))}
    </ul>
  )
}

function IntermediateRows({ step, catalog }: { step: ResultStep; catalog: Catalog | null | undefined }) {
  const { t, lang } = useT()
  if (!step.rows.length) return null
  const columns = stepColumns(step, catalog, lang)
  const rows = stepMatrix(step, columns).slice(0, INTERMEDIATE_ROWS)
  return (
    <div>
      <DataTable label={t('sq.ask.process.intermediate')} rows={rows}
        columns={columns.map((column) => ({ key: column.key, label: column.label, unit: column.unit,
          numeric: column.numeric, plain: column.plain, hint: column.hint }))} />
      {step.rows.length > rows.length && (
        <p className="field-hint">{t('sq.ask.process.firstRows', { n: rows.length, total: step.rows.length })}</p>
      )}
    </div>
  )
}

function ReadAt({ step }: { step: ResultStep }) {
  const { t, lang } = useT()
  if (!step.asOf) return null
  return <p className="field-hint">{t('sq.ask.process.readAt', { time: formatTime(step.asOf, lang), source: step.asOfSource || 'unknown' })}</p>
}

function TimeLine({ result, backfill }: { result: QueryResult; backfill: Backfill | null }) {
  const { t } = useT()
  return (
    <>
      {result.clientMs !== null ? t('sq.ask.process.clientTime', { time: formatDuration(result.clientMs) }) : '—'}
      {backfill?.serverMs !== null && backfill?.serverMs !== undefined
        ? ` · ${t('sq.ask.process.serverTime', { time: formatDuration(backfill.serverMs) })}` : ''}
    </>
  )
}

/** 原始响应。展开时才序列化那一大段 JSON */
function RawResponse({ raw }: { raw: unknown }) {
  const { t } = useT()
  const [open, setOpen] = useState(false)
  let text = ''
  if (open) {
    try { text = JSON.stringify(raw, null, 2) } catch { text = '' }
  }
  const clipped = text.length > RAW_LIMIT
  return (
    <details className="raw" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>{t('sq.ask.process.raw')}</summary>
      {open && (
        <>
          <pre className="mono sq-json">{clipped ? text.slice(0, RAW_LIMIT) : text}</pre>
          {clipped && <p className="field-hint">{t('sq.ask.process.rawClipped', { n: RAW_LIMIT })}</p>}
        </>
      )}
    </details>
  )
}

/** 查询过程：普通模式下收在一个折叠里 */
export function ProcessDetails({ question, result, backfill, catalog }: {
  question: string
  result: QueryResult
  backfill: Backfill | null
  catalog: Catalog | null | undefined
}) {
  const { t, lang } = useT()
  const { intermediate } = splitSteps(result, question)
  const intermediateSet = new Set(intermediate.map((step) => step.index))
  const queried = [...new Set(result.steps.map((step) => step.database))]
  const candidateTables = backfill?.candidateTables ?? []
  const source = (database: string) => sourceLabel(catalog, database, lang)

  return (
    <details className="raw sq-process">
      <summary>{t('sq.ask.process.title')}</summary>
      <div className="sq-stack">
        <dl className="sq-kv">
          <KvRow label={t('sq.ask.process.traceId')}>
            <span className="mono">{result.traceId || '—'}</span>
            {result.traceId && <CopyButton text={result.traceId} label={t('sq.ask.action.copy')} />}
          </KvRow>
          <KvRow label={t('sq.ask.process.transport')}>{transportLabel(result.transport as Transport, t)}</KvRow>
          <KvRow label={t('sq.ask.process.route')}>
            {routeLabel(result.route, t)}
            {result.confidence !== null ? ` · ${t('sq.ask.process.confidence', { value: result.confidence.toFixed(2) })}` : ''}
          </KvRow>
          {result.reason && <KvRow label={t('sq.ask.process.reason')}><span lang="zh">{result.reason}</span></KvRow>}
          <KvRow label={t('sq.ask.process.candidates')}>
            {result.candidateDatabases.length ? result.candidateDatabases.map(source).join(', ') : '—'}
          </KvRow>
          {candidateTables.length > 0 && (
            <KvRow label={t('sq.ask.process.candidateTables')}><span className="mono">{candidateTables.join(', ')}</span></KvRow>
          )}
          <KvRow label={t('sq.ask.process.queried')}>
            {queried.length ? queried.map(source).join(', ') : t('sq.ask.process.noneQueried')}
          </KvRow>
          <KvRow label={t('sq.ask.process.status')}>
            <span className="mono">{result.executionStatus}</span>
            {result.statusSource !== 'contract' ? ` · ${t('sq.ask.process.statusDerived')}` : ''}
          </KvRow>
          {result.degraded && (
            <KvRow label={t('sq.ask.process.degraded')}><span className="mono">{result.degradeReason || 'true'}</span></KvRow>
          )}
          <KvRow label={t('sq.ask.process.time')}><TimeLine result={result} backfill={backfill} /></KvRow>
          {backfill?.totalTokens !== null && backfill?.totalTokens !== undefined && (
            <KvRow label={t('sq.ask.process.tokens')}>{String(backfill.totalTokens)}</KvRow>
          )}
        </dl>
        {result.steps.map((step) => (
          <div key={step.index} className="sq-step-card">
            <div className="sq-step-head">
              <span>{t('sq.ask.process.step', { n: step.index + 1 })} · {source(step.database)}</span>
              <StepChips step={step} intermediate={intermediateSet.has(step.index)} />
            </div>
            <SqlBlocks step={step} />
            <RepairList step={step} />
            {step.error && <p><span className="field-hint">{t('sq.ask.process.error')}</span> <span lang="zh">{step.error}</span></p>}
            <FindingList step={step} />
            {intermediateSet.has(step.index) && <IntermediateRows step={step} catalog={catalog} />}
            <ReadAt step={step} />
          </div>
        ))}
        <RawResponse raw={result.raw} />
      </div>
    </details>
  )
}

// ── 学习模式 ──────────────────────────────────────────────────────────────

/** 服务端发来的阶段事件，按到达顺序 */
function StageTimeline({ stages, catalog }: { stages: StreamStage[]; catalog: Catalog | null | undefined }) {
  const { t, lang } = useT()
  if (!stages.length) return null
  return (
    <div className="sq-stack-sm">
      <p className="sq-learn-title">{t('sq.ask.learn.timeline.title')}</p>
      <p className="field-hint">{t('sq.ask.learn.timeline.note')}</p>
      <div className="sq-timeline-grid">
        {stages.map((stage, index) => {
          const key = `sq.stage.${stage.stage}`
          return (
            <div key={index} className="sq-timeline-line">
              <span className="mono sq-t">{offset(stage)}</span>
              <span className="mono">{stage.stage}</span>
              <span>
                {hasKey(key) ? t(key) : ''}
                {stage.step !== null ? ` · ${t('sq.ask.process.step', { n: stage.step })}` : ''}
                {stage.database ? ` · ${sourceLabel(catalog, stage.database, lang)}` : ''}
                {stage.shortPath === true ? ` · ${t('sq.ask.learn.shortPath')}` : ''}
                {stage.withMemory === true ? ` · ${t('sq.ask.learn.withMemory')}` : ''}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

/** 这一步在阶段事件里出现过没有。没走流式时返回 null：不知道，不是没发生 */
function observed(stages: StreamStage[], step: LearnStep, streamed: boolean): StreamStage[] | null {
  if (!streamed) return null
  return stages.filter((stage) => STAGE_TO_STEP[stage.stage] === step)
}

function ObservedLine({ events }: { events: StreamStage[] | null }) {
  const { t } = useT()
  if (events === null) return <li className="field-hint">{t('sq.ask.learn.notReported')}</li>
  if (!events.length) return <li className="field-hint">{t('sq.ask.learn.notObserved')}</li>
  const names = [...new Set(events.map((stage) => stage.stage))].join(', ')
  return <li>{t('sq.ask.learn.observed', { stages: names, at: offset(events[0]!) || '—', n: events.length })}</li>
}

/**
 * 一步的四种状态，只有第一种有事实可列：
 *   happened   响应里有这一步的结果，或者（走流式时）收到过它的阶段事件
 *   skipped    确实没有发生——这条消息走的是对话回答，后面的步骤根本不会跑
 *   unreached  请求是去查库的，但在走到这一步之前就结束了（收到的阶段事件里没有它）
 *   unknown    请求是去查库的、没有结果，而这条请求路径不回报阶段：不知道走到了哪
 * 后三种不能混成一句"没有发生"：模型服务中途断掉时，规划明明已经开始了。
 */
type LearnState = 'happened' | 'skipped' | 'unreached' | 'unknown'
const STATE_TEXT: Record<Exclude<LearnState, 'happened'>, Key> = {
  skipped: 'sq.ask.learn.skipped', unreached: 'sq.ask.learn.unreached', unknown: 'sq.ask.learn.unknownStage',
}

function LearnStepItem({ number, id, state = 'happened', children }: {
  number: number; id: LearnStep; state?: LearnState; children: ReactNode
}) {
  const { t } = useT()
  const happened = state === 'happened'
  return (
    <li className={`sq-learn-step${happened ? '' : ' is-skipped'}`}>
      <span className="sq-learn-num" aria-hidden="true">{number}</span>
      <div className="sq-stack-sm">
        <p className="sq-learn-title">{t(`sq.ask.learn.${id}.title`)}</p>
        <div>
          <p className="field-hint">{t(happened ? 'sq.ask.learn.happened' : STATE_TEXT[state])}</p>
          {happened && <ul className="sq-bullets">{children}</ul>}
        </div>
        <p className="sq-learn-note">{t(`sq.ask.learn.${id}.note`)}</p>
        <p className="sq-learn-src mono">{LEARN_SOURCES[id]}</p>
      </div>
    </li>
  )
}

function LearnResult({ question, result, backfill, catalog }: {
  question: string; result: QueryResult; backfill: Backfill | null; catalog: Catalog | null | undefined
}) {
  const { t, lang } = useT()
  const view = classify(result)
  const streamed = result.transport === 'stream'
  const stages = result.stages ?? []
  const { intermediate } = splitSteps(result, question)
  const intermediateSet = new Set(intermediate.map((step) => step.index))
  const ran = result.queriedDatabase && result.steps.length > 0
  const queried = [...new Set(result.steps.map((step) => step.database))]
  const candidateTables = backfill?.candidateTables ?? []
  const source = (database: string) => sourceLabel(catalog, database, lang)
  const stateOf = (id: LearnStep, hasFacts: boolean): LearnState => {
    if (hasFacts) return 'happened'
    if (result.route !== 'database_query') return 'skipped'
    const events = observed(stages, id, streamed)
    if (events === null) return 'unknown'
    return events.length ? 'happened' : 'unreached'
  }
  // 阶段开始过、但没有结果回来（请求在这一步里结束了）：只有阶段事件这一条事实
  const noResult = <li><span className="field-hint">{t('sq.ask.learn.noStepResult')}</span></li>

  const planEvents = observed(stages, 'plan', streamed)
  const shortPath = planEvents?.find((stage) => stage.shortPath !== null)
  // 这里写的是观察到的事实（上一步某一列的取值，全部出现在下一步的 SQL 里），不是服务端的声明——
  // 响应里没有"哪一步把什么交给了哪一步"这样的字段。只对上一个值时特别注明。
  const handoffs = result.steps.flatMap((step) => passedOn(result, step, question).map((carried) => (
    <li key={`${step.index}-${carried.column}`}>
      {t(carried.values.length === 1 ? 'sq.ask.learn.plan.handoffOne' : 'sq.ask.learn.plan.handoff', {
        from: step.index + 1,
        to: carried.intoStep + 1,
        column: carried.column,
        values: carried.values.slice(0, 4).map(String).join(', '),
        n: carried.values.length,
      })}
    </li>
  )))
  const units = result.steps.flatMap((step) => Object.entries(step.units).map(([column, unit]) => `${column}: ${unit}`))

  return (
    <div className="sq-stack">
      <StageTimeline stages={stages} catalog={catalog} />
      {!streamed && <p className="field-hint">{t('sq.ask.learn.queryTransportNote')}</p>}
      <ol className="sq-learn-steps">
        <LearnStepItem number={1} id="understand">
          <ObservedLine events={observed(stages, 'understand', streamed)} />
          <li>
            {t('sq.ask.learn.understand.route')} <strong>{routeLabel(result.route, t)}</strong>
            {result.confidence !== null ? ` · ${t('sq.ask.process.confidence', { value: result.confidence.toFixed(2) })}` : ''}
          </li>
          {result.reason
            ? <li>{t('sq.ask.learn.understand.reason')} <span lang="zh">“{result.reason}”</span></li>
            : streamed ? <li><span className="field-hint">{t('sq.ask.learn.understand.noReason')}</span></li> : null}
          {!result.queriedDatabase && <li>{t('sq.ask.learn.understand.noQuery')}</li>}
          {result.degraded && <li>{t('sq.ask.learn.degraded')} <span className="mono">{result.degradeReason}</span></li>}
        </LearnStepItem>

        <LearnStepItem number={2} id="retrieve" state={stateOf('retrieve', ran || result.candidateDatabases.length > 0)}>
          <ObservedLine events={observed(stages, 'retrieve', streamed)} />
          <li>{t('sq.ask.learn.retrieve.candidates')} {result.candidateDatabases.length ? result.candidateDatabases.map(source).join(', ') : '—'}</li>
          {candidateTables.length > 0 && (
            <li>{t('sq.ask.learn.retrieve.tables')} <span className="mono">{candidateTables.join(', ')}</span></li>
          )}
          <li>{t('sq.ask.learn.retrieve.queried')} {queried.length ? queried.map(source).join(', ') : '—'}</li>
          {result.candidateDatabases.some((database) => !queried.includes(database)) && (
            <li><span className="field-hint">{t('sq.ask.learn.retrieve.differ')}</span></li>
          )}
        </LearnStepItem>

        <LearnStepItem number={3} id="plan" state={stateOf('plan', ran)}>
          <ObservedLine events={planEvents} />
          {shortPath && <li>{t(shortPath.shortPath ? 'sq.ask.learn.plan.shortPath' : 'sq.ask.learn.plan.fullPlan')}</li>}
          {ran
            ? <li>{t(result.steps.length === 1 ? 'sq.ask.learn.plan.single' : 'sq.ask.learn.plan.multi', {
              n: result.steps.length,
              databases: result.steps.map((step) => `${step.index + 1}: ${source(step.database)}`).join(' → '),
            })}</li>
            : noResult}
          {handoffs}
          {result.steps.length > 1 && !handoffs.length && <li>{t('sq.ask.learn.plan.independent')}</li>}
        </LearnStepItem>

        <LearnStepItem number={4} id="generate" state={stateOf('generate', ran)}>
          <ObservedLine events={observed(stages, 'generate', streamed)} />
          {!ran && noResult}
          {result.steps.map((step) => (
            <li key={step.index}>
              {t('sq.ask.process.step', { n: step.index + 1 })} · {source(step.database)}
              <SqlBlocks step={step} />
            </li>
          ))}
        </LearnStepItem>

        <LearnStepItem number={5} id="execute" state={stateOf('execute', ran)}>
          <ObservedLine events={observed(stages, 'execute', streamed)} />
          {!ran && noResult}
          {result.steps.map((step) => (
            <li key={step.index}>
              {t('sq.ask.process.step', { n: step.index + 1 })}: <StepChips step={step} intermediate={intermediateSet.has(step.index)} />
              <ReadAt step={step} />
              {step.error && <p><span className="field-hint">{t('sq.ask.process.error')}</span> <span lang="zh">{step.error}</span></p>}
              <RepairList step={step} />
              {intermediateSet.has(step.index) && <IntermediateRows step={step} catalog={catalog} />}
            </li>
          ))}
        </LearnStepItem>

        <LearnStepItem number={6} id="check" state={stateOf('check', ran)}>
          <ObservedLine events={observed(stages, 'check', streamed)} />
          {!ran && noResult}
          <li>
            {t('sq.ask.learn.check.status')}{' '}
            <span className="mono">execution_status={result.executionStatus}</span>{' '}
            <span className="mono">has_risk={String(result.hasRisk)}</span>{' '}
            <span className="mono">degraded={String(result.degraded)}</span>{' '}
            <span className="mono">result_truncated={String(result.truncated)}</span>
          </li>
          {view.findings.length
            ? <li>{t('sq.ask.learn.check.findings', { n: view.findings.length })}{result.steps.map((step) => <FindingList key={step.index} step={step} />)}</li>
            : <li>{t('sq.ask.learn.check.noFindings')}</li>}
          <li>
            {units.length
              ? <>{t('sq.ask.learn.check.units')} <span className="mono">{units.join(', ')}</span>{result.currency ? ` · currency=${result.currency}` : ''}</>
              : <span className="field-hint">{t('sq.ask.learn.check.noUnits')}</span>}
          </li>
          {result.asOf && <li>{t('sq.ask.learn.check.asOf', { time: result.asOf, source: result.asOfSource || 'unknown' })}</li>}
        </LearnStepItem>
      </ol>
      <dl className="sq-kv">
        <KvRow label={t('sq.ask.process.traceId')}>
          <span className="mono">{result.traceId || '—'}</span>
          {result.traceId && <CopyButton text={result.traceId} label={t('sq.ask.action.copy')} />}
        </KvRow>
        <KvRow label={t('sq.ask.process.time')}>
          <TimeLine result={result} backfill={backfill} />
          {backfill?.totalTokens !== null && backfill?.totalTokens !== undefined
            ? ` · ${t('sq.ask.process.tokens')}: ${backfill.totalTokens}` : ''}
        </KvRow>
      </dl>
      <RawResponse raw={result.raw} />
    </div>
  )
}

/** 学习模式：等待中讲收到的阶段事件，有结果后逐步讲解，失败时说没有可讲的 */
export function LearnPanel({ question, transport, stages, result, failed, backfill, catalog }: {
  question: string
  transport: Transport
  stages: StreamStage[]
  result: QueryResult | null
  failed: boolean
  backfill: Backfill | null
  catalog: Catalog | null | undefined
}) {
  const { t } = useT()
  const shownTransport = (result?.transport as Transport | undefined) ?? transport
  let body: ReactNode
  if (result) {
    body = <LearnResult question={question} result={result} backfill={backfill} catalog={catalog} />
  } else if (failed) {
    body = <><StageTimeline stages={stages} catalog={catalog} /><p>{t('sq.ask.learn.failedBeforeResult')}</p></>
  } else {
    body = stages.length
      ? <StageTimeline stages={stages} catalog={catalog} />
      : <p className="field-hint">{t(transport === 'stream' ? 'sq.ask.learn.waitingStream' : 'sq.ask.learn.waitingQuery')}</p>
  }
  return (
    <section className="sq-learn" aria-label={t('sq.ask.learn.title')}>
      <div className="sq-learn-head">
        <span>{t('sq.ask.learn.title')}</span>
        <Chip tone="info">{transportLabel(shownTransport, t)}</Chip>
      </div>
      <div className="sq-learn-body">{body}</div>
    </section>
  )
}
