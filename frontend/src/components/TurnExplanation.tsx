import type { ReactNode } from 'react'
import { agentIdentity, agentLabel, ms, num } from '@/lib/format'
import { handoffReason } from '@/lib/handoff'
import { llmReasonKey } from '@/lib/llm'
import { useT } from '@/i18n'
import type { Key } from '@/i18n/catalog'
import { fallbackAgent, type ChatMessage } from '@/hooks/useChatStream'
import type { RoutingConfig, TrajectoryEntry } from '@/api/types'

const TOOL_NAMES: Record<string, Key> = {
  knowledge_search: 'lm.tools.name.knowledge_search',
  order_data_query: 'lm.tools.name.order_data_query',
}

const TOOL_OUTCOMES: Record<string, Key> = {
  empty: 'lm.tools.empty', evidence: 'lm.tools.evidence', unverified: 'lm.tools.unverified',
  failed: 'lm.tools.failed', unavailable: 'lm.tools.failed', error: 'lm.tools.failed',
  not_applicable: 'lm.tools.not_applicable', refused: 'lm.tools.refused', skipped: 'lm.tools.skipped',
  budget: 'lm.tools.budget',
  invalid_args: 'lm.tools.invalid', unavailable_tool: 'lm.tools.unknown_tool',
}

const FORCED: Record<string, Key> = {
  model_calls: 'lm.tools.forced.model_calls', tool_calls: 'lm.tools.forced.tool_calls',
  deadline: 'lm.tools.forced.deadline',
}

/** 一次工具调用的结局。知识库查到了就说几段；认不出的结局照原样写出来，不吞掉。 */
function toolOutcome(call: TrajectoryEntry, t: ReturnType<typeof useT>['t']): string {
  if (call.outcome === 'found') {
    const sources = call.trace?.sources
    return t('lm.tools.found', { n: Array.isArray(sources) ? sources.length : 0 })
  }
  const key = TOOL_OUTCOMES[call.outcome]
  return key ? t(key) : t('lm.tools.other', { outcome: call.outcome || '—' })
}

/**
 * 学习模式：把这一轮**实际发生的事**按顺序讲一遍——
 * 意图识别 → 知识检索 → 专家选择 → 业务数据委派 → 回答检查 → 转人工。
 *
 * 每一句都只转述后端在这一轮响应里报告的东西（meta / routing / result）。
 * 后端没报的就说"没有报告"，不从别的现象倒推：比如有没有查订单库，
 * 以 `business_data.outcome` 为准，不靠"回复里出现了订单号"来猜。
 * 门槛这类数字取自 /health 的编排配置，取不到就不写具体数。
 *
 * 模型服务不可用的轮次另有一套说法：没有 Agent 作答、没有可检查的回答、没有转人工。
 * 照着正常轮次的句式写会得出"逐句检查过，没有去掉任何内容""不需要转人工"这种
 * 字面上没错、读起来像一切正常的话。
 */
export function TurnExplanation({ message, routingConfig, onInspect }: {
  message: ChatMessage
  routingConfig?: RoutingConfig
  onInspect?: () => void
}) {
  const { t, lang } = useT()
  const { meta, routing, result } = message
  if (!result) return null

  const rows: Array<[Key, ReactNode]> = []
  const intent = <code className="mono">{meta?.intent ?? '—'}</code>
  const unavailable = !!result.service_unavailable
  // 准备阶段因为模型不可用整段跳过了（后端在同一处把检索和委派一起跳过，只在
  // business_data 上留了记号）
  const prepSkipped = unavailable && meta?.business_data?.outcome === 'model_unavailable'

  // 1. 意图识别
  if (meta?.intent_strategy === 'fast_path') {
    rows.push(['lm.intent', <>{t('lm.intent.fast.before')} {intent}{t('lm.intent.fast.after')}</>])
  } else if (meta?.intent_source_scores && meta.intent_confidence !== undefined) {
    const v = meta.intent_source_scores
    rows.push(['lm.intent', <>
      {t('lm.intent.vote.before')} {intent}{' '}
      {t('lm.intent.vote.after', {
        conf: num(meta.intent_confidence), rules: num(v.rules ?? 0),
        emb: num(v.embedding ?? 0), llm: num(v.llm ?? 0),
      })}
    </>])
  } else if (meta?.intent) {
    rows.push(['lm.intent', <>{t('lm.intent.plain')} {intent}</>])
  }

  // 2. 知识检索
  const kb = meta?.knowledge
  const sources = meta?.knowledge_sources ?? []
  if (kb && kb.attempted !== undefined) {
    if (!kb.attempted) {
      rows.push(['lm.kb', t(prepSkipped ? 'lm.kb.unavailable' : 'lm.kb.skipped')])
    } else if (kb.degraded && sources.length === 0) {
      rows.push(['lm.kb', t('lm.kb.degraded')])
    } else if (sources.length === 0) {
      rows.push(['lm.kb', t('lm.kb.none')])
    } else {
      const docs = [...new Set(sources.map((s) => s.doc_title || s.title))].join(', ')
      rows.push(['lm.kb', <>
        {t(kb.mode === 'direct' ? 'lm.kb.direct' : 'lm.kb.rewrite', { n: sources.length, docs })}
        {kb.reranked && <> {t('lm.kb.reranked')}</>}
        {kb.corrected && <> {t('lm.kb.corrected')}</>}
        {kb.evidence_ok === true && <> {t('lm.kb.evidence.ok')}</>}
        {kb.evidence_ok === false && <> {t('lm.kb.evidence.weak')}</>}
      </>])
    }
  } else if (meta) {
    // 老后端只有 knowledge_used 这一个布尔
    rows.push(['lm.kb', t(meta.knowledge_used ? 'lm.kb.legacy.used' : 'lm.kb.legacy.unused')])
  }

  // 3. 专家选择
  if (unavailable) {
    // 选中过谁照实说（非流式和流式半途失败时后端报了），但没有谁作答
    const picked = routing?.primary_agent
    rows.push(['lm.route', <>
      {t('lm.route.unavailable', { reason: t(llmReasonKey(result.unavailable_reason)) })}
      {picked && picked !== 'system' && picked !== 'handoff' && (
        <> {t('lm.route.unavailable.selected', { agent: agentIdentity(picked, t).name })}</>
      )}
    </>])
  } else if (routing) {
    const scores = routing.domain_scores ?? {}
    const primary = routing.primary_agent
    const support = routing.supporting_agents ?? []
    const name = agentIdentity(primary, t).name
    const score = scores[primary]
    const stoodIn = fallbackAgent(routing, result, routingConfig)
    if (primary === 'handoff') {
      rows.push(['lm.route', t('lm.route.handoff')])
    } else if (stoodIn) {
      // 选了谁、谁答的是两件事：专家这次没答完，回复是兜底 Agent 写的
      rows.push(['lm.route', t('lm.route.fallback', {
        agent: name, score: score === undefined ? '—' : num(score), actual: agentIdentity(stoodIn, t).name,
      })])
    } else if (score === undefined) {
      rows.push(['lm.route', t('lm.route.plain', { agent: name })])
    } else if (support.length > 0) {
      const top = Math.max(...Object.values(scores))
      const gate = routingConfig
        ? Math.max(routingConfig.supporting_min_score, top * routingConfig.supporting_min_ratio)
        : undefined
      rows.push(['lm.route', <>
        {t('lm.route.multi', {
          agent: name, score: num(score),
          support: support.map((s) => `${agentLabel(s, t)} (${num(scores[s])})`).join(', '),
        })}
        {gate !== undefined && <> {t('lm.route.gate', { gate: num(gate) })}</>}
      </>])
    } else {
      rows.push(['lm.route', t('lm.route.single', { agent: name, score: num(score) })])
    }
  }

  // 4. 业务数据委派
  const data = meta?.business_data
  const dbs = (data?.databases ?? []).join(', ') || t('cw.data.systems')
  const DATA_KEYS: Record<string, Key> = {
    not_configured: 'lm.data.not_configured', skipped: 'lm.data.skipped',
    evidence: 'lm.data.evidence', unverified: 'lm.data.unverified', failed: 'lm.data.failed',
    unavailable: 'lm.data.unavailable', not_applicable: 'lm.data.not_applicable',
    empty: 'lm.data.empty', error: 'lm.data.error', model_unavailable: 'lm.data.model_unavailable',
  }
  const dataKey = data?.outcome ? DATA_KEYS[data.outcome] : undefined
  rows.push(['lm.data', dataKey
    ? <>
        {t(dataKey, { dbs, reason: data?.reason || t('lm.data.noReason') })}
        {data?.trace_id && <> <span className="mono">trace {data.trace_id}</span></>}
      </>
    : t('lm.data.unknown')])

  // 4½. 工具循环：专家作答时自己又查了什么。只在循环打开的轮次有（后端才会报 answer_usage）。
  // 网关不支持工具时这一轮退回了单次生成，不能说成"专家决定不查"；被要求直接作答的也照实说，
  // 一次委派要十几秒，耗时到秒的写出来，这一轮为什么慢一眼能看到。
  // 模型服务不可用的轮次没有人答完：不能说成"专家直接用预取的资料作答"；作答阶段的调用次数
  // 只数拿到了用量的那几次，出错的那次不在里面，写出"0 次"读起来像根本没调过模型，所以不写。
  // 出错之前已经查过的照实列出来。
  const usage = result.answer_usage
  if (usage?.model_calls !== undefined) {
    const calls = (result.trajectory ?? []).filter((c) => c.actor === 'model')
    const forced = usage.forced ? FORCED[usage.forced] : undefined
    rows.push(['lm.tools', <>
      {usage.fallback === 'tools_unsupported' ? t('lm.tools.fallback')
        : calls.length === 0 ? t(unavailable ? 'lm.tools.unavailable' : 'lm.tools.none') : <>
        {t('lm.tools.calls', { n: calls.length })}{' '}
        {calls.map((c, i) => {
          const name = TOOL_NAMES[c.tool]
          return (
            <span key={c.call_id || i}>
              {i > 0 && t('lm.tools.sep')}
              {name ? t(name) : <code className="mono">{c.tool}</code>}
              {typeof c.args?.query === 'string' && <> <span className="lm-quote">“{c.args.query}”</span></>}
              {' → '}{toolOutcome(c, t)}
              {c.dedup && <> {t('lm.tools.dedup')}</>}
              {(c.latency_ms ?? 0) >= 1000 && <> {t('lm.tools.took', { t: ms(c.latency_ms ?? 0) })}</>}
            </span>
          )
        })}
        {unavailable && <> {t('lm.tools.unavailable.after')}</>}
      </>}
      {forced && <> {t(forced)}</>}
      {!unavailable && <> {t('lm.tools.usage', { n: usage.model_calls })}</>}
    </>])
  }

  // 5. 回答检查
  const cuts = result.policy_violations ?? []
  rows.push(['lm.check', unavailable ? t('lm.check.none') : cuts.length
    ? t('lm.check.cut', {
        n: cuts.length,
        rule: [...new Set(cuts.map((c) => t(`policy.${c.rule}` as Key)))].join(', '),
      })
    : t('lm.check.clean')])

  // 6. 转人工
  const conf = result.answer_confidence
  const confText = conf === undefined ? '—' : num(conf)
  if (unavailable) {
    rows.push(['lm.handoff', t('lm.handoff.unavailable')])
  } else if (result.handoff) {
    rows.push(['lm.handoff', <>
      {t('lm.handoff.yes')} <span className="lm-quote">{handoffReason(result.handoff_reason, t, lang) || '—'}</span>{' '}
      {result.ticket_id
        ? <>{t('lm.handoff.ticket')} <span className="mono">{result.ticket_id}</span>.</>
        : t('lm.handoff.noTicket')}
    </>])
  } else if (result.needs_clarification) {
    const th = routingConfig?.clarify_confidence_threshold
    rows.push(['lm.handoff', <>
      {t('lm.handoff.clarify', { conf: confText })}
      {th !== undefined && <> {t('lm.handoff.threshold', { v: num(th) })}</>}
    </>])
  } else {
    rows.push(['lm.handoff', t('lm.handoff.none', { conf: confText })])
  }

  return (
    <section className="explain" aria-label={t('lm.title')}>
      <p className="explain-title">{t('lm.title')}</p>
      <ol>
        {rows.map(([key, value]) => (
          <li className="explain-row" key={key}><b>{t(key)}</b><span>{value}</span></li>
        ))}
      </ol>
      <p className="explain-foot">
        {/* 直接转人工这类不调模型的路径耗时是 0，写出来没有信息量 */}
        {(result.latency_ms ?? 0) >= 1 && <span>{t('lm.latency')} {ms(result.latency_ms)}</span>}
        {onInspect && (
          <button className="redaction-why" onClick={onInspect}>{t('lm.inspect')}</button>
        )}
      </p>
    </section>
  )
}
