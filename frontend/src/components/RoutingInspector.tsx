import { Badge, EmptyHint, Panel, ScoreBar } from './ui'
import { agentLabel, confidenceTone, ms, num } from '@/lib/format'
import type { ChatMessage } from '@/hooks/useChatStream'

/**
 * 路由检查器：把最近一轮的编排决策完整摊开。
 *
 * 这些字段后端一直在返回，但如果只显示最终回答，
 * "三路融合 / 领域打分 / 主辅协作 / 置信度转人工"就全是不可见的。
 */
export function RoutingInspector({ message }: { message?: ChatMessage }) {
  if (!message || (!message.routing && !message.result)) {
    return (
      <Panel title="路由检查器" badge={<Badge tone="info">Routing</Badge>}>
        <EmptyHint>发一条消息后，这里会显示本轮的完整路由决策</EmptyHint>
      </Panel>
    )
  }

  const meta = message.meta
  const routing = message.routing
  const result = message.result
  const scores = routing?.domain_scores ?? {}
  const sourceScores = meta?.intent_source_scores ?? {}
  const primary = routing?.primary_agent ?? result?.primary_agent
  const supporting = routing?.supporting_agents ?? result?.supporting_agents ?? []
  const answerConfidence = result?.answer_confidence

  return (
    <Panel title="路由检查器" badge={<Badge tone="info">Routing</Badge>}>
      <div className="space-y-4">
        {/* 意图识别：走了哪条通道 */}
        <div>
          <h3 className="mb-1.5 text-[11px] font-semibold tracking-wide text-slate-400 uppercase">
            意图识别
          </h3>
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge tone="muted">{meta?.intent ?? result?.agent_type ?? '-'}</Badge>
            {meta?.intent_strategy === 'fast_path' ? (
              <Badge tone="good" title="规则锚点命中，跳过 Embedding 和 LLM 两路网络调用">
                规则快通道 · 0 次 LLM
              </Badge>
            ) : meta?.intent_strategy ? (
              <Badge tone="info" title="规则 10% + Embedding 20% + LLM 70% 加权投票">
                三路融合
              </Badge>
            ) : null}
            {meta?.intent_latency_ms !== undefined && (
              <Badge tone="muted">{ms(meta.intent_latency_ms)}</Badge>
            )}
          </div>
          {Object.keys(sourceScores).length > 0 && (
            <dl className="mt-2 space-y-1">
              {(['rules', 'embedding', 'llm'] as const).map((k) => (
                <div key={k} className="flex items-center gap-2">
                  <dt className="w-16 text-[11px] text-slate-500">{k}</dt>
                  <dd className="flex-1">
                    <ScoreBar
                      value={sourceScores[k] ?? 0}
                      tone={(sourceScores[k] ?? 0) > 0 ? 'info' : 'muted'}
                      label={num(sourceScores[k] ?? 0)}
                    />
                  </dd>
                </div>
              ))}
            </dl>
          )}
        </div>

        {/* 领域打分：三个专家怎么竞争出主 Agent */}
        {Object.keys(scores).length > 0 && (
          <div>
            <h3 className="mb-1.5 text-[11px] font-semibold tracking-wide text-slate-400 uppercase">
              领域打分
            </h3>
            <dl className="space-y-1">
              {Object.entries(scores)
                .sort(([, a], [, b]) => b - a)
                .map(([agent, score]) => {
                  const isPrimary = agent === primary
                  const isSupport = supporting.includes(agent)
                  return (
                    <div key={agent} className="flex items-center gap-2">
                      <dt className="flex w-20 items-center gap-1 text-[11px] text-slate-400">
                        {agentLabel(agent)}
                        {isPrimary && <span className="text-emerald-400">●</span>}
                        {isSupport && <span className="text-sky-400">○</span>}
                      </dt>
                      <dd className="flex-1">
                        <ScoreBar
                          value={score}
                          tone={isPrimary ? 'good' : isSupport ? 'info' : 'muted'}
                          label={num(score)}
                        />
                      </dd>
                    </div>
                  )
                })}
            </dl>
            <p className="mt-1.5 text-[11px] text-slate-500">
              <span className="text-emerald-400">●</span> 主 Agent
              <span className="ml-2 text-sky-400">○</span> 辅助 Agent（跨域时并行执行）
            </p>
          </div>
        )}

        {/* 综合置信度与转人工 */}
        <div>
          <h3 className="mb-1.5 text-[11px] font-semibold tracking-wide text-slate-400 uppercase">
            置信度与处置
          </h3>
          <div className="flex flex-wrap gap-1.5">
            {routing?.confidence !== undefined && (
              <Badge tone={confidenceTone(routing.confidence)}>
                路由 {num(routing.confidence)}
              </Badge>
            )}
            {answerConfidence !== undefined && (
              <Badge tone={confidenceTone(answerConfidence)}>
                综合 {num(answerConfidence)}
              </Badge>
            )}
            {result?.handoff && <Badge tone="bad">已转人工</Badge>}
            {result?.needs_clarification && <Badge tone="warn">澄清追问</Badge>}
            {result?.escalated && !result?.handoff && <Badge tone="warn">已升级</Badge>}
            {meta?.knowledge_used && <Badge tone="info">RAG</Badge>}
            {result?.latency_ms !== undefined && <Badge tone="muted">{ms(result.latency_ms)}</Badge>}
          </div>
          {result?.handoff_reason && (
            <p className="mt-2 rounded-lg bg-rose-500/8 px-2.5 py-1.5 text-[11px] leading-relaxed text-rose-300">
              {result.handoff_reason}
            </p>
          )}
        </div>

        {/* 答案策略拦截 */}
        {result?.policy_violations && result.policy_violations.length > 0 && (
          <div>
            <h3 className="mb-1.5 text-[11px] font-semibold tracking-wide text-slate-400 uppercase">
              答案策略拦截 · {result.policy_violations.length} 处
            </h3>
            <ul className="space-y-1.5">
              {result.policy_violations.map((v, i) => (
                <li key={i} className="rounded-lg bg-amber-500/8 px-2.5 py-1.5 text-[11px] text-amber-200">
                  <span className="font-medium">{v.label}</span>
                  <span className="mx-1 text-amber-400/60">·</span>
                  <code className="font-mono">{v.evidence}</code>
                  <p className="mt-0.5 leading-relaxed text-amber-200/70">{v.reason}</p>
                </li>
              ))}
            </ul>
          </div>
        )}

        {routing?.reason && (
          <details className="text-[11px]">
            <summary className="cursor-pointer text-slate-500 hover:text-slate-300">
              原始路由决策
            </summary>
            <pre className="mt-1.5 overflow-x-auto rounded-lg bg-slate-950/60 p-2.5 font-mono
                            text-[10px] leading-relaxed text-slate-400">
              {routing.reason}
            </pre>
          </details>
        )}
      </div>
    </Panel>
  )
}
