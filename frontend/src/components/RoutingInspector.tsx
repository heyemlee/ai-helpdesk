import { Chip, Empty, KV, MeterRow, SectionLabel } from './ui'
import { agentLabel, confidenceTone, ms, num } from '@/lib/format'
import type { ChatMessage } from '@/hooks/useChatStream'

/**
 * 路由检查器：把最近一轮的编排决策完整摊开。
 *
 * 这些字段后端一直在返回，但如果只显示最终回答，
 * "三路融合 / 领域打分 / 主辅协作 / 置信度转人工"就全是不可见的。
 *
 * 与对话流的分工：正文里的拦截区块说"切掉了哪句话"，这里说"依据什么规则、为什么"。
 */
export function RoutingInspector({ message }: { message?: ChatMessage }) {
  if (!message || (!message.routing && !message.result)) {
    return <Empty>发一条消息后，这里会显示本轮的完整路由决策。</Empty>
  }

  const { meta, routing, result } = message
  const scores = routing?.domain_scores ?? {}
  const sourceScores = meta?.intent_source_scores ?? {}
  const primary = routing?.primary_agent ?? result?.primary_agent
  const supporting = routing?.supporting_agents ?? result?.supporting_agents ?? []
  const entities = Object.entries(meta?.entities ?? {})

  return (
    <>
      <section>
        <SectionLabel>意图识别</SectionLabel>
        <div className="chip-row" style={{ marginBottom: 10 }}>
          {meta?.intent && <Chip mono>{meta.intent}</Chip>}
          {meta?.intent_strategy === 'fast_path' ? (
            <Chip tone="success" title="规则锚点命中，跳过 Embedding 和 LLM 两路网络调用">
              规则快通道 · 0 次 LLM
            </Chip>
          ) : meta?.intent_strategy ? (
            <Chip tone="info" title="规则 10% + Embedding 20% + LLM 70% 加权投票">
              三路加权投票
            </Chip>
          ) : null}
          {meta?.intent_latency_ms !== undefined && <Chip mono>{ms(meta.intent_latency_ms)}</Chip>}
        </div>
        {Object.keys(sourceScores).length > 0 && (
          <dl className="dl">
            {(['rules', 'embedding', 'llm'] as const).map((k) => (
              <MeterRow
                key={k} label={k} value={sourceScores[k] ?? 0}
                tone={(sourceScores[k] ?? 0) > 0 ? 'primary' : undefined}
              />
            ))}
          </dl>
        )}
        {entities.length > 0 && (
          <div className="chip-row" style={{ marginTop: 10 }}>
            {entities.map(([k, v]) => (
              <Chip key={k} mono>{k}: {v.join(', ')}</Chip>
            ))}
          </div>
        )}
      </section>

      {Object.keys(scores).length > 0 && (
        <section>
          <SectionLabel>领域打分</SectionLabel>
          <dl className="dl">
            {Object.entries(scores).sort(([, a], [, b]) => b - a).map(([agent, score]) => {
              const isPrimary = agent === primary
              const isSupport = supporting.includes(agent)
              return (
                <MeterRow
                  key={agent}
                  label={agentLabel(agent)}
                  suffix={isPrimary ? ' ●' : isSupport ? ' ○' : ''}
                  value={score}
                  tone={isPrimary ? 'primary' : isSupport ? 'info' : undefined}
                />
              )
            })}
          </dl>
          <p className="legend" style={{ marginTop: 10 }}>
            <span><i className="p" />主 Agent</span>
            <span><i className="s" />辅助 Agent（跨域时并行执行）</span>
          </p>
        </section>
      )}

      <section>
        <SectionLabel>置信度与处置</SectionLabel>
        <div className="chip-row">
          {routing?.confidence !== undefined && (
            <Chip tone={confidenceTone(routing.confidence)}>路由 {num(routing.confidence)}</Chip>
          )}
          {result?.answer_confidence !== undefined && (
            <Chip tone={confidenceTone(result.answer_confidence)}>
              综合 {num(result.answer_confidence)}
            </Chip>
          )}
          {result?.handoff && <Chip tone="danger">已转人工</Chip>}
          {result?.needs_clarification && <Chip tone="caution">澄清追问</Chip>}
          {result?.escalated && !result.handoff && <Chip tone="caution">已升级</Chip>}
          {meta?.knowledge_used && <Chip tone="info">RAG</Chip>}
        </div>
        <dl style={{ marginTop: 10 }}>
          {result?.latency_ms !== undefined && <KV label="总耗时">{ms(result.latency_ms)}</KV>}
          {result?.request_id && <KV label="Request ID"><span className="mono">{result.request_id}</span></KV>}
          {result?.ticket_id && <KV label="工单"><span className="mono">{result.ticket_id}</span></KV>}
        </dl>
        {result?.handoff_reason && (
          <p className="note danger" style={{ marginTop: 10 }}>{result.handoff_reason}</p>
        )}
      </section>

      {result?.policy_violations && result.policy_violations.length > 0 && (
        <section>
          <SectionLabel>答案策略拦截 · {result.policy_violations.length} 处</SectionLabel>
          <div className="dl">
            {result.policy_violations.map((v, i) => (
              <div className="violation" key={i}>
                <b>{v.label}</b> · <code>{v.rule}</code>
                <p>命中依据：<code>{v.evidence}</code></p>
                <p>{v.reason}</p>
              </div>
            ))}
          </div>
        </section>
      )}

      {routing?.reason && (
        <details className="raw">
          <summary>原始路由决策</summary>
          <pre>{routing.reason}</pre>
        </details>
      )}
    </>
  )
}
