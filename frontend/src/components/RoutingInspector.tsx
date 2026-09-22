import { Icon } from './Icon'
import { Chip, Empty, KV, MeterRow, SectionLabel } from './ui'
import { agentLabel, confidenceTone, ms, num } from '@/lib/format'
import { useT } from '@/i18n'
import type { ChatMessage } from '@/hooks/useChatStream'

/**
 * 路由检查器：把最近一轮的编排决策完整摊开。
 *
 * 这些字段后端一直在返回，但如果只显示最终回答，
 * "三路融合 / 领域打分 / 主辅协作 / 置信度转人工"就全是不可见的。
 *
 * 与对话流的分工：正文里的拦截区块说"切掉了哪句话"，这里说"依据什么规则、为什么"。
 */
export function RoutingInspector({ message, thresholds }: {
  message?: ChatMessage
  /** 协作门槛的两个后端常量，来自 /monitor 快照。拿不到就不画门槛线——
   *  前端不写死后端常量，宁可少画一条线也不要一个会悄悄漂移的数字。 */
  thresholds?: { minScore: number; minRatio: number }
}) {
  const { t } = useT()
  if (message?.restored) {
    return <Empty>{t('in.restored')}</Empty>
  }
  if (!message || (!message.routing && !message.result)) {
    return <div className="inspector-empty"><div className="welcome-mark"><Icon name="panel" size={24} /></div><h3>{t('in.empty.title')}</h3><p>{t('in.empty')}</p><ol><li>{t('in.intent')}</li><li>{t('in.primary')}</li><li>{t('in.disp')}</li></ol></div>
  }

  const { meta, routing, result } = message
  const scores = routing?.domain_scores ?? {}
  const sourceScores = meta?.intent_source_scores ?? {}
  const primary = routing?.primary_agent ?? result?.primary_agent
  const supporting = routing?.supporting_agents ?? result?.supporting_agents ?? []
  // 真实后端每轮都回全套实体键，抽不到就是空数组（order_id/product/date/amount/
  // item_code 五个一个不少）。只判 length > 0 等于判「有没有这些键」，永远为真——
  // 界面上就常驻五个 `order_id:` 这样后面什么都没有的空 chip。要的是有值的那几个。
  const entities = Object.entries(meta?.entities ?? {}).filter(([, v]) => (v?.length ?? 0) > 0)

  return (
    <>
      <section>
        <SectionLabel>{t('in.intent')}</SectionLabel>
        <div className="chip-row" style={{ marginBottom: 10 }}>
          {meta?.intent && <Chip mono>{meta.intent}</Chip>}
          {meta?.intent_strategy === 'fast_path' ? (
            <Chip tone="success" title={t('in.fast.title')}>
              {t('in.fast')}
            </Chip>
          ) : meta?.intent_strategy ? (
            <Chip tone="info" title={t('in.vote.title')}>
              {t('in.vote')}
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
                skipped={meta?.intent_strategy === 'fast_path' && k !== 'rules'}
                notStartedLabel={t('cw.notStarted')}
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
          <SectionLabel>{t('in.domain')}</SectionLabel>
          <dl className="dl">
            {(() => {
              const ranked = Object.entries(scores).sort(([, a], [, b]) => b - a)
              // 门槛取两者较大值：绝对分与「主分 × 比例」。主 Agent 不受它约束，
              // 所以只画在其余行上。
              const gate = thresholds
                ? Math.max(thresholds.minScore, (ranked[0]?.[1] ?? 0) * thresholds.minRatio)
                : undefined
              return ranked.map(([agent, score]) => {
                const isPrimary = agent === primary
                const isSupport = supporting.includes(agent)
                return (
                  <MeterRow
                    key={agent}
                    label={agentLabel(agent, t)}
                    suffix={isPrimary ? ' ●' : isSupport ? ' ○' : ''}
                    value={score}
                    tone={isPrimary ? 'primary' : isSupport ? 'info' : undefined}
                    gate={isPrimary ? undefined : gate}
                  />
                )
              })
            })()}
          </dl>
          <p className="legend" style={{ marginTop: 10 }}>
            <span><i className="p" />{t('in.primary')}</span>
            <span><i className="s" />{t('in.support')}</span>
            {thresholds && (
              <span>
                <i className="g" />{t('in.gate')}{' '}
                {num(Math.max(
                  thresholds.minScore,
                  (Object.values(scores).sort((a, b) => b - a)[0] ?? 0) * thresholds.minRatio,
                ))}
              </span>
            )}
          </p>
        </section>
      )}

      <section>
        <SectionLabel>{t('in.disp')}</SectionLabel>
        <div className="chip-row">
          {routing?.confidence !== undefined && (
            <Chip tone={confidenceTone(routing.confidence)}>{t('in.route')} {num(routing.confidence)}</Chip>
          )}
          {result?.answer_confidence !== undefined && (
            <Chip tone={confidenceTone(result.answer_confidence)}>
              {t('in.combined')} {num(result.answer_confidence)}
            </Chip>
          )}
          {result?.handoff && <Chip tone="danger">{t('cw.handedOff')}</Chip>}
          {result?.needs_clarification && <Chip tone="caution">{t('in.clarify')}</Chip>}
          {result?.escalated && !result.handoff && <Chip tone="caution">{t('cw.escalated')}</Chip>}
          {meta?.knowledge_used && <Chip tone="info">RAG</Chip>}
        </div>
        <dl style={{ marginTop: 10 }}>
          {result?.latency_ms !== undefined && <KV label={t('in.total')}>{ms(result.latency_ms)}</KV>}
          {result?.request_id && <KV label="Request ID"><span className="mono">{result.request_id}</span></KV>}
          {result?.ticket_id && <KV label={t('cw.ticket')}><span className="mono">{result.ticket_id}</span></KV>}
        </dl>
        {result?.handoff_reason && (
          <p className="note danger" style={{ marginTop: 10 }}>{result.handoff_reason}</p>
        )}
      </section>

      {result?.policy_violations && result.policy_violations.length > 0 && (
        <section>
          <SectionLabel>{t('in.policy')} · {result.policy_violations.length}</SectionLabel>
          <div className="dl">
            {result.policy_violations.map((v, i) => (
              <div className="violation" key={i}>
                <b>{v.label}</b> · <code>{v.rule}</code>
                <p>{t('in.evidence')} <code>{v.evidence}</code></p>
                <p>{v.reason}</p>
              </div>
            ))}
          </div>
        </section>
      )}

      {routing?.reason && (
        <details className="raw">
          <summary>{t('in.raw')}</summary>
          <pre>{routing.reason}</pre>
        </details>
      )}
    </>
  )
}
