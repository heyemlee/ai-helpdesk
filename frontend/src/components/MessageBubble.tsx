import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Chip } from './ui'
import { Icon } from './Icon'
import { agentIdentity, agentLabel, ms, num } from '@/lib/format'
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
  const { meta, routing: r, result: res } = message
  if (!r || !res) return null

  const rows: Array<[string, React.ReactNode]> = []

  if (meta?.intent_strategy === 'fast_path') {
    rows.push(['意图', <>
      <code className="mono">{meta.intent}</code> · 规则锚点命中，本轮 <strong>0 次 LLM 调用</strong>——省下 Embedding 与 LLM 两次网络往返
    </>])
  } else if (meta?.intent_source_scores) {
    const v = meta.intent_source_scores
    rows.push(['意图', <>
      <code className="mono">{meta.intent}</code> · 三路加权投票：
      LLM {K.W.llm.toFixed(2)}×{num(v.llm ?? 0)} ＋ Embedding {K.W.embedding.toFixed(2)}×{num(v.embedding ?? 0)} ＋ 规则 {K.W.rules.toFixed(2)}×{num(v.rules ?? 0)}
    </>])
  }

  const ranked = Object.entries(r.domain_scores ?? {}).sort((a, b) => b[1] - a[1])
  const top = ranked[0]
  if (top) {
    const gate = Math.max(K.SUPPORT_MIN, top[1] * K.SUPPORT_RATIO)
    const support = r.supporting_agents ?? []
    const second = ranked[1]
    rows.push(['路由', <>
      {agentLabel(top[0])} {num(top[1])} 胜出。协作门槛取两者较大值：绝对 {K.SUPPORT_MIN} 与
      主分×{K.SUPPORT_RATIO}={num(top[1] * K.SUPPORT_RATIO)} → <strong>{num(gate)}</strong>
      {support.length > 0
        ? <>；{support.map(agentLabel).join('、')} 过线，并行协作后由主 Agent 合并</>
        : second ? <>；次高 {agentLabel(second[0])} {num(second[1])} 未过线，本轮单 Agent 作答</> : null}
    </>])
  }

  rows.push(['出站', res.policy_violations?.length
    ? <>触发 <code className="mono">{res.policy_violations[0]!.rule}</code>：按句缓冲校验后整句切除才发出，流式与非流式受同一套策略约束</>
    : <>答案策略逐句校验通过，无切除</>])

  const c = res.answer_confidence
  rows.push(['处置', res.handoff
    ? <>综合置信度 {num(c)} —— 已建单转人工，本轮对话记录一并转交，人工接手时客户无需复述</>
    : res.needs_clarification
      ? <>综合置信度 {num(c)} <strong>低于</strong>澄清阈值 {K.CLARIFY.toFixed(2)} → 追问而不是硬答</>
      : <>综合置信度 {num(c)} 高于澄清阈值 {K.CLARIFY.toFixed(2)}，正常作答</>])

  rows.push(['耗时', ms(res.latency_ms)])

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

  // routing 还没到 —— 谁在答都不知道，不编造身份
  if (!routing?.primary_agent) {
    return (
      <article className="turn assistant">
        <div className="thinking"><span className="pulse" />正在编排…</div>
      </article>
    )
  }

  const agent = agentIdentity(routing.primary_agent)
  const support = routing.supporting_agents ?? []
  const cut = result?.policy_violations?.[0]

  return (
    <article className="turn assistant">
      {result?.handoff ? (
        // 转人工是会话的处置结果，不是某个 Agent 在说话
        <p className="turn-status">
          <Icon name="person" size={14} />
          已转人工
          {result.ticket_id && <>· 工单 <span className="mono">{result.ticket_id}</span></>}
        </p>
      ) : (
        <div className="chip-row">
          <p className="turn-role">
            {agent.name}
            {support.length > 0 && (
              <>
                <span className="sep">·</span>
                <span className="support">＋ {support.map(agentLabel).join('、')} 并行协作</span>
              </>
            )}
          </p>
          {result?.needs_clarification && <Chip tone="caution">需要补充信息</Chip>}
          {result?.escalated && <Chip tone="caution">已升级</Chip>}
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
            答案策略切除 {result!.policy_violations!.length} 句 · {cut.label}
          </p>
          <p className="redaction-cut">{cut.sentence}</p>
          {onInspect && (
            <button className="redaction-why" onClick={onInspect}>
              为什么被切？在检查器里看规则与依据
            </button>
          )}
        </div>
      )}

      {explain && result && <ExplainBlock message={message} />}

      <div className="turn-foot">
        <span className="turn-actions">
          <button
            className="icon-btn" title="复制回复" aria-label="复制回复"
            onClick={() => navigator.clipboard?.writeText(message.content)}
          >
            <Icon name="copy" size={16} />
          </button>
          {onInspect && (
            <button
              className="icon-btn" title="查看本轮编排详情" aria-label="查看本轮编排详情"
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
