import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Chip } from './ui'
import { Icon } from './Icon'
import { agentIdentity, agentLabel, ms, num } from '@/lib/format'
import { useT } from '@/i18n'
import type { Key } from '@/i18n/catalog'
import type { ChatMessage } from '@/hooks/useChatStream'

/**
 * 单条消息。
 *
 * 这是客服工作台，不是调试面板：默认只呈现「谁答的 + 回复」，
 * 通道、耗时、置信度一律留给右侧检查器——它们本来就在那儿，
 * 再挂一份在每条回复下面是纯冗余，还会把正常对话弄得像后台日志。
 * 只有真正需要人处置的信号（转人工 / 澄清 / 策略拦截）才进入阅读流。
 *
 * 分阶段渲染：SSE 序列是 meta → routing → delta* → result，
 * routing 之前不知道谁在答，result 之前不知道处置结果，都不提前显示。
 */
/** 后端真实常量。改后端要同步改这里——每个值的源码出处见架构讲解页 index.html。 */
const K = {
  SUPPORT_MIN: 0.45,    // SUPPORTING_MIN_SCORE          agent_orchestrator.py:608
  SUPPORT_RATIO: 0.55,  // SUPPORTING_MIN_RATIO          :609
  CLARIFY: 0.5,         // CLARIFY_CONFIDENCE_THRESHOLD  :606
  W: { llm: 0.7, embedding: 0.2, rules: 0.1 },  // _VOTE_WEIGHTS intent_recognizer.py:522
}

/**
 * 讲解模式的一轮解释。每行是一句带真实数字的因果句，不是裸指标堆——
 * 「给了却不解释」正是这套界面最早被指出的问题。
 */
function ExplainBlock({ message }: { message: ChatMessage }) {
  const { t } = useT()
  const { meta, routing: r, result: res } = message
  if (!r || !res) return null

  const rows: Array<[string, React.ReactNode]> = []

  if (meta?.intent_strategy === 'fast_path') {
    rows.push([t('ex.intent'), <>
      <code className="mono">{meta.intent}</code> · {t('ex.fastPath')}
    </>])
  } else if (meta?.intent_source_scores) {
    const v = meta.intent_source_scores
    rows.push([t('ex.intent'), <>
      <code className="mono">{meta.intent}</code> · {t('ex.vote')}{' '}
      LLM {K.W.llm.toFixed(2)}×{num(v.llm ?? 0)} ＋ Embedding {K.W.embedding.toFixed(2)}×{num(v.embedding ?? 0)} ＋ {t('ex.rules')} {K.W.rules.toFixed(2)}×{num(v.rules ?? 0)}
    </>])
  }

  const ranked = Object.entries(r.domain_scores ?? {}).sort((a, b) => b[1] - a[1])
  const top = ranked[0]
  if (top) {
    const gate = Math.max(K.SUPPORT_MIN, top[1] * K.SUPPORT_RATIO)
    const support = r.supporting_agents ?? []
    const second = ranked[1]
    rows.push([t('ex.route'), <>
      {agentLabel(top[0], t)} {num(top[1])} {t('ex.wins')} {K.SUPPORT_MIN} {t('ex.and')}{K.SUPPORT_RATIO}={num(top[1] * K.SUPPORT_RATIO)} → <strong>{num(gate)}</strong>
      {support.length > 0
        ? <>; {support.map((x) => agentLabel(x, t)).join(', ')} {t('ex.passed')}</>
        : second ? <>; {t('ex.runnerUp')} {agentLabel(second[0], t)} {num(second[1])} {t('ex.notPassed')}</> : null}
    </>])
  }

  rows.push([t('ex.out'), res.policy_violations?.length
    ? <>{t('ex.outHit1')} <code className="mono">{res.policy_violations[0]!.rule}</code> {t('ex.outHit2')}</>
    : <>{t('ex.outClean')}</>])

  const c = res.answer_confidence
  rows.push([t('ex.disp'), res.handoff
    ? <>{t('ex.conf')} {num(c)} — {t('ex.handoff')}</>
    : res.needs_clarification
      ? <>{t('ex.conf')} {num(c)} <strong>{t('ex.below')}</strong> {t('ex.clarifyTh')} {K.CLARIFY.toFixed(2)} → {t('ex.askInstead')}</>
      : <>{t('ex.conf')} {num(c)} {t('ex.above')} {t('ex.clarifyTh')} {K.CLARIFY.toFixed(2)}, {t('ex.normal')}</>])

  rows.push([t('ex.latency'), ms(res.latency_ms)])

  return (
    <div className="explain">
      {rows.map(([k, v]) => (
        <div className="explain-row" key={k}><b>{k}</b><span>{v}</span></div>
      ))}
    </div>
  )
}

export function MessageBubble({ message, onInspect, explain }: {
  message: ChatMessage
  onInspect?: () => void
  explain?: boolean
}) {
  const { t } = useT()
  const { routing, result } = message

  if (message.role === 'user') {
    return (
      <article className="turn user">
        <div className="bubble">{message.content}</div>
      </article>
    )
  }

  if (message.error) {
    return (
      <article className="turn assistant">
        <p className="note danger">{message.error}</p>
      </article>
    )
  }

  // 刷新后从审计日志恢复的历史：只有原文和作答 Agent，没有路由/置信度，
  // 不能走下面「routing 还没到 = 正在编排」那条分支，否则永远转圈
  if (message.restored) {
    const agent = message.agentType ? agentIdentity(message.agentType, t)
        : { name: t('agent.reply'), mark: '·' }
    return (
      <article className="turn assistant restored">
        <div className="chip-row">
          <p className="turn-role">{agent.name}</p>
          <Chip>{t('cw.history')}</Chip>
        </div>
        <div className="body">
          <div className="prose-chat">
            <Markdown remarkPlugins={[remarkGfm]}>{message.content}</Markdown>
          </div>
        </div>
      </article>
    )
  }

  // routing 还没到 —— 谁在答都不知道，不编造身份
  if (!routing?.primary_agent) {
    return (
      <article className="turn assistant">
        <div className="thinking"><span className="pulse" />{t('cw.orchestrating')}</div>
      </article>
    )
  }

  const agent = agentIdentity(routing.primary_agent, t)
  const support = routing.supporting_agents ?? []
  const cut = result?.policy_violations?.[0]

  return (
    <article className="turn assistant">
      {result?.handoff ? (
        // 转人工是会话的处置结果，不是某个 Agent 在说话
        <p className="turn-status">
          <Icon name="person" size={14} />
          {t('cw.handedOff')}
          {result.ticket_id && <>· {t('cw.ticket')} <span className="mono">{result.ticket_id}</span></>}
        </p>
      ) : (
        <div className="chip-row">
          <p className="turn-role">
            {agent.name}
            {support.length > 0 && (
              <>
                <span className="sep">·</span>
                <span className="support">＋ {support.map((x) => agentLabel(x, t)).join(', ')} {t('cw.parallel')}</span>
              </>
            )}
          </p>
          {result?.needs_clarification && <Chip tone="caution">{t('cw.needInfo')}</Chip>}
          {result?.escalated && <Chip tone="caution">{t('cw.escalated')}</Chip>}
        </div>
      )}

      <div className="body">
        {/* 流式下每次增量都重渲染半截 Markdown——未闭合的语法先当普通文本，
            补全后自然变成格式。react-markdown 默认不解析原始 HTML。 */}
        <div className="prose-chat">
          <Markdown remarkPlugins={[remarkGfm]}>{message.content}</Markdown>
        </div>
        {message.streaming && <span className="caret" />}
      </div>

      {cut && (
        <div className="redaction">
          <p className="redaction-head">
            <Icon name="shield" size={14} />
            {t('cw.cut', { n: result!.policy_violations!.length })} · {t(`policy.${cut.rule}` as Key)}
          </p>
          <p className="redaction-cut">{cut.sentence}</p>
          {onInspect && (
            <button className="redaction-why" onClick={onInspect}>
              {t('cw.why')}
            </button>
          )}
        </div>
      )}

      {explain && result && <ExplainBlock message={message} />}

      <div className="turn-foot">
        <span className="turn-actions">
          <button
            className="icon-btn" title={t('cw.copy')} aria-label={t('cw.copy')}
            onClick={() => navigator.clipboard?.writeText(message.content)}
          >
            <Icon name="copy" size={16} />
          </button>
          {onInspect && (
            <button
              className="icon-btn" title={t('cw.inspectTurn')} aria-label={t('cw.inspectTurn')}
              onClick={onInspect}
            >
              <Icon name="panel" size={16} />
            </button>
          )}
        </span>
      </div>
    </article>
  )
}
